"""Pure phone-number and group-participant identity rules.

Moved verbatim out of main.py; main.py re-exports every name.
"""

import logging
from core.utils import parse_bool_flag as _parse_bool_flag


# Fewest digits a value has to carry before it may be read as a phone number
# at all. These helpers are the last gate before deleting the whole local
# history, so anything shorter — a truncated field, a short code, an @lid's
# raw digits — has to read as "no verdict", never as "a different account".
_MIN_COMPARABLE_PHONE_DIGITS = 8


def linked_phone_digits(main_window, linked_value) -> str:
    """Digits of the phone WPPConnect says is linked, or "" when unprovable.

    ``linked_value`` is whatever host-device answered with (see
    MainWindow._host_device_link_probe): a bare digit string, a phone JID in
    either format, possibly with a device suffix, possibly an @lid.

    Everything this cannot positively resolve to a phone number comes back
    empty, because the only caller turns a non-empty answer into permission
    to wipe. In particular an @lid is only accepted once the usual
    _lid_to_phone bridge already holds its phone number: an @lid's own digits
    are not a phone number, and comparing them against a stored one would
    "prove" a difference for every single @lid.

    Two refusals are worth naming because they read like oversights and are
    not; both land on the side that deletes nothing. A value that still
    carries its "+" (or a space, or a dash) does not pass isdigit() and comes
    back empty — only _normalize_jid()'s own output is trusted here, and
    host-device has never been seen to answer in that shape. And
    _MIN_COMPARABLE_PHONE_DIGITS refuses genuinely short international
    numbering too: Saint Helena (+290) and Niue (+683) reach seven digits in
    total, so an account on one of those never arms the comparison at all
    rather than being judged on a value this cannot tell apart from a
    truncated field. Never arming it is the old behaviour — a merge the user
    can still resolve by hand — while getting it wrong is a history nobody
    can get back.
    """
    linked = (linked_value or "").strip() if isinstance(linked_value, str) else ""
    if not linked:
        return ""
    if "@" not in linked:
        linked = f"{linked}@s.whatsapp.net"
    normalized = main_window._normalize_jid(linked)
    if normalized.endswith("@lid"):
        bridged = (getattr(main_window, "_lid_to_phone", {}) or {}).get(normalized, "")
        if not bridged:
            return ""
        normalized = main_window._normalize_jid(bridged)
    if not normalized.endswith("@s.whatsapp.net"):
        # A group, a broadcast, or a shape nobody here understands.
        return ""
    digits = normalized.split("@", 1)[0]
    if not digits.isdigit() or len(digits) < _MIN_COMPARABLE_PHONE_DIGITS:
        return ""
    return digits


def linked_number_differs(stored_digits, linked_digits) -> bool:
    """True only when the linked phone is PROVABLY a different number.

    Both sides are the same kind of value, and that is the whole point: the
    digits WhatsApp itself reported through host-device for the phone it had
    linked, read out by linked_phone_digits(). ``stored_digits`` is what a
    previous run of this check recorded under
    privateinfo["WA_phone_number_linked"]; ``linked_digits`` is what the probe
    just answered.

    Nothing the user typed reaches here any more, and that was the bug. The
    check used to read privateinfo["WA_phone_number"], which connect.py writes
    the moment a pairing code arrives — before the pairing concludes, and with
    nothing restoring the previous value if the attempt is abandoned. So a
    number typed in by mistake, abandoned, and followed by a perfectly correct
    QR pairing of the account's OWN phone read as "another number is linked",
    and the answer to that reading is deleting the user's whole history.

    Equality is MainWindow._phone_digits_equivalent(), the same test the rest
    of the app uses to recognise one person. It only ever widens equality (the
    Brazilian 8/9-digit mobile pair), and widening equality can only make a
    wipe less likely — which is the only direction this function may be wrong
    in. Nothing wider is needed now that both sides come from getWid(): a
    heuristic that guessed at national-prefix variants collapsed genuinely
    different subscribers (measured on +49 211 1234567 vs +49 211 234567, and
    on a Portuguese mobile against a Sofia landline), and the reason it existed
    was comparing a typed number against a reported one.

    Anything less than two recognisable, non-empty numbers is False: no
    recorded number (an install that predates this check, or the normal state
    of a freshly created multi-account entry), or an answer the probe could
    not read.
    """
    stored_raw = stored_digits if isinstance(stored_digits, str) else ""
    stored = "".join(c for c in stored_raw if c.isdigit())
    if len(stored) < _MIN_COMPARABLE_PHONE_DIGITS:
        return False
    if not linked_digits:
        return False
    from main_window.contacts import ContactsMixin
    return not ContactsMixin._phone_digits_equivalent(stored, linked_digits)


