""""Marcar todas as conversas como lidas": asked for, and actually completed.

Two defects, both seen on one real install with 100+ unread chats:

- **Nothing asked first.** The command is the first item of the Arquivo menu,
  so a stray Alt followed by Enter ran it. 1.2 s after that Alt every unread
  chat was read on WhatsApp too, with no way to undo it. The dialog now shows
  the count and defaults to No.
- **One simultaneous /send-seen per chat.** Every request left in the same
  millisecond; WPPConnect (one WhatsApp Web page) answered each in 3-6 s
  instead of ~250 ms and a batch timed out. run_bulk_read_state() pushes them
  through a small pool and retries in rounds for as long as rounds make
  progress, so hundreds of chats all get marked, and only a WhatsApp that
  stopped answering leaves any behind — those are rolled back and announced.
"""

import threading
import time
from functools import partial
from types import SimpleNamespace

import pytest
import wx

from core.bulk_read_state import run_bulk_read_state
from main import MainWindow
from tests.god_modules import patch_main_global


def _no_sleep(_seconds):
    pass


class TestRunBulkReadState:
    def test_every_chat_is_sent_once_when_all_succeed(self):
        sent = []
        lock = threading.Lock()

        def send(jid):
            with lock:
                sent.append(jid)
            return True

        jids = [f"{n}@s.whatsapp.net" for n in range(300)]
        assert run_bulk_read_state(jids, send, sleep=_no_sleep) == []
        assert sorted(sent) == sorted(jids)

    def test_concurrency_never_exceeds_the_pool(self):
        active = 0
        peak = 0
        lock = threading.Lock()

        def send(_jid):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.005)
            with lock:
                active -= 1
            return True

        jids = [f"{n}@s.whatsapp.net" for n in range(40)]
        run_bulk_read_state(jids, send, max_workers=4, sleep=_no_sleep)
        assert 1 <= peak <= 4

    def test_a_slow_queue_still_completes_however_many_rounds_it_takes(self):
        """Only one chat gets through per round and every chat still ends up
        confirmed, because a round with any success is progress, not
        idleness."""
        jids = [f"{n}@s.whatsapp.net" for n in range(12)]
        done = set()
        lock = threading.Lock()
        round_winner = {"taken": False}

        def send(jid):
            with lock:
                if jid in done:
                    return True
                if not round_winner["taken"]:
                    round_winner["taken"] = True
                    done.add(jid)
                    return True
                return False

        def next_round(_seconds):
            round_winner["taken"] = False

        failed = run_bulk_read_state(
            jids, send, max_workers=1, sleep=next_round
        )
        assert failed == []
        assert done == set(jids)

    def test_a_reload_that_fails_fast_is_ridden_out(self):
        """A WhatsApp Web reload makes every request fail instantly. Many
        quick idle rounds inside the time budget must not give up — the old
        three-round limit rolled back the whole batch within seconds."""
        now = [0.0]
        attempts = {}
        lock = threading.Lock()

        def send(jid):
            with lock:
                attempts[jid] = attempts.get(jid, 0) + 1
                # The page comes back after ~60 s of backoff.
                return now[0] >= 60

        def sleep(seconds):
            now[0] += seconds

        jids = [f"{n}@s.whatsapp.net" for n in range(5)]
        failed = run_bulk_read_state(
            jids, send, idle_timeout=120, sleep=sleep, clock=lambda: now[0]
        )
        assert failed == []
        assert max(attempts.values()) > 3

    def test_gives_up_once_nothing_succeeded_for_the_whole_budget(self):
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        failed = run_bulk_read_state(
            ["ok@s.whatsapp.net", "bad@s.whatsapp.net"],
            lambda jid: jid.startswith("ok"),
            idle_timeout=120, sleep=sleep, clock=lambda: now[0],
        )
        assert failed == ["bad@s.whatsapp.net"]
        assert now[0] >= 120

    def test_a_hung_page_stops_sending_mid_round_when_the_budget_runs_out(self):
        """Each request burns its timeout. Without the per-send check one
        round over every chat would run to completion long past the budget."""
        now = [0.0]
        sent = []

        def send(jid):
            sent.append(jid)
            now[0] += 20  # two 10 s timeouts, one per JID alias
            return False

        jids = [f"{n}@s.whatsapp.net" for n in range(300)]
        failed = run_bulk_read_state(
            jids, send, max_workers=1, idle_timeout=120,
            sleep=_no_sleep, clock=lambda: now[0],
        )
        assert sorted(failed) == sorted(jids)
        assert len(sent) <= 6

    def test_an_exception_fails_that_chat_not_the_run(self):
        def send(jid):
            if jid == "boom@s.whatsapp.net":
                raise RuntimeError("boom")
            return True

        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        failed = run_bulk_read_state(
            ["a@s.whatsapp.net", "boom@s.whatsapp.net"], send,
            sleep=sleep, clock=lambda: now[0],
        )
        assert failed == ["boom@s.whatsapp.net"]

    def test_duplicates_and_blanks_are_ignored(self):
        sent = []
        run_bulk_read_state(
            ["a@s.whatsapp.net", "", "a@s.whatsapp.net"],
            lambda jid: sent.append(jid) or True, sleep=_no_sleep,
        )
        assert sent == ["a@s.whatsapp.net"]


