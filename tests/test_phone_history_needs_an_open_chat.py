"""Who gets to put a notification on the user's phone.

Everything the backfill does is local and free except one step: asking the
phone for older history. That request lights up the user's own lock screen,
and when the phone cannot satisfy it the follow-up reads "Sync paused. Open
WhatsApp to resume." — an error, for a conversation they never opened.

Measured on the reporting install after the earlier rate and per-chat bounds
were already shipped: 82 chats short of the 200-message target, one phone
request every two minutes, marching through the account for hours. The bounds
held; the queue was simply the whole address book. The user's own summary,
twice: it should be following new messages, not fetching old history for other
conversations in the background.

So opening a conversation is the gate. It is the signal that its history is
worth something, and the moment a notification about it is welcome. Scrolling
up (fetch_older_messages) is unaffected and always was — that request is
attended by definition.
"""

import types

from main import MainWindow


class _Stub:
    _note_conversation_opened = MainWindow._note_conversation_opened
    _user_has_opened = MainWindow._user_has_opened
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _jid_address_forms = MainWindow._jid_address_forms
    _canonical_backfill_jid = MainWindow._canonical_backfill_jid

    def __init__(self):
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self.db = types.SimpleNamespace(
            set_metadata_json=lambda k, v: self.persisted.update({k: v}),
            get_metadata_json=lambda k, d=None: self.persisted.get(k, d),
        )
        self.persisted = {}


PHONE = "5511900000000@s.whatsapp.net"
LID = "123456789012345@lid"


class TestTheGate:
    def test_a_chat_never_opened_is_refused(self):
        assert _Stub()._user_has_opened(PHONE) is False

    def test_opening_it_opens_the_gate(self):
        stub = _Stub()
        stub._note_conversation_opened(PHONE)
        assert stub._user_has_opened(PHONE) is True

    def test_opening_one_chat_does_not_open_another(self):
        stub = _Stub()
        stub._note_conversation_opened(PHONE)
        assert stub._user_has_opened("5511911111111@s.whatsapp.net") is False

    def test_it_is_recognised_under_the_other_address_form(self):
        """The backfill queue keys chats by @lid while the UI opens them under
        the phone JID, and vice versa — one bridge, two names for one chat."""
        stub = _Stub()
        stub._lid_to_phone = {LID: PHONE}
        stub._phone_to_lid = {PHONE: LID}
        stub._note_conversation_opened(PHONE)
        assert stub._user_has_opened(LID) is True

    def test_the_legacy_c_us_form_resolves_too(self):
        stub = _Stub()
        stub._note_conversation_opened("5511900000000@c.us")
        assert stub._user_has_opened(PHONE) is True

    def test_a_blank_jid_records_nothing(self):
        stub = _Stub()
        stub._note_conversation_opened("")
        stub._note_conversation_opened(None)
        assert stub._user_has_opened(PHONE) is False


class TestItSurvivesARestart:
    def test_the_open_is_persisted(self):
        stub = _Stub()
        stub._note_conversation_opened(PHONE)
        assert PHONE in stub.persisted["opened_conversations_v1"]

    def test_reopening_the_same_chat_does_not_rewrite_it(self):
        stub = _Stub()
        stub._note_conversation_opened(PHONE)
        writes = []
        stub.db.set_metadata_json = lambda k, v: writes.append(k)
        stub._note_conversation_opened(PHONE)
        assert writes == []

    def test_a_database_that_refuses_the_write_is_not_fatal(self):
        """The gate still opens for this session; losing it costs one chat's
        history backfill after a restart, never a message."""
        stub = _Stub()

        def _boom(_k, _v):
            raise OSError("disk full")

        stub.db.set_metadata_json = _boom
        stub._note_conversation_opened(PHONE)
        assert stub._user_has_opened(PHONE) is True


class TestTheBackfillHonoursIt:
    def test_the_phone_request_is_gated_on_it(self):
        import inspect
        src = inspect.getsource(MainWindow._backfill_empty_chats)
        assert "_user_has_opened" in src
        # ...and before the request is built, not after it went out.
        assert src.index("_user_has_opened") < src.index("request_older_messages(jid)")
