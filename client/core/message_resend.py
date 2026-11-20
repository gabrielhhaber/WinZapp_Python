"""Which own messages can be sent again by hand, and with what.

A send that failed (``_send_failed``: the queue gave up) or that WinZapp never
saw confirmed (``_send_unconfirmed``: it timed out with an unknown outcome)
gets "Try sending again" in the message's menu and on its key. It is never
retried automatically: an unconfirmed one may have gone out, so trying again
is the user's call.

A text resends its wire text. An attachment resends the file it was sent
from, recorded on the pending row as ``RESEND_PATH_KEY`` when it was queued
(ui/conversation_panel/attachments.py); without that file (moved, deleted, or
a recording WinZapp made and removed) there is nothing to send again.
"""

import os

#: The file an attachment was sent from, kept on its pending row.
RESEND_PATH_KEY = "_resend_path"

_MEDIA_TYPES = {
    "imageMessage": "image",
    "videoMessage": "video",
    "audioMessage": "audio",
    "documentMessage": "document",
}


def needs_resend(msg) -> bool:
    """An own message whose send failed or was never confirmed."""
    if not isinstance(msg, dict) or not (msg.get("key") or {}).get("fromMe"):
        return False
    return msg.get("_send_failed") is True or msg.get("_send_unconfirmed") is True


def resend_request(msg, file_exists=os.path.isfile):
    """What sending *msg* again would send: ``("text", text)``,
    ``("media", path, media_type, caption)``, or None when nothing can be."""
    if not needs_resend(msg):
        return None
    body = msg.get("message") or {}
    msg_type = msg.get("messageType", "")
    if msg_type in ("conversation", "extendedTextMessage", ""):
        text = body.get("conversation") or (body.get("extendedTextMessage") or {}).get("text") or ""
        return ("text", text) if isinstance(text, str) and text else None
    media_type = _MEDIA_TYPES.get(msg_type)
    path = msg.get(RESEND_PATH_KEY)
    if media_type is None or not isinstance(path, str) or not path or not file_exists(path):
        return None
    inner = body.get(msg_type) if isinstance(body.get(msg_type), dict) else {}
    caption = inner.get("caption") if isinstance(inner.get("caption"), str) else ""
    return ("media", path, media_type, caption)


def offers_raw_send(msg, file_exists=os.path.isfile) -> bool:
    """Whether *msg* gets "Send without noise reduction": a voice note kept
    pending after its neural noise reduction failed
    (ui/conversation_panel/voice_nr_send.py) whose kept original is still
    there to send."""
    if not isinstance(msg, dict) or not msg.get("_nr_pending_retry"):
        return False
    request = resend_request(msg, file_exists)
    return request is not None and request[0] == "media"
