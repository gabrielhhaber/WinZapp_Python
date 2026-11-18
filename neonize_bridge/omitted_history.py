"""WhatsApp On-Demand History Sync v2 (`OmittedConversationMetadata`) wire helpers.

Between 2026-10-28 and 2026-11-17 WhatsApp for iOS lowered the number of chats
included in INITIAL_BOOTSTRAP from 700 (699 excluding status@broadcast) to 400
(399), sending older conversations only via OmittedConversationMetadata
batches — and only when the companion advertises
`DeviceProps.HistorySyncConfig.supportOmittedConversationMetadata = true`
(field 28) during registration. Conversations received only through those
batches start with zero local messages and must be fetched with
`HistorySyncOnDemandRequest` in `INITIAL_REVERSE_HISTORY` mode (mode = 1,
supportInlineResponse = true).

Handling these newer protobuf fields at the wire level avoids regenerating
50,000+ lines of `.pb.go`/`_pb2.py` while preserving unknown fields on both
Go (`google.golang.org/protobuf`) and Python (`google.protobuf`).
"""

#: DeviceProps.HistorySyncConfig.supportOmittedConversationMetadata = 28
FIELD_SUPPORT_OMITTED_METADATA = 28

#: HistorySync.omittedConversationMetadata = 25, snapshotId = 26
FIELD_OMITTED_METADATA = 25
FIELD_OMITTED_SNAPSHOT_ID = 26

#: HistorySync.HistorySyncType values
SYNC_TYPE_INITIAL_BOOTSTRAP = 0
SYNC_TYPE_FULL = 2
SYNC_TYPE_RECENT = 3
SYNC_TYPE_RECENT_CONVERSATIONS_ON_DEMAND = 9

#: PeerDataOperationRequestType.HISTORY_SYNC_ON_DEMAND = 3
PEER_OP_HISTORY_SYNC_ON_DEMAND = 3

#: HistorySyncOnDemandRequest extra fields (accountLid=6, supportInlineResponse=7, mode=8)
FIELD_ONDEMAND_ACCOUNT_LID = 6
FIELD_ONDEMAND_SUPPORT_INLINE = 7
FIELD_ONDEMAND_MODE = 8
MODE_INITIAL_REVERSE_HISTORY = 1


def _decode_varint(buf: bytes, pos: int) -> tuple[int, int]:
    val = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7
        if shift >= 70:
            raise ValueError("varint too long")
    raise ValueError("truncated varint")


def _encode_varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _encode_tag(field_num: int, wire_type: int) -> bytes:
    return _encode_varint((field_num << 3) | wire_type)


def _encode_varint_field(field_num: int, value: int) -> bytes:
    return _encode_tag(field_num, 0) + _encode_varint(value)


def _encode_bytes_field(field_num: int, payload: bytes) -> bytes:
    return _encode_tag(field_num, 2) + _encode_varint(len(payload)) + payload


