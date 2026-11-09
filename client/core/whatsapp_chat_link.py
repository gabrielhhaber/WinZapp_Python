"""What counts as a WhatsApp link that opens a chat with a number.

Pure and import-free on purpose, like core/group_invitation_link.py: the
process start reads its command line with this before anything else loads.

Three families of link carry a phone number and an optional pre-filled text:

* ``whatsapp://send?phone=<number>&text=<text>`` -- what Windows hands to a
  program registered for ``whatsapp:`` (core/whatsapp_link_association.py),
  and what the web pages below ask the system to open;
* ``https://wa.me/<number>?text=<text>``;
* ``https://api.whatsapp.com/send/?phone=<number>&text=<text>`` (also
  ``web.whatsapp.com/send``).

The https forms are matched too for the places that pass them on as text (a
command line, another program), but a browser never gives them to WinZapp:
Windows leaves web links to packaged apps. The browser lands on the page,
whose button opens the ``whatsapp://`` form.
"""

import re
from urllib.parse import parse_qs, unquote, urlsplit

from core.group_invitation_link import invitation_code

#: Longer than a pre-filled message needs to be; a bound for text that comes
#: from outside the process (command line, IPC). Windows itself caps a
#: command line at 32,767 characters.
MAX_CHAT_LINK_LENGTH = 4096
#: E.164 allows 15 digits; fewer than 7 is not a number.
_MIN_DIGITS = 7
_MAX_DIGITS = 15
_MAX_TEXT = 4000

_HTTPS_SEND_HOSTS = {"api.whatsapp.com", "web.whatsapp.com", "whatsapp.com", "www.whatsapp.com"}


def _phone(value):
    digits = re.sub(r"\D", "", unquote(value or ""))
    return digits if _MIN_DIGITS <= len(digits) <= _MAX_DIGITS else ""


def chat_target(value):
    """``(phone_digits, text)`` for a chat link, or ``None`` for anything else.

    *text* is "" when the link carries none. A link without a usable phone
    number (a ``wa.me/message/...`` business link, a bare ``whatsapp://send``)
    is not a chat link."""
    if not isinstance(value, str) or len(value) > MAX_CHAT_LINK_LENGTH:
        return None
    text = value.strip()
    if not text or any(ch in text for ch in "\r\n\x00"):
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower() if scheme in ("http", "https") else ""
    query = parse_qs(parts.query, keep_blank_values=True)
    phone = ""
    if scheme == "whatsapp":
        if parts.netloc.lower() != "send" or parts.path not in ("", "/"):
            return None
        phone = _phone((query.get("phone") or [""])[0])
    elif scheme == "https" and host == "wa.me":
        path = parts.path.strip("/")
        if "/" in path:
            return None
        phone = _phone(path)
    elif scheme == "https" and host in _HTTPS_SEND_HOSTS:
        if parts.path.rstrip("/") != "/send":
            return None
        phone = _phone((query.get("phone") or [""])[0])
    if not phone:
        return None
    body = (query.get("text") or [""])[0][:_MAX_TEXT]
    return phone, body


def is_whatsapp_link(value) -> bool:
    """A link WinZapp knows how to open: a group invitation or a chat."""
    return bool(invitation_code(value)) or chat_target(value) is not None


def whatsapp_link_argument(argv):
    """The first group invitation or chat link on a command line, or ""."""
    for arg in list(argv)[1:]:
        if is_whatsapp_link(arg):
            return arg.strip()
    return ""
