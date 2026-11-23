"""Tests for ConversationsPanel._get_participant_name() and _sender_label()'s
inner _contact_name() — regression coverage for the "Unknown User" bug
propagating consistently everywhere a contact name gets resolved.

Found in the same audit as tests/test_bad_contact_name.py: three separate
places in conversations.py each hand-rolled their own copy of "is this name
a placeholder" instead of calling MainWindow._is_bad_contact_name(). Fixing
_is_bad_contact_name() alone (to catch WhatsApp's "Unknown User" username-
feature placeholder) did NOT fix these — they had their own, older,
exact-match-only copy of the same check that still let "Unknown User"
through as if it were a real saved name. All three now call
_is_bad_contact_name() directly instead of re-implementing it.

ConversationsPanel is a wx.Panel and cannot be instantiated without a
running wx.App, so the methods under test are exercised as plain functions
against a small stub — same approach as tests/test_sender_names.py.
"""

from core.utils import format_number
from main import MainWindow
from ui.conversations import ConversationsPanel


class _FakeMainWindow:
    _is_bad_contact_name = staticmethod(MainWindow._is_bad_contact_name)
    # The real lookup, not chats.get(): it falls back to the mapped @lid/phone
    # variant, which is the whole reason the panel calls it. This stub already
    # carries chats/_lid_to_phone/_phone_to_lid, so the real method just works.
    get_chat = MainWindow.get_chat

    def __init__(self, contacts=None, chats=None):
        self.contacts = contacts or {}
        self.chats = chats or {}
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self._presence_pushname_map = {}

    def _is_self_jid(self, jid):
        return False

    def self_reference_label(self):
        return "Eu"

    @staticmethod
    def _normalize_jid(jid):
        return jid


class _PanelStub:
    _get_participant_name = ConversationsPanel._get_participant_name
    _sender_label         = ConversationsPanel._sender_label
    _saved_contact_name  = ConversationsPanel._saved_contact_name

    def __init__(self, main_window):
        self.main_window = main_window
        self._sorted_messages = []
        self._group_participants_cache = []


JID = "5511999999999@s.whatsapp.net"


class TestGetParticipantNameRejectsPlaceholders:
    def test_a_real_saved_name_is_returned(self):
        mw = _FakeMainWindow(contacts={JID: {"name": "Ana Silva"}})
        panel = _PanelStub(mw)
        assert panel._get_participant_name(JID) == "Ana Silva"

    def test_unknown_user_placeholder_is_rejected_and_falls_back_to_phone(self):
        """The exact regression: a contact record whose 'name' is WhatsApp's
        own "Unknown User" username-feature placeholder must not be
        returned as-is."""
        mw = _FakeMainWindow(contacts={JID: {"name": "Unknown User"}})
        panel = _PanelStub(mw)
        result = panel._get_participant_name(JID)
        assert result != "Unknown User"
        assert result == format_number(JID)

    def test_contato_sem_nome_placeholder_is_rejected(self):
        """main.py itself assigns this exact literal string to
        contact["name"] in some code paths — must not be echoed back as a
        real name either."""
        mw = _FakeMainWindow(contacts={JID: {"name": "Contato sem nome"}})
        panel = _PanelStub(mw)
        result = panel._get_participant_name(JID)
        assert result != "Contato sem nome"
        assert result == format_number(JID)

    def test_falls_through_to_chat_name_when_contact_name_is_bad(self):
        mw = _FakeMainWindow(
            contacts={JID: {"name": "Unknown User"}},
            chats={JID: {"name": "Ana (grupo do trabalho)"}},
        )
        panel = _PanelStub(mw)
        assert panel._get_participant_name(JID) == "Ana (grupo do trabalho)"


class TestSenderLabelContactName:
    """_sender_label()'s inner _contact_name() closure — exercised via the
    public _sender_label() entry point since the closure isn't addressable
    on its own."""

    def test_unknown_user_is_rejected_falls_back_to_lookup_jid_formatting(self):
        mw = _FakeMainWindow(contacts={JID: {"name": "Unknown User"}})
        panel = _PanelStub(mw)
        msg = {"key": {"fromMe": False, "participant": JID, "remoteJid": "g@g.us"}}
        label = panel._sender_label(msg)
        assert "Unknown User" not in label


class TestFreshestNameWiring:
    LID = "9@lid"

    def _panel(self):
        mw = _FakeMainWindow(contacts={JID: {"name": "alex oliveira grupos",
                                             "pushName": "alex oliveira grupos"}})
        mw._lid_to_phone = {self.LID: JID}
        mw._phone_to_lid = {JID: self.LID}
        mw._presence_pushname_map = {self.LID: "alex oliveira"}
        mw._pushname_stamps = {self.LID: 200}
        return _PanelStub(mw)

    def test_participant_name_takes_the_name_learned_last_over_a_stale_record(self):
        panel = self._panel()
        assert panel._get_participant_name(JID) == "alex oliveira"

    def test_sender_row_and_quoted_sender_agree(self):
        panel = self._panel()
        msg = {"key": {"remoteJid": "1@g.us", "participant": self.LID, "fromMe": False}}
        assert panel._sender_label(msg) == panel._get_participant_name(self.LID) == "alex oliveira"

    def test_without_a_stamp_the_old_order_is_kept(self):
        panel = self._panel()
        panel.main_window._pushname_stamps = {}
        assert panel._get_participant_name(JID) == "alex oliveira grupos"

    def test_message_scan_fallback_uses_the_newest_message_name(self):
        mw = _FakeMainWindow()
        panel = _PanelStub(mw)
        panel._sorted_messages = [
            {"key": {"participant": "7@s.whatsapp.net"}, "pushName": "nome velho"},
            {"key": {"participant": "7@s.whatsapp.net"}, "pushName": "nome novo"},
        ]
        assert panel._get_participant_name("7@s.whatsapp.net") == "nome novo"
