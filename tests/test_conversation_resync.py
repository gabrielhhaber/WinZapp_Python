"""Shift+F5: resync only the open conversation.

F5 wipes every chat and syncs from scratch. For one conversation the same wipe
would lose history older than the single page get-messages returns, and leave
the conversation empty whenever that request failed. So Shift+F5 fetches
first, through sync_chat_messages(), and then removes only the local rows the
server contradicts -- inside the window it answered for, never local-only
records, and nothing at all while WhatsApp Web's store is known to be behind
the database after a profile restore.

MainWindow needs a wx.App, so the handler and worker run against a stub; the
menu and the sync hook are checked structurally.
"""

import inspect
import threading

import pytest

import main
from core.conversation_resync import stale_ids_in_fetched_window
from main import MainWindow

JID = "5511999999999@s.whatsapp.net"


def _msg(mid, ts, **extra):
    record = {"key": {"remoteJid": JID, "fromMe": False, "id": mid},
              "messageType": "conversation", "message": {"conversation": mid},
              "messageTimestamp": ts}
    record.update(extra)
    return record


class TestWhatTheServerContradicts:
    def test_a_row_inside_the_window_the_server_did_not_return_is_stale(self):
        records = [_msg("A", 100), _msg("GHOST", 150), _msg("B", 200)]
        assert stale_ids_in_fetched_window(records, {"A", "B"}) == ["GHOST"]

    def test_history_older_than_the_window_is_kept(self):
        records = [_msg("OLD", 50), _msg("A", 100), _msg("B", 200)]
        assert stale_ids_in_fetched_window(records, {"A", "B"}) == []

    def test_a_message_newer_than_the_window_is_kept(self):
        """Arrived live while the request was in flight."""
        records = [_msg("A", 100), _msg("B", 200), _msg("LIVE", 250)]
        assert stale_ids_in_fetched_window(records, {"A", "B"}) == []

    def test_local_only_records_are_never_stale(self):
        records = [
            _msg("A", 100),
            _msg("SENDING", 150, _local_pending=True),
            _msg("FAILED", 160, _send_failed=True),
            _msg("CANCELLED", 170, _cancelled_awaiting_id=True),
            _msg("B", 200),
        ]
        assert stale_ids_in_fetched_window(records, {"A", "B"}) == []

    def test_nothing_fetched_removes_nothing(self):
        records = [_msg("A", 100), _msg("B", 200)]
        assert stale_ids_in_fetched_window(records, set()) == []
        assert stale_ids_in_fetched_window(records, None) == []

    def test_millisecond_timestamps_are_compared_as_seconds(self):
        records = [_msg("A", 1_790_000_100), _msg("GHOST", 1_790_000_150_000),
                   _msg("B", 1_790_000_200)]
        assert stale_ids_in_fetched_window(records, {"A", "B"}) == ["GHOST"]

    def test_a_record_without_an_id_is_left_alone(self):
        orphan = _msg("", 150)
        records = [_msg("A", 100), orphan, _msg("B", 200)]
        assert stale_ids_in_fetched_window(records, {"A", "B"}) == []


# ── The worker, on a stub ─────────────────────────────────────────────────────


class _Db:
    def __init__(self):
        self.deleted = []

    def delete_message(self, remote_jid, message_id):
        self.deleted.append((remote_jid, message_id))


class _I18n:
    def t(self, key):
        return key


class _Window:
    _resync_conversation_worker = MainWindow._resync_conversation_worker

    def __init__(self, records, fetched, ok=True):
        self.chats = {JID: {"remoteJid": JID, "messages": {"messages": {
            "total": len(records), "records": records}}}}
        self._fetched = fetched
        self._ok = ok
        self._resyncing_conversations = {JID}
        self.db = _Db()
        self.i18n = _I18n()
        self.spoken = []
        self.refreshed = []
        self.sync_calls = []

    def sync_chat_messages(self, chat, expected_run_id=None, sync_mode="full",
                           fetched_ids_out=None):
        self.sync_calls.append(sync_mode)
        if fetched_ids_out is not None:
            fetched_ids_out.update(self._fetched)
        return self._ok

    def _refresh_open_conversation_after_sync(self, remote_jid, chat):
        self.refreshed.append(remote_jid)

    def _schedule_set_chats(self):
        pass

    def output(self, text, interrupt=False):
        self.spoken.append(text)


@pytest.fixture(autouse=True)
def _call_after_inline(monkeypatch):
    monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))


