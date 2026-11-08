"""Learn chat-list message aliases without crossing the account boundary."""
from core.jid_alias_guard import lid_phone_pair_allowed


def learn_snapshot_alias(owner, key):
    """Keep the existing mapping lock and follow contacts outside that lock."""
    remote = owner._normalize_jid(key.get("remoteJid", ""))
    alt = owner._normalize_jid(key.get("remoteJidAlt", ""))
    if remote.endswith("@lid") and alt.endswith("@s.whatsapp.net"):
        lid, phone = remote, alt
    elif alt.endswith("@lid") and remote.endswith("@s.whatsapp.net"):
        lid, phone = alt, remote
    else:
        return False
    if not lid_phone_pair_allowed(owner, lid, phone):
        return False
    learned = False
    with owner._lid_mapping_lock:
        # Account identity may have become available while waiting for the lock.
        if not lid_phone_pair_allowed(owner, lid, phone):
            return False
        if not hasattr(owner, "_lid_to_phone"):
            owner._lid_to_phone = {}
        if not hasattr(owner, "_phone_to_lid"):
            owner._phone_to_lid = {}
        if owner._lid_to_phone.get(lid) != phone:
            owner._lid_to_phone[lid] = phone
            owner._phone_to_lid[phone] = lid
            learned = True
    if learned:
        owner._follow_saved_contacts([(lid, phone)])
    return learned
