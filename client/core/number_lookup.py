"""Ask the engine which address WhatsApp gives a typed phone number.

A typed number may be registered in its other form (Brazilian mobiles with or
without the 9th digit); a chat opened under the typed form is a dead end whose
messages stay "not confirmed". The engine's `check-number-status` answers the
canonical id (WPPConnect and Neonize alike).
"""
import logging
from urllib.parse import quote

from core.api_client import api_get


def _phone_form(serialized) -> str:
    user, _, server = str(serialized or "").partition("@")
    user = user.split(":")[0]
    if not user.isdigit() or server not in ("c.us", "s.whatsapp.net"):
        return ""
    return f"{user}@s.whatsapp.net"


def typed_number_identity(body):
    """`check-number-status` body -> (phone JID, @lid or '') of a number WhatsApp
    knows, ('', '') when it does not or cannot say which number it is.

    WhatsApp Web answers with the person's @lid (2.3000.1052669929, seen over
    CDP on 2026-11-17); WinZapp's WPPConnect side then adds the number the
    @lid belongs to as `phoneNumber`, in WhatsApp's own form: a Brazilian
    number typed with the 9th digit can be registered without it."""
    response = body.get("response") if isinstance(body, dict) else None
    if not isinstance(response, dict) or not response.get("numberExists"):
        return "", ""
    wid = response.get("id")
    serialized = str((wid.get("_serialized") if isinstance(wid, dict) else wid) or "")
    if serialized.endswith("@lid"):
        number = response.get("phoneNumber")
        number = number.get("_serialized") if isinstance(number, dict) else number
        phone = _phone_form(number)
        lid = serialized.split(":")[0].split("@")[0] + "@lid"
        return (phone, lid) if phone and lid[:-4].isdigit() else ("", "")
    return _phone_form(serialized), ""


def resolve_typed_number(mw, digits: str) -> str:
    """The canonical phone JID of `digits`, or '' when the engine cannot say
    (offline, unknown number, error): the caller keeps the typed one. An @lid
    the answer names is bridged to that number (register_jid_mapping), so the
    chat WhatsApp opens under the @lid is this one."""
    url = (f"{mw.wpp_server}:{mw.wpp_port}/api/{mw.token}"
           f"/check-number-status/{quote(digits, safe='')}")
    try:
        response = api_get(url, token=mw.token, timeout=5)
        if response.status_code >= 400:
            return ""
        phone, lid = typed_number_identity(response.json())
    except Exception as error:
        logging.info("[new_conversation] number lookup unavailable: %s", type(error).__name__)
        return ""
    register = getattr(mw, "register_jid_mapping", None)
    if phone and lid and callable(register):
        try:
            register(lid, phone)
        except Exception as error:
            logging.info("[new_conversation] number mapping not kept: %s", type(error).__name__)
    return phone
