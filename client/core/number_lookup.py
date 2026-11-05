"""Ask the engine which address WhatsApp gives a typed phone number.

A typed number may be registered in its other form (Brazilian mobiles with or
without the 9th digit); a chat opened under the typed form is a dead end whose
messages stay "not confirmed". The engine's `check-number-status` answers the
canonical id (WPPConnect and Neonize alike).
"""
import logging
from urllib.parse import quote

from core.api_client import api_get


def canonical_jid(body) -> str:
    """`check-number-status` body -> 'digits@s.whatsapp.net' for a number
    WhatsApp knows by a phone address, '' when it does not or answers a LID."""
    response = body.get("response") if isinstance(body, dict) else None
    if not isinstance(response, dict) or not response.get("numberExists"):
        return ""
    serialized = str(((response.get("id") or {}).get("_serialized")) or "")
    user, _, server = serialized.partition("@")
    user = user.split(":")[0]
    if not user.isdigit() or server not in ("c.us", "s.whatsapp.net"):
        return ""
    return f"{user}@s.whatsapp.net"


def resolve_typed_number(mw, digits: str) -> str:
    """The canonical phone JID of `digits`, or '' when the engine cannot say
    (offline, unknown number, error): the caller keeps the typed one."""
    url = (f"{mw.wpp_server}:{mw.wpp_port}/api/{mw.token}"
           f"/check-number-status/{quote(digits, safe='')}")
    try:
        response = api_get(url, token=mw.token, timeout=5)
        if response.status_code >= 400:
            return ""
        return canonical_jid(response.json())
    except Exception as error:
        logging.info("[new_conversation] number lookup unavailable: %s", type(error).__name__)
        return ""
