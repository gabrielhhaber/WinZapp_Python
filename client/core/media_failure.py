"""Why a media download failed, when the engine said so (issue #766).

A media whose link expired is asked again from the phone (Neonize engine,
neonize_bridge/media_retry.py). That can fail in three ways the user can act
on differently, and the bridge answers each with a `code`:

- the phone did not answer in time: try again (the phone may be busy);
- the phone sent the file again but it could not be opened;
- the phone no longer has the file.

Every one of them used to end in the same "the link may have expired or the
WhatsApp connection may be unavailable", even with the phone connected and
answering. The download (main_window/media.py) notes the code by message id;
the window that tells the user picks the matching sentence with
`failure_key()`. An answer without a code (WPPConnect, any other fault)
keeps the old sentence.
"""

import json
import threading
from collections import OrderedDict

GENERIC_KEY = "media_download_failed"
REASON_KEYS = {
    "media_retry_no_answer": "media_download_phone_no_answer",
    "media_retry_unreadable": "media_download_retried_unreadable",
    "media_gone_from_phone": "media_download_gone_from_phone",
}
#: a few failed downloads at once at most; old notes are dropped
_KEEP = 64

_reasons = OrderedDict()
_lock = threading.Lock()


def code_of(body) -> str:
    """The bridge's `code` in an error answer's body, or ""."""
    try:
        answer = json.loads(body) if isinstance(body, (str, bytes)) else body
    except (TypeError, ValueError):
        return ""
    code = answer.get("code") if isinstance(answer, dict) else ""
    return code if code in REASON_KEYS else ""


def note(message_id, body) -> None:
    """Remember why *message_id*'s download failed (or forget a stale reason)."""
    if not message_id:
        return
    code = code_of(body)
    with _lock:
        _reasons.pop(message_id, None)
        if code:
            _reasons[message_id] = code
            while len(_reasons) > _KEEP:
                _reasons.popitem(last=False)


def failure_key(msg) -> str:
    """The i18n key saying why *msg*'s media could not be downloaded; the
    reason is used once."""
    message_id = ""
    if isinstance(msg, dict):
        message_id = (msg.get("key") or {}).get("id", "") or msg.get("id", "")
    with _lock:
        code = _reasons.pop(message_id, "") if message_id else ""
    return REASON_KEYS.get(code, GENERIC_KEY)
