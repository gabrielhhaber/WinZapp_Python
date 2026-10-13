"""What counts as a WhatsApp group invitation link.

Pure and import-free on purpose: the process start reads its command line with
this before anything else is loaded (a link handed over by Windows), and the
join dialog, the message links and the IPC listener all ask the same question.
"""

import re

_CODE = r"[A-Za-z0-9]{10,64}"
# The link WhatsApp itself hands out. It often carries a query string
# ("?mode=..."), seen on real messages; nothing after the code is read.
_WEB_LINK = re.compile(r"https://chat\.whatsapp\.com/(?:invite/)?(%s)/?(?:[?#]\S*)?" % _CODE)
# What the "Join chat" button of that web page asks Windows to open, and so
# what WinZapp receives when it is registered for whatsapp: links
# (core/whatsapp_link_association.py).
_APP_LINK = re.compile(r"whatsapp://chat/?\?(?:[^#\s]*&)?code=(%s)(?:[&#]\S*)?" % _CODE)

#: Longer than any real link by a wide margin; a bound for text that arrives
#: from outside the process (command line, IPC).
MAX_LINK_LENGTH = 512


def invitation_code(value):
    """The invitation code of a group link, or "" for anything else.

    A bare code, another host, a longer path or another scheme is not an
    invitation: the code is only ever taken from a link shaped like one."""
    if not isinstance(value, str) or len(value) > MAX_LINK_LENGTH:
        return ""
    text = value.strip()
    match = _WEB_LINK.fullmatch(text) or _APP_LINK.fullmatch(text)
    return match[1] if match else ""


def invitation_link_argument(argv):
    """The group invitation link on a command line, or "".

    Windows starts the registered handler as ``WinZapp.exe "<link>"``; the
    link is whichever argument is one, wherever the other flags sit."""
    for arg in list(argv)[1:]:
        if invitation_code(arg):
            return arg.strip()
    return ""
