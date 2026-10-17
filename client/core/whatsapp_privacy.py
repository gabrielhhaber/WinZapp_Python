"""Rules behind File > WhatsApp settings > Privacy
(ui/dialogs/whatsapp_privacy_panel.py).

These settings live on the WhatsApp account, not in WinZapp: they are read and
written through the local server (main_window/whatsapp_privacy.py, which calls
the ``/privacy`` routes in api_patches). No wx here, so all of it is tested
without opening a window.

Two kinds of setting:

* Six account settings, each one value from a short list (last seen, online,
  About, profile photo, read receipts, who can add me to groups).
* Who can see my Status: my contacts, my contacts except some, or only some.
  The "some" is a list of people picked from the contacts WinZapp already
  synchronised with WhatsApp (ui/dialogs/status_audience_dialog.py).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PrivacyField:
    """One account-wide privacy setting.

    ``setting`` is the name the server and WPP.privacy use; ``options`` pairs
    the raw value WhatsApp expects with the i18n key of the text shown for it.
    """
    setting: str
    label_key: str
    options: tuple


_EVERYONE = ("all", "wa_privacy_opt_all")
_CONTACTS = ("contacts", "wa_privacy_opt_contacts")
_NOBODY = ("none", "wa_privacy_opt_none")

PRIVACY_FIELDS = (
    PrivacyField("lastSeen", "wa_privacy_last_seen_label",
                 (_EVERYONE, _CONTACTS, _NOBODY)),
    PrivacyField("online", "wa_privacy_online_label",
                 (_EVERYONE, ("match_last_seen", "wa_privacy_opt_match_last_seen"))),
    PrivacyField("about", "wa_privacy_about_label",
                 (_EVERYONE, _CONTACTS, _NOBODY)),
    PrivacyField("profilePicture", "wa_privacy_profile_pic_label",
                 (_EVERYONE, _CONTACTS, _NOBODY)),
    PrivacyField("readReceipts", "wa_privacy_read_receipts_label",
                 (("all", "wa_privacy_opt_on"), ("none", "wa_privacy_opt_off"))),
    PrivacyField("groupAdd", "wa_privacy_group_add_label",
                 (_EVERYONE, _CONTACTS)),
)


def option_index(field: PrivacyField, current) -> int:
    """Position of *current* among the field's options, or -1 when WhatsApp
    answered something this list does not know (a value added after this was
    written, or "my contacts except…", which WhatsApp stores as its own value):
    the dropdown is then left unselected instead of guessing."""
    for index, (value, _label_key) in enumerate(field.options):
        if value == current:
            return index
    return -1


def unreadable(settings) -> bool:
    """True when WhatsApp answered but no account setting holds a value this
    dialog knows.

    That is not an error to the server (it said 200), so without this check the
    dialog would show six empty dropdowns and look as if it worked. It happens
    when wa-js can reach the privacy call but not the data behind it, e.g. after
    WhatsApp Web changes an internal module.
    """
    if not isinstance(settings, dict):
        return True
    return all(
        option_index(field, settings.get(field.setting)) == -1
        for field in PRIVACY_FIELDS
    )


def unrecognized_fields(settings: dict) -> list:
    """The fields whose current value has no option here. Their dropdowns stay
    empty and are left alone on apply; the dialog names them so an empty
    dropdown is never mistaken for "not loaded"."""
    return [
        field for field in PRIVACY_FIELDS
        if option_index(field, settings.get(field.setting)) == -1
    ]


def changed_privacy(loaded: dict, chosen: dict) -> list:
    """(setting, value) pairs to send, only for what actually changed.

    *loaded* is what WhatsApp reported; *chosen* maps a setting to the index
    picked in its dropdown (-1 or missing = never set, so left alone). Sending
    only differences keeps the number of calls — and of possible failures — to
    what the person changed.
    """
    changes = []
    for field in PRIVACY_FIELDS:
        index = chosen.get(field.setting, -1)
        if not 0 <= index < len(field.options):
            continue
        value = field.options[index][0]
        if loaded.get(field.setting) != value:
            changes.append((field.setting, value))
    return changes


def not_confirmed(requested: list, readback) -> list:
    """The settings among *requested* that WhatsApp does not report back as set.

    *requested* is the (setting, value) list that was sent, *readback* a fresh
    read of the account settings afterwards. A setter can answer "success" and
    still do nothing (the server only knows the call returned), so the only
    proof is reading the value again. An unreadable *readback* confirms
    nothing: every setting is returned.
    """
    if unreadable(readback):
        return [setting for setting, _value in requested]
    return [
        setting for setting, value in requested
        if readback.get(setting) != value
    ]


# ── Who can see my Status ────────────────────────────────────────────────────

STATUS_CONTACTS = "contact"
STATUS_EXCEPT = "deny-list"
STATUS_ONLY = "allow-list"

STATUS_OPTIONS = (
    (STATUS_CONTACTS, "wa_privacy_opt_contacts"),
    (STATUS_EXCEPT, "wa_privacy_status_opt_except"),
    (STATUS_ONLY, "wa_privacy_status_opt_only"),
)

# Other spellings WhatsApp has used for the same three settings.
_STATUS_ALIASES = {
    "contact": STATUS_CONTACTS,
    "contacts": STATUS_CONTACTS,
    "deny-list": STATUS_EXCEPT,
    "denylist": STATUS_EXCEPT,
    "blacklist": STATUS_EXCEPT,
    "contact_blacklist": STATUS_EXCEPT,
    "allow-list": STATUS_ONLY,
    "allowlist": STATUS_ONLY,
    "whitelist": STATUS_ONLY,
}


@dataclass(frozen=True)
class StatusPrivacy:
    """Who can see my Status, as WhatsApp reported it.

    ``people`` is None when the setting was read but its list was not: the
    list then cannot be shown, and is only ever replaced on purpose.
    """
    setting: str
    people: "tuple | None"


def parse_status_privacy(raw) -> "StatusPrivacy | None":
    """The server's ``status`` answer, or None when it could not be read or
    holds a setting this dialog does not know (the Status choice is then left
    out, and the six account settings still work)."""
    if not isinstance(raw, dict):
        return None
    setting = _STATUS_ALIASES.get(str(raw.get("setting", "")).strip().lower())
    if setting is None:
        return None
    people = raw.get("list")
    if setting == STATUS_CONTACTS:
        return StatusPrivacy(setting, ())
    if not isinstance(people, list):
        return StatusPrivacy(setting, None)
    return StatusPrivacy(setting, tuple(p for p in people if isinstance(p, str) and p))


def status_option_index(setting: str) -> int:
    for index, (value, _label_key) in enumerate(STATUS_OPTIONS):
        if value == setting:
            return index
    return -1


def jid_for_status_list(jid: str) -> str:
    """The id WhatsApp's status list takes for a contact: a phone JID in the
    ``@c.us`` form (WinZapp stores ``@s.whatsapp.net``), an ``@lid`` unchanged,
    anything else (groups, broadcasts, devices) is not a person: ''."""
    jid = (jid or "").strip()
    if "@" not in jid:
        return ""
    local, domain = jid.split("@", 1)
    local = local.split(":", 1)[0]
    if not local.isdigit():
        return ""
    if domain in ("s.whatsapp.net", "c.us"):
        return f"{local}@c.us"
    if domain == "lid":
        return f"{local}@lid"
    return ""


def _br_mobile_variants(digits: str) -> tuple:
    """*digits* and, for a Brazilian mobile, its other 8/9-digit form."""
    if digits.startswith("55"):
        if len(digits) == 13 and digits[4] == "9":
            return digits, digits[:4] + digits[5:]
        if len(digits) == 12:
            return digits, digits[:4] + "9" + digits[4:]
    return (digits,)


def status_list_id(jid: str, phone_to_lid: "dict | None") -> str:
    """The id to put on WhatsApp's status list for *jid*.

    On an account addressed by linked-device ids WhatsApp accepts a phone
    ``@c.us`` entry without error and then ignores it (the same trap
    ``_resolve_jid_for_send`` documents), so a person whose ``@lid`` is known
    (*phone_to_lid*, MainWindow's bridge, tried under both Brazilian 8/9-digit
    forms) goes in as that ``@lid``. Only when none is known does the phone
    form go, and the read-back after applying tells whether it stuck.
    """
    base = jid_for_status_list(jid)
    if not base.endswith("@c.us"):
        return base
    digits = base.split("@", 1)[0]
    for variant in _br_mobile_variants(digits):
        lid = (phone_to_lid or {}).get(f"{variant}@s.whatsapp.net", "")
        lid_digits = lid.split("@", 1)[0].split(":", 1)[0] if lid else ""
        if lid_digits.isdigit():
            return f"{lid_digits}@lid"
    return base


def status_list_confirmed(sent: list, reported: "StatusPrivacy | None",
                          setting: str, same_person) -> bool:
    """True when a fresh read shows WhatsApp holding *setting* with exactly the
    people in *sent*.

    The setter can return and still drop entries (a phone id on an @lid
    account), and only the list read back proves who is in it. *same_person*
    maps an id to the key identifying the person across ``@lid``/phone forms
    (``core.utils.contact_dedup_key``), since WhatsApp may answer with a
    different form than the one sent. A list that could not be read confirms
    nothing.
    """
    if reported is None or reported.setting != setting:
        return False
    if setting == STATUS_CONTACTS:
        return True
    if reported.people is None:
        return False
    return {same_person(j) for j in sent} == {same_person(j) for j in reported.people}


def status_change(loaded: "StatusPrivacy | None", setting: str,
                  people: "list | None", to_id=jid_for_status_list) -> "tuple | None":
    """(setting, list of JIDs) to send, or None when nothing changed.

    *people* is the list chosen in the dialog, or None when the person never
    opened the contact picker — then the list WhatsApp already holds is kept
    (sent again unchanged when only the setting changes). "My contacts" never
    sends a list. *to_id* turns a stored JID into the id the list takes
    (``status_list_id`` with the account's bridge, in the dialog).
    """
    if setting == STATUS_CONTACTS:
        if loaded is not None and loaded.setting == STATUS_CONTACTS:
            return None
        return (STATUS_CONTACTS, [])
    if people is None:
        if loaded is not None and loaded.setting == setting:
            return None
        # Switching setting without picking anyone: carry the current list
        # over only when it is the same kind of list.
        people = []
    wanted = sorted({j for j in (to_id(p) for p in people) if j})
    if loaded is not None and loaded.setting == setting and loaded.people is not None:
        current = sorted({j for j in (to_id(p) for p in loaded.people) if j})
        if current == wanted:
            return None
    return (setting, wanted)


def status_needs_people(change: "tuple | None") -> bool:
    """"Only share with" nobody would hide every Status from everyone: WhatsApp
    itself asks for at least one person, so the dialog does too."""
    return bool(change) and change[0] == STATUS_ONLY and not change[1]


def apply_outcome(changes: list, errors: dict, unconfirmed: list,
                  status: "tuple | None", status_error, status_unconfirmed) -> tuple:
    """What an Apply actually changed: (confirmed settings as {setting: value},
    whether the Status change took effect).

    A setting that failed, or that the read-back did not show, is not
    confirmed: the window keeps its last known value for it, so the next Apply
    tries again instead of believing WhatsApp already has it.
    """
    failed = set(errors) | set(unconfirmed)
    confirmed = {setting: value for setting, value in changes if setting not in failed}
    status_confirmed = status is not None and not status_error and not status_unconfirmed
    return confirmed, status_confirmed


class AudienceSelection:
    """Who is ticked in the Status contact picker, kept by person, outside the
    list: filtering only changes which rows are shown, never who is chosen.

    *rows* are ``(key, label, jid)`` (``status_audience_dialog.audience_rows``);
    *matches(query, label)* decides whether a row fits the search.
    """

    def __init__(self, rows: list, current_keys, matches):
        self.rows = list(rows)
        self._matches = matches
        self.checked = {key for key, _label, _jid in self.rows if key in set(current_keys)}
        self.shown = []  # keys of the rows on screen, in order

    def filter(self, query: str) -> list:
        """(label, checked) for the rows matching *query*; ``shown`` follows."""
        visible = [(key, label) for key, label, _jid in self.rows
                   if self._matches(query, label)]
        self.shown = [key for key, _label in visible]
        return [(label, key in self.checked) for key, label in visible]

    def set_checked(self, index: int, checked: bool) -> bool:
        """Tick or untick the shown row *index*; False when it is not a person
        (the "no contacts" placeholder row)."""
        if not 0 <= index < len(self.shown):
            return False
        if checked:
            self.checked.add(self.shown[index])
        else:
            self.checked.discard(self.shown[index])
        return True

    def chosen(self) -> list:
        return [jid for key, _label, jid in self.rows if key in self.checked]