def _iter_fields(buf: bytes):
    """Yields (field_num, wire_type, raw_bytes, value) for each top-level field."""
    pos = 0
    n = len(buf)
    while pos < n:
        start = pos
        tag, pos = _decode_varint(buf, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            value, pos = _decode_varint(buf, pos)
        elif wire_type == 1:
            end = pos + 8
            if end > n:
                raise ValueError("truncated 64-bit field")
            value = buf[pos:end]
            pos = end
        elif wire_type == 2:
            length, pos = _decode_varint(buf, pos)
            end = pos + length
            if end > n:
                raise ValueError("truncated length-delimited field")
            value = buf[pos:end]
            pos = end
        elif wire_type == 5:
            end = pos + 4
            if end > n:
                raise ValueError("truncated 32-bit field")
            value = buf[pos:end]
            pos = end
        else:
            raise ValueError(f"unsupported wire type {wire_type}")
        yield field_num, wire_type, buf[start:pos], value


def _strip_fields(buf: bytes, drop: set[int]) -> bytes:
    out = bytearray()
    for field_num, _wt, raw, _val in _iter_fields(buf):
        if field_num not in drop:
            out.extend(raw)
    return bytes(out)


def _wire_field_bytes(msg, target_field: int) -> list[bytes]:
    """Reads all raw length-delimited/varint values for `target_field` on `msg`."""
    try:
        from google.protobuf.unknown_fields import UnknownFieldSet
    except ImportError:
        UnknownFieldSet = None
    if UnknownFieldSet is not None:
        try:
            unknowns = UnknownFieldSet(msg)
            values = [bytes(uf.data) for uf in unknowns if uf.field_number == target_field]
            if values:
                return values
        except Exception:
            pass
    # Fallback via serialized bytes only when caller checked it's cheap.
    raw = msg.SerializeToString()
    return [val for num, wt, _raw, val in _iter_fields(raw)
            if num == target_field and wt == 2 and isinstance(val, bytes)]


def advertise_omitted_metadata(props):
    """Sets `historySyncConfig.supportOmittedConversationMetadata = True` (field 28).

    Idempotent: strips any prior field 28 before appending it so unknown fields
    never accumulate across repeated calls.
    """
    cfg = props.historySyncConfig
    raw = _strip_fields(cfg.SerializeToString(), {FIELD_SUPPORT_OMITTED_METADATA})
    cfg.Clear()
    cfg.MergeFromString(raw + _encode_varint_field(FIELD_SUPPORT_OMITTED_METADATA, 1))
    return props


def has_omitted_metadata_support(props) -> bool:
    """Whether `props.historySyncConfig` carries field 28 set to True."""
    if props is None or not hasattr(props, "historySyncConfig"):
        return False
    raw = props.historySyncConfig.SerializeToString()
    for num, wt, _raw, val in _iter_fields(raw):
        if num == FIELD_SUPPORT_OMITTED_METADATA and wt == 0:
            return bool(val)
    return False


def snapshot_id_of(history_sync) -> str:
    """Returns `HistorySync.omittedConversationMetadataSnapshotId` (field 26), or ''."""
    if history_sync is None or not hasattr(history_sync, "SerializeToString"):
        return ""
    if int(getattr(history_sync, "syncType", -1)) != SYNC_TYPE_INITIAL_BOOTSTRAP:
        try:
            from google.protobuf.unknown_fields import UnknownFieldSet
            unknowns = UnknownFieldSet(history_sync)
            if not any(uf.field_number == FIELD_OMITTED_SNAPSHOT_ID for uf in unknowns):
                return ""
        except Exception:
            return ""
    for val in _wire_field_bytes(history_sync, FIELD_OMITTED_SNAPSHOT_ID):
        return val.decode("utf-8", errors="replace")
    return ""


def _parse_omitted_metadata(payload: bytes) -> dict:
    from neonize.proto.waHistorySync.WAWebProtobufsHistorySync_pb2 import Conversation
    out = {
        "snapshot_id": "",
        "timestamp_ms": 0,
        "batch_index": 0,
        "batch_count": 0,
        "complete": False,
        "conversations": [],
    }
    for num, wt, _raw, val in _iter_fields(payload):
        if num == 1 and wt == 2:
            out["snapshot_id"] = val.decode("utf-8", errors="replace")
        elif num == 2 and wt == 0:
            out["timestamp_ms"] = int(val)
        elif num == 3 and wt == 0:
            out["batch_index"] = int(val)
        elif num == 4 and wt == 0:
            out["batch_count"] = int(val)
        elif num == 5 and wt == 0:
            out["complete"] = bool(val)
        elif num == 6 and wt == 2:
            out["conversations"].append(Conversation.FromString(val))
    return out


def omitted_batch_of(history_sync) -> "dict | None":
    """Parses `HistorySync.omittedConversationMetadata` (field 25) or returns None.

    Skips serializing large conversation-bearing blocks: WhatsApp sends omitted
    metadata batches with `conversations` empty, and protobuf's UnknownFieldSet
    covers any hypothetical mixed payload in O(unknown fields).
    """
    if history_sync is None or not hasattr(history_sync, "SerializeToString"):
        return None
    has_conversations = bool(getattr(history_sync, "conversations", None))
    if has_conversations:
        try:
            from google.protobuf.unknown_fields import UnknownFieldSet
            if not any(uf.field_number == FIELD_OMITTED_METADATA
                       for uf in UnknownFieldSet(history_sync)):
                return None
        except Exception:
            return None
    chunks = _wire_field_bytes(history_sync, FIELD_OMITTED_METADATA)
    if not chunks:
        return None
    return _parse_omitted_metadata(chunks[-1])


def is_omitted_batch(history_sync) -> bool:
    """Whether `history_sync` carries an OmittedConversationMetadata batch."""
    return omitted_batch_of(history_sync) is not None


def tracks_transfer_progress(history_sync) -> bool:
    """Whether `history_sync` is a numbered RECENT/FULL message transfer block."""
    try:
        sync_type = int(getattr(history_sync, "syncType", -1))
    except (TypeError, ValueError):
        return False
    return sync_type in (SYNC_TYPE_FULL, SYNC_TYPE_RECENT) and not is_omitted_batch(history_sync)


def encode_omitted_batch(conversations, *, snapshot_id="snap-1", timestamp_ms=1700000000000,
                         batch_index=0, batch_count=1, complete=True, sync_type=SYNC_TYPE_RECENT,
                         chunk_order=1, progress=100):
    """Builds a `HistorySync` containing an `OmittedConversationMetadata` batch (for tests)."""
    from neonize.proto.waHistorySync.WAWebProtobufsHistorySync_pb2 import HistorySync
    body = bytearray()
    if snapshot_id:
        body.extend(_encode_bytes_field(1, snapshot_id.encode("utf-8")))
    if timestamp_ms:
        body.extend(_encode_varint_field(2, int(timestamp_ms)))
    body.extend(_encode_varint_field(3, int(batch_index)))
    body.extend(_encode_varint_field(4, int(batch_count)))
    if complete:
        body.extend(_encode_varint_field(5, 1))
    for conv in conversations:
        body.extend(_encode_bytes_field(6, conv.SerializeToString()))
    hs = HistorySync(syncType=sync_type, chunkOrder=chunk_order, progress=progress)
    raw = hs.SerializeToString() + _encode_bytes_field(FIELD_OMITTED_METADATA, bytes(body))
    return HistorySync.FromString(raw)


def _norm_jid(jid: str) -> str:
    text = str(jid or "").strip()
    if not text or "@" not in text:
        return ""
    user, _, server = text.partition("@")
    user = user.split(":", 1)[0]
    if not user:
        return ""
    return f"{user}@{'s.whatsapp.net' if server == 'c.us' else server}"


def account_lid_of(conv) -> str:
    """Best `@lid` address carried by a `Conversation` proto, or ''."""
    for attr in ("accountLid", "lidJID", "ID"):
        jid = _norm_jid(getattr(conv, attr, ""))
        if jid.endswith("@lid"):
            return jid
    return ""


def conversation_lid_mappings(conversations) -> list[tuple[str, str]]:
    """Extracts unique `(lid_jid, pn_jid)` pairs from `Conversation` protos."""
    pairs = []
    seen = set()
    for conv in conversations or ():
        cid = _norm_jid(getattr(conv, "ID", ""))
        pn = _norm_jid(getattr(conv, "pnJID", "")) or (
            cid if cid.endswith("@s.whatsapp.net") else "")
        lid = account_lid_of(conv)
        if (lid.endswith("@lid") and pn.endswith("@s.whatsapp.net")
                and (lid, pn) not in seen):
            seen.add((lid, pn))
            pairs.append((lid, pn))
    return pairs


def block_lid_mappings(data, extra_conversations=()) -> list[tuple[str, str]]:
    """Extracts unique `(lid_jid, pn_jid)` pairs from `HistorySync.phoneNumberToLidMappings`
    and its conversation entries (plus any omitted-batch conversations)."""
    pairs = []
    seen = set()
    for m in getattr(data, "phoneNumberToLidMappings", None) or ():
        lid = _norm_jid(getattr(m, "lidJID", ""))
        pn = _norm_jid(getattr(m, "pnJID", ""))
        if lid.endswith("@lid") and pn.endswith("@s.whatsapp.net") and (lid, pn) not in seen:
            seen.add((lid, pn))
            pairs.append((lid, pn))
    convs = list(getattr(data, "conversations", None) or ()) + list(extra_conversations or ())
    for pair in conversation_lid_mappings(convs):
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    return pairs


def build_initial_reverse_request(chat_jid: str, count: int = 50, account_lid: str = ""):
    """Builds `PeerDataOperationRequestMessage` (`HISTORY_SYNC_ON_DEMAND`) for a
    conversation that has no local message anchor yet (`mode = INITIAL_REVERSE_HISTORY`)."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message, ProtocolMessage
    message = Message()
    protocol = message.protocolMessage
    protocol.type = ProtocolMessage.PEER_DATA_OPERATION_REQUEST_MESSAGE
    op = protocol.peerDataOperationRequestMessage
    op.peerDataOperationRequestType = PEER_OP_HISTORY_SYNC_ON_DEMAND
    req = op.historySyncOnDemandRequest
    req.chatJID = _norm_jid(chat_jid)
    req.onDemandMsgCount = int(count)

    fields = req.DESCRIPTOR.fields_by_name
    extra = bytearray()
    lid = _norm_jid(account_lid) if account_lid else ""
    if lid.endswith("@lid"):
        if "accountLid" in fields:
            req.accountLid = lid
        else:
            extra.extend(_encode_bytes_field(FIELD_ONDEMAND_ACCOUNT_LID, lid.encode("utf-8")))
    if "supportInlineResponse" in fields:
        req.supportInlineResponse = True
    else:
        extra.extend(_encode_varint_field(FIELD_ONDEMAND_SUPPORT_INLINE, 1))
    extra.extend(_encode_varint_field(FIELD_ONDEMAND_MODE, MODE_INITIAL_REVERSE_HISTORY))

    raw = req.SerializeToString() + bytes(extra)
    req.Clear()
    req.MergeFromString(raw)
    return message
