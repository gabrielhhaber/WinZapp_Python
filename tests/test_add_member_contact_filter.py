"""Tests for the "own contact" legitimacy filter shared by every contact
picker in WinZapp (ui.dialogs.contact_list_picker.build_own_contact_rows).

Originally these guarded AddMemberDialog._populate_contacts() alone, and
attach_contact_dialog.py kept its own near-identical copy of the same
filter — the two had already drifted (the attach picker deduplicated the
same person appearing under several bridged JIDs, this one didn't). Both
dialogs now build their contact list through the one shared function this
file tests directly.

Reported live: the group-member picker showed contacts main_window.contacts
never actually earned that status for — a JID that only ever appeared there
because on_presence_update()/sender-name learning wrote {name, pushName} for
someone who spoke in some OTHER group. Those entries carry no isMyContact/
isSaved flag and aren't backed by a 1:1 chat, so they now get filtered out;
only genuine WhatsApp contacts (isMyContact), locally-added ones (isSaved),
"me", and anyone with an existing 1:1 chat still show up.

The function under test never touches wx, so no wx.App/ListCtrl stub is
needed here (contrast test_attach_contact_dedup.py's own docstring, which
predates this — both now exercise the same shared function).
"""

from ui.dialogs.contact_list_picker import build_own_contact_rows


class _FakeMw:
    def __init__(self, contacts=None, chats=None):
        self.contacts = dict(contacts or {})
        self.chats = dict(chats or {})

    def _normalize_jid(self, jid):
        return jid


def _jids(rows):
    return [entry["remoteJid"] for _, _, entry in rows]


class TestAddMemberContactFilter:
    def test_group_participant_only_entry_is_excluded(self):
        """No isMyContact/isSaved, no 1:1 chat — just a name learned from
        some other group's presence updates."""
        jid = "5511999999999@s.whatsapp.net"
        mw = _FakeMw({jid: {"name": "Alice", "pushName": "Alice"}})

        rows = build_own_contact_rows(mw)

        assert _jids(rows) == []

    def test_genuine_whatsapp_contact_is_included(self):
        jid = "5511999999999@s.whatsapp.net"
        mw = _FakeMw({jid: {"name": "Alice", "isMyContact": True}})

        rows = build_own_contact_rows(mw)

        assert _jids(rows) == [jid]

    def test_locally_added_contact_is_included(self):
        jid = "5511999999999@s.whatsapp.net"
        mw = _FakeMw({jid: {"name": "Alice", "isSaved": True}})

        rows = build_own_contact_rows(mw)

        assert _jids(rows) == [jid]

    def test_contact_with_an_existing_1to1_chat_is_included(self):
        """Someone who messaged first without being in the user's own
        address book — WhatsApp may never flag them isMyContact, but the
        user clearly already has a real conversation with them."""
        jid = "5511999999999@s.whatsapp.net"
        mw = _FakeMw(
            {jid: {"name": "Alice"}},
            chats={jid: {"remoteJid": jid}},
        )

        rows = build_own_contact_rows(mw)

        assert _jids(rows) == [jid]

    def test_groups_are_always_excluded_regardless_of_flags(self):
        jid = "123456789-987654321@g.us"
        mw = _FakeMw({jid: {"name": "Some Group", "isMyContact": True}})

        rows = build_own_contact_rows(mw)

        assert _jids(rows) == []

    def test_mixed_list_keeps_only_the_legitimate_ones(self):
        real = "5511111111111@s.whatsapp.net"
        leaked = "5522222222222@s.whatsapp.net"
        mw = _FakeMw({
            real:   {"name": "Real Contact", "isMyContact": True},
            leaked: {"name": "Leaked From Another Group"},
        })

        rows = build_own_contact_rows(mw)

        assert _jids(rows) == [real]
