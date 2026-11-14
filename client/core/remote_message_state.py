"""Identity of account-scoped message-state events, shared by both engines."""
import re
from core.jid_device import strip_device_suffix


def message_target(data):
    if not isinstance(data, dict):
        return None
    jid, mid = data.get("chatId"), data.get("messageId")
    if (not isinstance(jid, str) or len(jid) > 128
            or not re.fullmatch(r"[\w.:-]+@(c\.us|s\.whatsapp\.net|lid|g\.us)", jid)
            or not isinstance(mid, str) or not mid or len(mid) > 128):
        return None
    return strip_device_suffix(jid).replace("@c.us", "@s.whatsapp.net"), mid


def chat_aliases(mw, jid):
    aliases = {jid}
    if jid.endswith("@lid"):
        aliases.add(getattr(mw, "_lid_to_phone", {}).get(jid))
    elif jid.endswith("@s.whatsapp.net"):
        aliases.add(getattr(mw, "_phone_to_lid", {}).get(jid))
    return {strip_device_suffix(j).replace("@c.us", "@s.whatsapp.net") for j in aliases if j}
