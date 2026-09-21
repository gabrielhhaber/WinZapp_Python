"""Mask WhatsApp phone numbers, LIDs and group ids wherever they appear in a
log line.

WinZapp's diagnostic logging logs a JID directly at well over a hundred call
sites across main.py alone — a JID's leading digits ARE the contact's phone
number (`docs/traps/`'s own JID-handling notes: `@s.whatsapp.net`/`@c.us` are
"the canonical phone JID", `@lid` a "linked-device id"). Editing every one of
those call sites is both impossible to keep complete (they keep being added)
and reopens the leak the moment a new one is. `redact_phone()` is instead
applied once, to the fully rendered message, in `setup_logging()`'s
`logging.Formatter` — see `main.py`. `client/api_patches/src/util/logger.ts`
mirrors this exact regex/approach on the Node side, so log.log and
wppconnect.log redact the same way.

This is the same class of fix as `get_remote_chats()`'s "log shape, not
content" rule (see `tests/test_chat_log_has_no_pii.py`), extended from whole
chat/contact objects to every plain-text log line that mentions a JID
directly. It does NOT redact a bare contact *name* (e.g. "Bruna" logged
without a JID next to it) — free text has no reliable pattern to match, so
that class of leak needs its own fix at its own call site (see
`get_remote_contacts()`'s "First 50 named contacts" line, removed instead of
patched, in the same change that added this module).
"""

import hashlib
import re

# Upper bound is 20, not 15: a phone/LID digit run is at most ~15, but a
# group JID's own id (never a participant's digits — see main.py's JID
# handling notes) runs longer, e.g. "120363067944453158@g.us" (18 digits).
_PHONE_OR_JID = re.compile(
    r"(?<!\d)(\d{8,20})"
    r"(@(?:s\.whatsapp\.net|c\.us|lid|g\.us|broadcast|newsletter))?"
    r"(?!\d)"
)


def redact_phone(text: str) -> str:
    """Mask a WhatsApp phone number, LID or group id anywhere in `text`.

    Each match is replaced with a short, deterministic tag (an md5 prefix of
    the digits, not the number itself), so a log can still show "this is the
    same id three times" without ever spelling out which one. Idempotent:
    the tag itself never matches the digit-run pattern again.
    """
    if not text:
        return text if text is not None else ""

    def _mask(match: "re.Match[str]") -> str:
        digits = match.group(1)
        suffix = match.group(2) or ""
        tag = hashlib.md5(digits.encode("utf-8")).hexdigest()[:8]
        return f"~{tag}{suffix}"

    return _PHONE_OR_JID.sub(_mask, text)
