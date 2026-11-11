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

import pytest

from core.sync_lifecycle import capture_sync_context
from main import MainWindow
from tests.god_modules import patch_main_global


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
CHANNEL = "120363000000000001@newsletter"
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
    @pytest.mark.parametrize("opened,target", [(False, PHONE), (True, PHONE), (True, CHANNEL)])
    def test_the_phone_request_is_gated_on_it(self, monkeypatch, caplog, opened, target):
        """Local history still runs for an unopened chat, but no phone ask does;
        nor for a channel, whose updates are on the server, not the phone."""
        stub = _Stub()
        if opened:
            stub._note_conversation_opened(target)
        clock = types.SimpleNamespace(now=1.0)
        synced, requested, persisted = [], [], []
        stub._sync_run_id, stub.token = 1, "synthetic:key"
        stub.wpp_server, stub.wpp_port = "http://synthetic.invalid", 6300
        stub._wa_connected = True
        stub._ui_ready_event = types.SimpleNamespace(is_set=lambda: True)
        stub._BACKFILL_BUDGET = stub._BACKFILL_LANDING_BUDGET = 10
        stub._BACKFILL_FIRST_DELAY, stub._BACKFILL_MAX_DELAY = 1, 2
        stub._BACKFILL_CHUNK = stub._BACKFILL_WORKERS = 1
        stub._OLDER_REQUESTS_PER_PASS, stub._PHONE_REQUEST_MIN_GAP = 1, 0
        stub._OLDER_REQUEST_GRACE, stub._MAX_PHONE_HISTORY_REQUESTS = 30, 2
        stub._older_requested_chats, stub._older_request_attempts = {}, {}
        stub._initial_backfill_delay = lambda *a: 1
        stub._collapse_and_list_backfill_pending = lambda: [target]
        stub._pending_name_resolution = stub._chats_needing_deep_history = lambda: []
        stub._voice_call_in_progress = lambda: False
        stub.refresh_history_still_landing = lambda **k: False
        stub._start_deferred_media_sync = lambda: None
        stub._background_backfill_work_allowed = lambda *a: False
        stub._chats_blocking_background_work = MainWindow._chats_blocking_background_work
        stub._resolve_backfill_target = lambda jid: (jid, {"remoteJid": jid})
        stub._local_record_count = lambda jid: 1
        stub.history_page_target = lambda: 200
        stub._oldest_stored_message = lambda jid: {"key": {"id": "oldest"}}
        stub._anchor_identity = lambda message: message["key"]["id"]
        stub._keep_backfill_pending = lambda *a: None
        removed = []
        stub._remove_backfill_pending = removed.append
        stub._persist_backfill_pending_state = stub._persist_history_gap_jids = lambda: None
        stub._persist_older_requested = lambda: persisted.append(dict(stub._older_requested_chats))
        context = capture_sync_context(stub)

        def sync(chat, run, expected_context=None):
            assert run == context.run and expected_context == context
            synced.append(chat["remoteJid"])

        def request(jid, *, outcome_out, expected_context):
            assert expected_context == context
            requested.append(jid)
            return True

        def complete(window):
            # End after this pass without a real wait or another phone request.
            clock.now = 100.0
            return 0

        stub.sync_chat_messages = sync
        stub.request_older_messages = request
        stub._completed_backfill_targets = complete

        class InlinePool:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def submit(self, fn, *args, **kwargs):
                result = fn(*args, **kwargs)
                return types.SimpleNamespace(result=lambda: result)

        patch_main_global(monkeypatch, "ThreadPoolExecutor", InlinePool)
        patch_main_global(monkeypatch, "as_completed", lambda futures: futures)
        patch_main_global(monkeypatch, "time", types.SimpleNamespace(
            monotonic=lambda: clock.now, time=lambda: clock.now,
            sleep=lambda delay: setattr(clock, "now", clock.now + delay)))
        MainWindow._backfill_empty_chats(stub, expected_context=context)
        assert synced == [target]
        asked = opened and target == PHONE
        assert requested == ([target] if asked else [])
        assert stub._older_request_attempts == ({target: 1} if asked else {})
        assert persisted == ([{target: 2.0}] if asked else [])
        assert removed == ([CHANNEL] if target == CHANNEL else [])
        assert not [record for record in caplog.records if record.levelno >= 30]
