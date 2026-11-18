"""Tests for WhatsApp On-Demand History Sync v2 wire helpers (omitted_history.py)."""
from neonize.proto.waCompanionReg.WAWebProtobufsCompanionReg_pb2 import DeviceProps
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ProtocolMessage
from neonize.proto.waHistorySync.WAWebProtobufsHistorySync_pb2 import Conversation, HistorySync

import omitted_history as oh


def test_advertise_omitted_metadata_sets_field_28_once_and_preserves_other_fields():
    props = DeviceProps(os="WinZapp", platformType=DeviceProps.DESKTOP, requireFullSync=True)
    props.historySyncConfig.fullSyncDaysLimit = 3650
    props.historySyncConfig.fullSyncSizeMbLimit = 102400
    props.historySyncConfig.storageQuotaMb = 102400

    oh.advertise_omitted_metadata(props)
    assert oh.has_omitted_metadata_support(props) is True
    first = props.historySyncConfig.SerializeToString()

    oh.advertise_omitted_metadata(props)
    assert props.historySyncConfig.SerializeToString() == first
    assert props.historySyncConfig.fullSyncDaysLimit == 3650
    assert props.historySyncConfig.fullSyncSizeMbLimit == 102400
    assert props.historySyncConfig.storageQuotaMb == 102400


def test_omitted_batch_roundtrip_and_non_omitted_blocks():
    c1 = Conversation(
        ID="111@lid",
        name="Older Contact",
        conversationTimestamp=1750000000,
        archived=True,
        pinned=1750000100,
        muteEndTime=1800000000,
        pnJID="5511999990001@s.whatsapp.net",
        lidJID="111@lid",
        accountLid="111@lid",
    )
    c2 = Conversation(
        ID="120363000000000001@g.us",
        name="Old Group",
        lastMsgTimestamp=1740000000,
        isParentGroup=False,
    )
    hs = oh.encode_omitted_batch(
        [c1, c2],
        snapshot_id="snap-abc",
        timestamp_ms=1780000000000,
        batch_index=1,
        batch_count=3,
        complete=False,
    )
    assert oh.is_omitted_batch(hs) is True
    batch = oh.omitted_batch_of(hs)
    assert batch["snapshot_id"] == "snap-abc"
    assert batch["timestamp_ms"] == 1780000000000
    assert batch["batch_index"] == 1
    assert batch["batch_count"] == 3
    assert batch["complete"] is False
    assert [c.ID for c in batch["conversations"]] == ["111@lid", "120363000000000001@g.us"]
    assert batch["conversations"][0].pnJID == "5511999990001@s.whatsapp.net"
    assert batch["conversations"][0].archived is True

    plain = HistorySync(syncType=HistorySync.RECENT, conversations=[c1])
    assert oh.is_omitted_batch(plain) is False
    assert oh.omitted_batch_of(plain) is None


def test_snapshot_id_of_reads_field_26():
    boot = HistorySync(syncType=HistorySync.INITIAL_BOOTSTRAP)
    raw = boot.SerializeToString() + oh._encode_bytes_field(oh.FIELD_OMITTED_SNAPSHOT_ID, b"snap-xyz")
    parsed = HistorySync.FromString(raw)
    assert oh.snapshot_id_of(parsed) == "snap-xyz"
    assert oh.snapshot_id_of(HistorySync(syncType=HistorySync.RECENT)) == ""


def test_conversation_lid_mappings_and_account_lid():
    c_lid = Conversation(ID="111:0@lid", pnJID="5511999990001@c.us")
    c_pn = Conversation(ID="5511999990002@s.whatsapp.net", accountLid="222:1@lid")
    c_group = Conversation(ID="120363000000000001@g.us")
    assert oh.account_lid_of(c_lid) == "111@lid"
    assert oh.account_lid_of(c_pn) == "222@lid"
    assert oh.account_lid_of(c_group) == ""
    assert oh.conversation_lid_mappings([c_lid, c_pn, c_group, c_lid]) == [
        ("111@lid", "5511999990001@s.whatsapp.net"),
        ("222@lid", "5511999990002@s.whatsapp.net"),
    ]


def test_build_initial_reverse_request_encodes_mode_1_inline_and_account_lid():
    msg = oh.build_initial_reverse_request("111@lid", count=50, account_lid="111@lid")
    proto = msg.protocolMessage
    assert proto.type == ProtocolMessage.PEER_DATA_OPERATION_REQUEST_MESSAGE
    op = proto.peerDataOperationRequestMessage
    assert op.peerDataOperationRequestType == oh.PEER_OP_HISTORY_SYNC_ON_DEMAND
    req = op.historySyncOnDemandRequest
    assert req.chatJID == "111@lid"
    assert req.onDemandMsgCount == 50
    assert req.oldestMsgID == ""

    raw = req.SerializeToString()
    fields = {num: (wt, val) for num, wt, _raw, val in oh._iter_fields(raw)}
    assert fields[oh.FIELD_ONDEMAND_ACCOUNT_LID] == (2, b"111@lid")
    assert fields[oh.FIELD_ONDEMAND_SUPPORT_INLINE] == (0, 1)
    assert fields[oh.FIELD_ONDEMAND_MODE] == (0, 1)


def test_tracks_transfer_progress_excludes_omitted_metadata_and_non_transfer_types():
    c = Conversation(ID="111@lid", conversationTimestamp=100)
    assert oh.tracks_transfer_progress(HistorySync(syncType=HistorySync.FULL)) is True
    assert oh.tracks_transfer_progress(HistorySync(syncType=HistorySync.RECENT)) is True
    assert oh.tracks_transfer_progress(HistorySync(syncType=HistorySync.INITIAL_BOOTSTRAP)) is False
    assert oh.tracks_transfer_progress(HistorySync(syncType=HistorySync.ON_DEMAND)) is False
    omitted_recent = oh.encode_omitted_batch([c], sync_type=HistorySync.RECENT, progress=100)
    assert oh.tracks_transfer_progress(omitted_recent) is False