class _I18n:
    def t(self, key):
        return key + "{count}" if key != "menu_mark_all_read" else key


class _Stub:
    _on_mark_all_read = MainWindow._on_mark_all_read
    mark_conversations_as_read = MainWindow.mark_conversations_as_read
    mark_conversation_as_read = MainWindow.mark_conversation_as_read
    _on_bulk_read_failed = MainWindow._on_bulk_read_failed
    _restore_unread_after_send_seen_failure = (
        MainWindow._restore_unread_after_send_seen_failure
    )
    _anchor_unread_to_local_read = MainWindow._anchor_unread_to_local_read
    _drop_unread_local_read_anchor = MainWindow._drop_unread_local_read_anchor
    _normalize_jid = staticmethod(MainWindow._normalize_jid)

    def __init__(self, chats, settings=None):
        self.chats = chats
        self.i18n = _I18n()
        self.announced = []
        self.persists = 0
        self.sent = []
        self.failing = set()
        self.settings = settings if settings is not None else {}
        self.settings_saves = 0

    def save_settings(self):
        self.settings_saves += 1

    def output(self, text, interrupt=False):
        self.announced.append(text)

    def _persist_locally_read_at(self):
        self.persists += 1

    def _schedule_save(self, dirty_jid=None):
        pass

    def _refresh_chat_row_in_list(self, jid):
        pass

    def _schedule_set_chats(self):
        pass

    def _sync_conversation_read_state(self, *a, **kw):
        pytest.fail("bulk path must not start a per-chat /send-seen thread")

    def _send_read_state_blocking(self, jid, unread, attempts=3):
        self.sent.append((jid, unread, attempts))
        return jid not in self.failing


class _InlineThread:
    def __init__(self, target=None, daemon=None, **_kw):
        self._target = target

    def start(self):
        self._target()


@pytest.fixture
def inline(monkeypatch):
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *args: fn(*args))
    # Replace main's *name* for the module, not threading.Thread itself: the
    # pool inside run_bulk_read_state() needs real threads.
    patch_main_global(monkeypatch, "threading", SimpleNamespace(Thread=_InlineThread))
    # sleep/clock are bound as default arguments, so patching time.sleep in
    # the module would not reach them: hand the runner a fake clock that
    # sleep advances, or an unconfirmed chat waits out the real 120 s budget.
    now = [0.0]

    def fake_sleep(seconds):
        now[0] += seconds

    patch_main_global(monkeypatch, "run_bulk_read_state",
        partial(run_bulk_read_state, sleep=fake_sleep, clock=lambda: now[0]),
    )


def _chats():
    return {
        "a@s.whatsapp.net": {"unreadCount": 3, "t": 100},
        "b@s.whatsapp.net": {"unreadCount": 1, "t": 200},
        "c@s.whatsapp.net": {"unreadCount": 0, "t": 300},
    }


def _answer(monkeypatch, confirmed, dont_ask_again=False, seen=None):
    def _confirm(parent, message, title, checkbox_label, **kw):
        if seen is not None:
            seen.update(message=message, title=title, checkbox_label=checkbox_label, **kw)
        return confirmed, dont_ask_again

    patch_main_global(monkeypatch, "confirm_with_checkbox", _confirm)