class TestTheWorker:
    def test_it_asks_for_a_full_page_and_removes_the_stale_row(self):
        records = [_msg("A", 100), _msg("GHOST", 150), _msg("B", 200)]
        window = _Window(records, {"A", "B"})

        window._resync_conversation_worker(JID)

        assert window.sync_calls == ["full"]
        assert [r["key"]["id"] for r in records] == ["A", "B"]
        assert window.chats[JID]["messages"]["messages"]["total"] == 2
        assert window.db.deleted == [(JID, "GHOST")]
        assert window.refreshed == [JID]
        assert window.spoken == ["resync_conversation_done"]

    def test_a_failed_fetch_changes_nothing_and_says_so(self):
        records = [_msg("A", 100), _msg("GHOST", 150), _msg("B", 200)]
        window = _Window(records, set(), ok=False)

        window._resync_conversation_worker(JID)

        assert len(records) == 3
        assert window.db.deleted == []
        assert window.spoken == ["resync_conversation_failed"]

    def test_an_answer_with_no_messages_is_not_a_success(self):
        """chat_not_found returns True from sync_chat_messages() -- with
        nothing fetched, which is no reason to call the conversation synced."""
        window = _Window([_msg("A", 100)], set(), ok=True)

        window._resync_conversation_worker(JID)

        assert window.spoken == ["resync_conversation_failed"]

    def test_after_a_profile_restore_nothing_is_removed(self):
        records = [_msg("A", 100), _msg("GHOST", 150), _msg("B", 200)]
        window = _Window(records, {"A", "B"})
        window._remote_deletions_untrusted = True

        window._resync_conversation_worker(JID)

        assert len(records) == 3
        assert window.db.deleted == []
        assert window.spoken == ["resync_conversation_done"]

    def test_the_conversation_can_be_resynced_again_afterwards(self):
        window = _Window([_msg("A", 100)], {"A"})

        window._resync_conversation_worker(JID)

        assert window._resyncing_conversations == set()

    def test_a_crash_is_spoken_and_releases_the_conversation(self):
        window = _Window([_msg("A", 100)], {"A"})

        def _boom(*_a, **_k):
            raise RuntimeError("boom")
        window.sync_chat_messages = _boom

        window._resync_conversation_worker(JID)

        assert window.spoken == ["resync_conversation_failed"]
        assert window._resyncing_conversations == set()


# ── The handler, on a stub ────────────────────────────────────────────────────


class _Panel:
    def __init__(self, conversation):
        self.conversation = conversation


class _Handler:
    _on_menu_resync_conversation = MainWindow._on_menu_resync_conversation
    _normalize_jid = staticmethod(MainWindow._normalize_jid)

    def __init__(self, conversation=None, connected=True):
        self.conversations_panel = _Panel(conversation)
        self._wa_connected = connected
        self.i18n = _I18n()
        self.spoken = []
        self.started = []
        self.error_sound = type("S", (), {"play": lambda self: None})()

    def check_wa_connection_http(self):
        pass

    def output(self, text, interrupt=False):
        self.spoken.append(text)

    def _resync_conversation_worker(self, remote_jid):
        self.started.append(remote_jid)


@pytest.fixture
def _inline_threads(monkeypatch):
    class _Inline:
        def __init__(self, target=None, args=(), **_kw):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)
    monkeypatch.setattr(main.threading, "Thread", _Inline)


class TestTheHandler:
    def test_without_an_open_conversation_it_says_so(self, _inline_threads):
        handler = _Handler()

        handler._on_menu_resync_conversation()

        assert handler.spoken == ["resync_conversation_none_open"]
        assert handler.started == []

    def test_it_announces_and_starts_the_worker(self, _inline_threads):
        handler = _Handler({"remoteJid": JID})

        handler._on_menu_resync_conversation()

        assert handler.spoken == ["resyncing_conversation_announcement"]
        assert handler.started == [JID]
        assert JID in handler._resyncing_conversations

    def test_a_second_press_while_it_runs_is_refused_out_loud(self, _inline_threads):
        handler = _Handler({"remoteJid": JID})
        handler._resyncing_conversations = {JID}

        handler._on_menu_resync_conversation()

        assert handler.spoken == ["resync_conversation_busy"]
        assert handler.started == []

    def test_the_full_sync_running_is_refused_out_loud(self, _inline_threads):
        handler = _Handler({"remoteJid": JID})
        handler._initial_sync_running = True

        handler._on_menu_resync_conversation()

        assert handler.spoken == ["resync_conversation_busy"]
        assert handler.started == []

    def test_offline_it_warns_like_f5(self, _inline_threads, monkeypatch):
        boxes = []
        monkeypatch.setattr(main.wx, "MessageBox", lambda *a, **k: boxes.append(a[0]))
        handler = _Handler({"remoteJid": JID}, connected=False)

        handler._on_menu_resync_conversation()

        assert boxes == ["resync_failed_offline"]
        assert handler.started == []


# ── Wiring ────────────────────────────────────────────────────────────────────


class TestTheWiring:
    def test_shift_f5_is_the_menu_accelerator_on_build_and_relabel(self):
        src = inspect.getsource(main)
        assert src.count("{self.i18n.t('menu_resync_conversation')}\\tShift+F5") == 2
        assert ("self.Bind(wx.EVT_MENU, self._on_menu_resync_conversation,\n"
                "                  id=self._ID_RESYNC_CONVERSATION)") in src

    def test_the_sync_reports_the_fetched_ids_before_merging_local_rows(self):
        src = inspect.getsource(MainWindow.sync_chat_messages)
        hook = src.index("fetched_ids_out.update(")
        assert src.index("matching_messages.append(message)") < hook
        assert hook < src.index("local_records = (local_chat.get(")
        assert "if fetched_ids_out is not None and api_ok:" in src
