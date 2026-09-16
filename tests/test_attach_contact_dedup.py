"""Issue #70 (and a follow-up the user flagged manually): the "attach a
contact" picker iterated main_window.contacts directly, but that dict
intentionally holds more than one entry per real person — the same contact
bridged under @lid, @c.us and/or @s.whatsapp.net, and Brazilian numbers
additionally under both their 8- and 9-digit mobile form (see
core.utils.contact_dedup_key()). Every contact showed up twice, once per
JID variant with a different phone-number format, and in dict-insertion
order rather than sorted.

The follow-up: an @lid entry with no bridge to a phone number at all (never
resolved, or only ever learned from a group participant's presence/sender
name — main_window.contacts has no isMyContact/isSaved flag distinguishing
those from a real saved contact) doesn't fold into anything via
contact_dedup_key() and used to get its own row showing raw, unconverted
@lid digits — not exactly a duplicate, but useless junk (format_number()
refuses to format a LID as a phone number, so it can't build a vCard
either) that looked like the same bug. Fixed by reusing the "is this
actually my contact" legitimacy check and additionally excluding any @lid
that contact_dedup_key() couldn't resolve to a phone.

This logic now lives in ui.dialogs.contact_list_picker.build_own_contact_rows,
shared with add_member_dialog.py's contact list (see
test_add_member_contact_filter.py for the legitimacy-filter side of the same
function) so the two pickers can never drift apart again. AttachContactDialog
itself is a wx.Dialog and can't be instantiated without a running wx.App, but
the function under test here never touches wx.
"""

from ui.dialogs.contact_list_picker import build_own_contact_rows


class _FakeMw:
    def __init__(self, contacts=None, lid_to_phone=None, chats=None):
        self.contacts = dict(contacts or {})
        self._lid_to_phone = dict(lid_to_phone or {})
        self.chats = dict(chats or {})

    def _normalize_jid(self, jid):
        if not jid:
            return jid
        if jid.endswith("@c.us"):
            return jid[:-5] + "@s.whatsapp.net"
        return jid


def _contact(name="", is_my_contact=True):
    return {"name": name, "isMyContact": is_my_contact}


class TestAttachContactDedup:
    def test_brazilian_8_vs_9_digit_forms_are_not_duplicated(self):
        mw = _FakeMw(contacts={
            "551199999999@s.whatsapp.net": _contact("Carla"),
            "5511999999999@s.whatsapp.net": _contact("Carla"),
        })

        rows = build_own_contact_rows(mw)

        assert [name for name, _, _ in rows] == ["Carla"]

    def test_lid_and_phone_forms_are_not_duplicated(self):
        lid = "10000000000001@lid"
        phone = "5511999999999@s.whatsapp.net"
        mw = _FakeMw(
            contacts={lid: _contact("Duda"), phone: _contact("Duda")},
            lid_to_phone={lid: phone},
        )

        rows = build_own_contact_rows(mw)

        assert [name for name, _, _ in rows] == ["Duda"]
        # The phone JID is kept (usable for the vCard), not the @lid one.
        assert rows[0][2]["remoteJid"] == phone

    def test_cus_and_net_forms_are_not_duplicated(self):
        mw = _FakeMw(contacts={
            "5511999999999@c.us": _contact("Bia"),
            "5511999999999@s.whatsapp.net": _contact("Bia"),
        })

        rows = build_own_contact_rows(mw)

        assert [name for name, _, _ in rows] == ["Bia"]

    def test_rows_are_sorted_alphabetically_case_insensitively(self):
        mw = _FakeMw(contacts={
            "3@s.whatsapp.net": _contact("carlos"),
            "1@s.whatsapp.net": _contact("Ana"),
            "2@s.whatsapp.net": _contact("Bia"),
        })

        rows = build_own_contact_rows(mw)

        assert [name for name, _, _ in rows] == ["Ana", "Bia", "carlos"]

    def test_groups_are_excluded(self):
        mw = _FakeMw(contacts={
            "120363000000000001@g.us": _contact("Grupo"),
            "1@s.whatsapp.net": _contact("Ana"),
        })

        rows = build_own_contact_rows(mw)

        assert [name for name, _, _ in rows] == ["Ana"]

    def test_unresolved_lid_with_no_phone_bridge_is_excluded(self):
        """An @lid learned only from a group participant's presence, never
        bridged to a real phone number, must not appear at all — it can't
        format as a phone number or build a vCard, and used to show up as
        its own row of raw @lid digits."""
        mw = _FakeMw(contacts={
            "10000000000099@lid": _contact("Fulano"),
            "1@s.whatsapp.net": _contact("Ana"),
        })

        rows = build_own_contact_rows(mw)

        assert [name for name, _, _ in rows] == ["Ana"]

    def test_contact_not_actually_saved_is_excluded(self):
        """A name learned from group presence/sender-name resolution with no
        isMyContact/isSaved flag and no existing 1:1 chat is not a real
        contact the user could plausibly attach or add to a group."""
        mw = _FakeMw(contacts={
            "1@s.whatsapp.net": _contact("Ana", is_my_contact=False),
            "2@s.whatsapp.net": _contact("Bia", is_my_contact=True),
        })

        rows = build_own_contact_rows(mw)

        assert [name for name, _, _ in rows] == ["Bia"]

    def test_contact_with_an_existing_chat_counts_as_legitimate_even_without_the_flag(self):
        jid = "1@s.whatsapp.net"
        mw = _FakeMw(
            contacts={jid: _contact("Ana", is_my_contact=False)},
            chats={jid: {}},
        )

        rows = build_own_contact_rows(mw)

        assert [name for name, _, _ in rows] == ["Ana"]