class TestMarkAllReadConfirmation:
    def test_declining_changes_nothing(self, inline, monkeypatch):
        seen = {}
        _answer(monkeypatch, False, seen=seen)
        stub = _Stub(_chats())
        stub._on_mark_all_read()

        assert stub.chats["a@s.whatsapp.net"]["unreadCount"] == 3
        assert stub.sent == []
        # The count of unread chats is in the question.
        assert seen["message"].endswith("2")
        # Enter on the dialog must not confirm — that is the keystroke that
        # triggered the accident in the first place.
        assert seen["default_yes"] is False
        # "Don't show again" starts unticked.
        assert seen["checked"] is False
        assert seen["checkbox_label"] == "mark_all_read_dont_show_again{count}"

    def test_accepting_marks_only_the_unread_chats(self, inline, monkeypatch):
        _answer(monkeypatch, True)
        stub = _Stub(_chats())
        stub._on_mark_all_read()

        assert all(c["unreadCount"] == 0 for c in stub.chats.values())
        assert sorted(j for j, _, _ in stub.sent) == [
            "a@s.whatsapp.net", "b@s.whatsapp.net"
        ]
        # Rounds do the retrying, so each request is a single attempt.
        assert {attempts for _, _, attempts in stub.sent} == {1}
        assert stub.persists == 1
        assert stub.announced == []
        # Not asked to stop asking: nothing is written.
        assert stub.settings == {}
        assert stub.settings_saves == 0

    def test_nothing_unread_says_so_without_a_dialog(self, inline, monkeypatch):
        patch_main_global(monkeypatch, "confirm_with_checkbox", lambda *a, **k: pytest.fail("asked")
        )
        chats = _chats()
        for chat in chats.values():
            chat["unreadCount"] = 0
        stub = _Stub(chats)
        stub._on_mark_all_read()
        assert stub.announced == ["mark_all_read_none{count}"]


class TestDontShowAgain:
    """The confirmation carries a "don't show again" checkbox; ticked and
    confirmed it clears user_interface.confirm_mark_all_read, the same key
    Settings > Interface mirrors so the confirmation can be turned back on."""

    def test_ticked_and_confirmed_stops_asking_and_persists(self, inline, monkeypatch):
        _answer(monkeypatch, True, dont_ask_again=True)
        stub = _Stub(_chats())

        stub._on_mark_all_read()

        assert stub.settings["user_interface"]["confirm_mark_all_read"] is False
        assert stub.settings_saves == 1
        assert all(c["unreadCount"] == 0 for c in stub.chats.values())

    def test_ticked_but_declined_changes_nothing_at_all(self, inline, monkeypatch):
        """No with the box ticked must not turn every later request into an
        unconfirmed one."""
        _answer(monkeypatch, False, dont_ask_again=True)
        stub = _Stub(_chats())

        stub._on_mark_all_read()

        assert stub.settings == {}
        assert stub.settings_saves == 0
        assert stub.sent == []

    def test_once_turned_off_it_marks_without_asking(self, inline, monkeypatch):
        patch_main_global(monkeypatch, "confirm_with_checkbox", lambda *a, **k: pytest.fail("asked")
        )
        stub = _Stub(_chats(), settings={"user_interface": {"confirm_mark_all_read": False}})

        stub._on_mark_all_read()

        assert all(c["unreadCount"] == 0 for c in stub.chats.values())
        assert stub.settings_saves == 0

    def test_turned_back_on_it_asks_again(self, inline, monkeypatch):
        seen = {}
        _answer(monkeypatch, False, seen=seen)
        stub = _Stub(_chats(), settings={"user_interface": {"confirm_mark_all_read": True}})

        stub._on_mark_all_read()

        assert seen
        assert stub.sent == []

    def test_the_default_is_to_ask(self):
        from core.utils import DEFAULT_SETTINGS

        assert DEFAULT_SETTINGS["user_interface"]["confirm_mark_all_read"] is True