def record_linked_phone_if_unknown(main_window, linked_value) -> bool:
    """Record WA_phone_number_linked when this account has none on file yet.

    Write-only, by design: it never compares and never deletes. This is the
    migration half of _wipe_local_data_if_another_number_linked(), which only
    runs on a pairing (_just_paired) or when a mid-session pairing dialog
    closes. So every install that predates this code — and every QR-only one
    — reaches that check for the first time on a pairing, which is precisely
    the moment a different phone may already have linked, and there the empty
    key sends it down the "learn it, delete nothing" branch. The feature
    would arm itself only from the *second* divergent pairing onwards, and it
    is the first one that costs the history.

    Learning the number from an ordinary host-device answer closes that: the
    first launch after the update records the phone WhatsApp says is linked,
    which by construction is the one the local data belongs to. It cannot
    disarm the check either, because it refuses to overwrite an existing
    value — the only thing that rewrites the key is the check itself, after
    the wipe.

    Returns True when settings were changed, so the caller decides when to
    save.
    """
    privateinfo = getattr(main_window, "settings", {}).get("privateinfo")
    if not isinstance(privateinfo, dict):
        return False
    if privateinfo.get("WA_phone_number_linked"):
        return False
    digits = linked_phone_digits(main_window, linked_value)
    if not digits:
        return False
    logging.info(
        "[another_number_check] Recording the phone host-device reports as "
        "linked (...%s) — this account had none on file.", digits[-4:])
    privateinfo["WA_phone_number_linked"] = digits
    return True


def participant_digits(jid) -> str:
    if not isinstance(jid, str):
        return ""
    return jid.rsplit("@", 1)[0].split(":")[0]


def group_participant_is_me(participant, my_phone_digits, my_lid_digits,
                            digits_equivalent) -> bool:
    if not isinstance(participant, dict):
        return False
    p_id = participant.get("id") or ""
    if isinstance(p_id, dict):
        p_id = p_id.get("_serialized", "")
    p_digits = participant_digits(p_id)
    if not p_digits:
        return False
    if my_phone_digits and digits_equivalent(p_digits, my_phone_digits):
        return True
    return bool(my_lid_digits and p_digits == my_lid_digits)


def set_group_participant_admin(participants, is_admin, my_phone_digits,
                                my_lid_digits, digits_equivalent) -> bool:
    if not isinstance(participants, list):
        return False
    for p in participants:
        if not group_participant_is_me(p, my_phone_digits, my_lid_digits,
                                       digits_equivalent):
            continue
        p["admin"] = "admin" if is_admin else None
        p["isAdmin"] = is_admin
        if not is_admin:
            p["isSuperAdmin"] = False
        return True
    return False


def group_participant_admin_flag(participants, my_phone_digits, my_lid_digits,
                                 digits_equivalent) -> "bool | None":
    if not isinstance(participants, list) or not participants:
        return None
    for p in participants:
        if group_participant_is_me(p, my_phone_digits, my_lid_digits,
                                   digits_equivalent):
            return bool(p.get("admin") or p.get("isAdmin") or p.get("isSuperAdmin"))
    return None


def group_send_permission_from_metadata(chat, my_phone_digits, my_lid_digits,
                                        digits_equivalent, known_am_admin=None):
    group_meta = chat.get("groupMetadata")
    if not isinstance(group_meta, dict):
        group_meta = {}
    announce = _parse_bool_flag(group_meta.get("announce"))
    if announce is None:
        announce = _parse_bool_flag(chat.get("announce"))
    if announce is None:
        return None
    participants = group_meta.get("participants") or chat.get("participants") or []
    am_admin = group_participant_admin_flag(
        participants, my_phone_digits, my_lid_digits, digits_equivalent
    )
    if am_admin is None:
        am_admin = known_am_admin
    if announce and am_admin is None:
        return None
    return {"announce": announce, "am_admin": bool(am_admin)}


def unexpired_group_send_verdict(stored, now, max_age_seconds):
    if not isinstance(stored, dict):
        return None
    t = stored.get("t")
    if isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0:
        return None
    if now - t > max_age_seconds:
        return None
    return stored
