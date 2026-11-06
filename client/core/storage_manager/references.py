"""Attach only proved received-media references to metadata scan entries."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib

from .types import canonical


@dataclass(frozen=True)
class Reference:
    media_id: str
    chat_key: str
    category: str
    protected: bool = False
    display_name: str = ""
    media_type: str = ""


@dataclass
class References:
    files: dict
    complete: bool = True

    def classify(self, entry):
        ref = self.files.get(entry.key)
        if not self.complete or ref is None or entry.root.scope != "account":
            return replace(entry, reason="unmatched") if entry.category in ("media", "voice") else entry
        if ref.protected:
            return replace(entry, reason="important", chat_key=ref.chat_key, display_name=ref.display_name,
                           media_type=ref.media_type)
        return replace(entry, reason="received", media_id=ref.media_id, chat_key=ref.chat_key,
                       display_name=ref.display_name, media_type=ref.media_type)


def chat_token(jid):
    """Private policies need a stable comparison key, not a raw phone/JID."""
    return hashlib.sha256(str(jid).encode("utf-8")).hexdigest()


def collect(chats, read_count, read_page, path_for, cache_id, *,
            cancel=lambda: False, protected=lambda msg: False, label_for=lambda jid, msg: "",
            media_type_for=lambda msg: msg.get("messageType", ""),
            max_messages=100_000):
    """Read the full selected local histories in bounded pages, without HTTP.

    The caller supplies only chats allowed by the current vault/lock state.
    A changing count, missing/decryption-filtered row or an exhausted budget
    makes references incomplete; no partially proved media is made cleanable.
    """
    result = References({})
    seen = 0
    for jid in dict.fromkeys(chats):
        if cancel():
            result.complete = False
            break
        try:
            count = read_count(jid)
            if type(count) is not int or count < 0 or seen + count > max_messages:
                result.complete = False
                break
            offset = 0
            while offset < count:
                if cancel():
                    result.complete = False
                    return result
                batch = read_page(jid, min(256, count - offset), offset)
                if not isinstance(batch, list) or not batch:
                    result.complete = False
                    break
                offset += len(batch)
                seen += len(batch)
                for message in batch:
                    if not isinstance(message, dict):
                        result.complete = False
                        continue
                    key = message.get("key") or {}
                    msg_id = key.get("id")
                    kind = message.get("messageType") or message.get("type")
                    if not isinstance(msg_id, str) or not msg_id or kind not in (
                            "imageMessage", "videoMessage", "audioMessage", "documentMessage"):
                        continue
                    path = path_for(kind, msg_id)
                    token = cache_id(msg_id)
                    if not path or not token:
                        continue
                    candidate = Reference(token, chat_token(jid),
                                          "voice" if kind == "audioMessage" else "media",
                                          protected=key.get("fromMe") is not False
                                          or bool(message.get("_local_pending") or message.get("_send_failed"))
                                          or protected(message), display_name=label_for(jid, message),
                                          media_type=media_type_for(message))
                    # Shared/colliding cache ids are never made eligible from
                    # the most permissive reference alone.
                    previous = result.files.get(canonical(path))
                    if previous is not None and (previous != candidate):
                        candidate = replace(candidate, protected=True)
                    result.files[canonical(path)] = candidate
            if offset != count or read_count(jid) != count:
                result.complete = False
        except (OSError, ValueError, TypeError, AttributeError):
            result.complete = False
    return result