class TestBulkReadFailure:
    def test_unconfirmed_chats_get_their_unread_back_and_are_announced(self, inline):
        stub = _Stub(_chats())
        stub.failing = {"b@s.whatsapp.net"}

        assert stub.mark_conversations_as_read(
            ["a@s.whatsapp.net", "b@s.whatsapp.net"]
        ) == 2

        assert stub.chats["a@s.whatsapp.net"]["unreadCount"] == 0
        assert stub.chats["b@s.whatsapp.net"]["unreadCount"] == 1
        assert stub.announced == ["mark_read_bulk_failed1"]

    def test_force_sends_even_a_chat_already_at_zero(self, inline):
        stub = _Stub(_chats())
        stub.mark_conversations_as_read(["c@s.whatsapp.net"], force=True)
        assert [j for j, _, _ in stub.sent] == ["c@s.whatsapp.net"]

    def test_bulk_rollback_writes_once_and_spares_a_chat_with_new_mail(self, inline):
        stub = _Stub(_chats())
        stub.failing = {"a@s.whatsapp.net", "b@s.whatsapp.net"}
        refreshed = []
        stub._refresh_chat_row_in_list = refreshed.append
        stub._schedule_set_chats = lambda: refreshed.append("rebuild")

        original = stub._on_bulk_read_failed

        def with_new_message(jobs):
            # A message lands in "b" while the batch was still running.
            stub.chats["b@s.whatsapp.net"].update(unreadCount=1, t=201)
            stub._new_since_read["b@s.whatsapp.net"] = 1
            # Only what the rollback itself refreshes is under test.
            refreshed.clear()
            original(jobs)

        stub._on_bulk_read_failed = with_new_message
        stub.persists = 0
        stub.mark_conversations_as_read(["a@s.whatsapp.net", "b@s.whatsapp.net"])

        assert stub.chats["a@s.whatsapp.net"]["unreadCount"] == 3
        assert stub.chats["b@s.whatsapp.net"]["unreadCount"] == 1
        # One persist for the marking, one for the whole rollback.
        assert stub.persists == 2
        assert refreshed == ["rebuild"]

    def test_batched_single_mark_returns_the_job_without_sending(self, inline):
        stub = _Stub(_chats())
        job = stub.mark_conversation_as_read("a@s.whatsapp.net", batched=True)
        assert job == ("a@s.whatsapp.net", 3, 100)
        assert stub.persists == 0
        assert stub.sent == []


class _SenderStub:
    _sync_conversation_read_state = MainWindow._sync_conversation_read_state
    _send_read_state_blocking = MainWindow._send_read_state_blocking
    _normalize_jid = staticmethod(MainWindow._normalize_jid)

    def __init__(self):
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "t"
        self._phone_to_lid = {}
        self._lid_to_phone = {}


class _Resp:
    def __init__(self, ok, data=None):
        self.ok = ok
        self.status_code = 201 if ok else 500
        self._data = data
        self.text = ""

    def json(self):
        return {"response": {"data": self._data}}


class TestSendReadStateBlocking:
    def test_single_attempt_never_sleeps(self, monkeypatch):
        """The bulk path retries through rounds; a per-chat sleep would stall
        a pool worker for nothing."""
        patch_main_global(monkeypatch, "api_post", lambda *a, **k: _Resp(False))
        monkeypatch.setattr("main.time.sleep", lambda s: pytest.fail("slept"))
        assert _SenderStub()._send_read_state_blocking(
            "a@s.whatsapp.net", False, attempts=1
        ) is False

    def test_confirmed_only_when_every_result_is_true(self, monkeypatch):
        patch_main_global(monkeypatch, "api_post", lambda *a, **k: _Resp(True, [True]))
        assert _SenderStub()._send_read_state_blocking(
            "a@s.whatsapp.net", False, attempts=1
        ) is True

    def test_background_sync_reports_failure(self, monkeypatch):
        patch_main_global(monkeypatch, "threading", SimpleNamespace(Thread=_InlineThread))
        stub = _SenderStub()
        stub._send_read_state_blocking = lambda jid, unread: False
        failures = []
        stub._sync_conversation_read_state(
            "a@s.whatsapp.net", False, lambda: failures.append(1)
        )
        assert failures == [1]

    def test_background_sync_reports_failure_when_the_sender_raises(self, monkeypatch):
        patch_main_global(monkeypatch, "threading", SimpleNamespace(Thread=_InlineThread))
        stub = _SenderStub()

        def boom(jid, unread):
            raise KeyError("x")

        stub._send_read_state_blocking = boom
        failures = []
        stub._sync_conversation_read_state(
            "a@s.whatsapp.net", False, lambda: failures.append(1)
        )
        assert failures == [1]
