"""Keep untrusted message aliases inside the account/peer identity boundary.

Do not ask the mutable LID cache whether a candidate is ours: that same cache
is what message/history metadata is trying to populate. Use explicit account
identity and preserve unresolved peers until an authoritative mapping arrives.
"""
from core.jid_device import strip_device_suffix
from core.phone_variants import other_brazilian_mobile_jid


def _normal(jid):
    if not isinstance(jid, str):
        return ""
    jid = strip_device_suffix(jid)
    if jid.endswith("@c.us"):
        jid = jid[:-5] + "@s.whatsapp.net"
    return jid


def lid_phone_pair_allowed(owner, lid_jid, phone_jid):
    """Whether an untrusted LID/phone pair stays on the same side of self.

    A phone equal to ours is not evidence that an arbitrary peer LID is ours.
    The existing own-identity resolver establishes my_lid separately. Rejected
    aliases must leave messages under their original peer JID, never drop them.
    """
    lid, phone = _normal(lid_jid), _normal(phone_jid)
    if not lid.endswith("@lid") or not phone.endswith("@s.whatsapp.net"):
        return False
    mine = _normal(getattr(owner, "my_jid", ""))
    my_lid = _normal(getattr(owner, "my_lid", ""))
    phone_is_mine = bool(mine) and phone in (mine, other_brazilian_mobile_jid(mine))
    lid_is_mine = bool(my_lid) and lid == my_lid
    if phone_is_mine:
        return lid_is_mine
    if lid_is_mine:
        return False
    return True
