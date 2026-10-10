"""post_ui_init's worker thread never calls sys.exit().

post_ui_init runs on a worker thread once the main window is up. Three of its
"cannot go on" exits called sys.exit() there — which, off the main thread, only
raises SystemExit in that thread. The thread died, the window stayed up
connected to nothing, and nothing told the user:

* STEP 1b: the pairing dialog it reopens was closed without pairing;
* STEP 2: retrieve_token() found no token (after showing its error box from
  the worker, off the UI thread);
* STEP 2: self.token still empty after retrieve_token().

The first two now queue real_exit() (and, for the token, the same error box)
on the UI thread, as _confirm_pairing_behind_window() does on the start behind
the window. The third can now only be a logout handled on the UI thread in
between, which opens the pairing dialog itself, so there the thread just stops
and leaves that flow in charge. retrieve_token()'s call from __init__ runs on
the main thread before the main loop, where sys.exit() does end the process,
and is kept as it was.

_recheck_pairing_after_ui() is STEP 1, lifted out of the _post_ui_init closure
so it can be driven on a stub; the closure's own wiring is checked at source
level, like the rest of _post_ui_init (test_api_start_behind_window.py).
"""

import inspect

import pytest

from main import MainWindow
from main_window import session_tokens, window_chrome
from tests.god_modules import patch_main_global


class _Connect:
    def __init__(self, answers):
        self._answers = list(answers)
        self.dialogs = 0

    def check_connection_status(self):
        return self._answers.pop(0)

    def show_connection_dial(self):
        self.dialogs += 1


class _Stub:
    _recheck_pairing_after_ui = MainWindow._recheck_pairing_after_ui

    def __init__(self, answers):
        self.connect = _Connect(answers)
        self.events = []
        self._just_paired = False

    def real_exit(self):
        self.events.append("exit")

    def _offer_switch_when_unpaired(self):
        self.events.append("offer")
        return False


@pytest.fixture
def call_after(monkeypatch):
    """Records what is queued instead of running it, so a test can tell
    "queued for the UI thread" from "ran inline on the worker"."""
    queued = []
    monkeypatch.setattr(window_chrome.wx, "CallAfter",
                        lambda fn, *a, **k: queued.append((fn, a)))
    return queued


class TestRecheckPairingAfterUi:
    def test_a_paired_session_goes_straight_on(self, call_after):
        stub = _Stub([True])
        assert stub._recheck_pairing_after_ui() is True
        assert stub.connect.dialogs == 0
        assert call_after == [] and stub.events == []
        assert stub._just_paired is False

    def test_pairing_through_the_dialog_goes_on_as_just_paired(self, call_after):
        stub = _Stub([False, True])
        assert stub._recheck_pairing_after_ui() is True
        assert stub.connect.dialogs == 1
        assert stub._just_paired is True
        assert call_after == [] and stub.events == []

    def test_closing_the_dialog_unpaired_queues_a_full_quit(self, call_after):
        stub = _Stub([False, False])
        # Must not raise SystemExit: on post_ui_init's thread that ended only
        # the thread and left the window up.
        assert stub._recheck_pairing_after_ui() is False
        assert stub.connect.dialogs == 1
        assert stub._just_paired is False
        # real_exit() runs on the UI thread, not inline on the worker.
        assert stub.events == []
        assert call_after == [(stub.real_exit, ())]

    def test_no_switch_account_offer_is_added(self, call_after):
        """Minimal fix: this step never offered to switch accounts, and still
        does not — only the start behind the window carries that over from
        __init__."""
        stub = _Stub([False, False])
        stub._recheck_pairing_after_ui()
        assert "offer" not in stub.events


class _I18n:
    def t(self, key):
        return {"token_retrieval_failed": "No token.",
                "error": "{app_name} error"}[key]


class _Sound:
    def __init__(self, events):
        self._events = events

    def play(self):
        self._events.append("sound")


class _TokenStub:
    """What retrieve_token()'s no-token branch and the UI-thread report touch."""

    retrieve_token = MainWindow.retrieve_token
    _report_token_retrieval_failure = MainWindow._report_token_retrieval_failure
    _show_token_retrieval_failure = MainWindow._show_token_retrieval_failure

    def __init__(self, background_mode=False):
        self.background_mode = background_mode
        self.events = []
        self.error_sound = _Sound(self.events)
        self.i18n = _I18n()
        self.app_name = "WinZapp"
        # The token __init__ loaded; the teardown needs it to close the
        # session, so a failed STEP 2 must leave it alone.
        self.token = "sess-old:hash"

    def _get_wa_token(self):
        return None

    def _set_wa_token(self, token):
        raise AssertionError("nothing to store")

    def real_exit(self):
        self.events.append("exit")


@pytest.fixture
def no_token_anywhere(monkeypatch, tmp_path):
    # The legacy token.tk fallback must find nothing either.
    patch_main_global(monkeypatch, "data_path", lambda name: str(tmp_path / name))


@pytest.fixture
def worker_thread(monkeypatch):
    monkeypatch.setattr(session_tokens.wx, "IsMainThread", lambda: False)
    queued = []
    monkeypatch.setattr(session_tokens.wx, "CallAfter",
                        lambda fn, *a, **k: queued.append((fn, a)))
    boxes = []
    monkeypatch.setattr(session_tokens.wx, "MessageBox",
                        lambda *a, **k: boxes.append(a))
    return queued, boxes


@pytest.fixture
def main_thread(monkeypatch):
    monkeypatch.setattr(session_tokens.wx, "IsMainThread", lambda: True)
    monkeypatch.setattr(session_tokens.wx, "CallAfter",
                        lambda *a, **k: pytest.fail("queued from the main thread"))
    boxes = []
    monkeypatch.setattr(session_tokens.wx, "MessageBox",
                        lambda *a, **k: boxes.append(a))
    return boxes


class TestNoTokenOnTheWorkerThread:
    def test_the_error_box_and_quit_are_queued_for_the_ui_thread(
            self, no_token_anywhere, worker_thread):
        queued, boxes = worker_thread
        stub = _TokenStub()
        assert stub.retrieve_token() is False  # no SystemExit
        # Nothing shown or played on the worker itself.
        assert boxes == [] and stub.events == []
        assert stub.token == "sess-old:hash"
        assert len(queued) == 1
        fn, args = queued[0]
        assert fn == stub._report_token_retrieval_failure

        # What the UI thread then runs: the same sound and box, then the quit.
        fn(*args)
        assert stub.events == ["sound", "exit"]
        (message, title, _style), = boxes
        assert message.startswith("No token. ")
        assert title == "WinZapp error"

    def test_background_mode_quits_silently(self, no_token_anywhere, worker_thread):
        queued, boxes = worker_thread
        stub = _TokenStub(background_mode=True)
        assert stub.retrieve_token() is False
        assert queued == [(stub.real_exit, ())]
        assert boxes == [] and stub.events == []

    def test_a_box_that_cannot_be_shown_still_quits(self, worker_thread):
        _queued, _boxes = worker_thread
        stub = _TokenStub()
        stub.error_sound = None  # .play() raises
        stub._report_token_retrieval_failure("details")
        assert stub.events == ["exit"]

    def test_a_quit_already_under_way_skips_the_box_but_still_quits(self, worker_thread):
        """Nobody is waiting on the box, and a modal would hold the quit
        back; real_exit() still runs, since it copes with an overlapping quit
        and a cancelled Windows shutdown would otherwise leave the window up."""
        _queued, boxes = worker_thread
        stub = _TokenStub()
        stub._shutting_down = True
        stub._report_token_retrieval_failure("details")
        assert boxes == []
        assert stub.events == ["exit"]

    def test_both_threads_share_one_box(self):
        """Same sound and text on both paths, by construction."""
        report = inspect.getsource(MainWindow._report_token_retrieval_failure)
        retrieve = inspect.getsource(MainWindow.retrieve_token)
        assert "self._show_token_retrieval_failure(details)" in report
        assert "self._show_token_retrieval_failure(format_exc())" in retrieve
        for src in (report, retrieve):
            assert "wx.MessageBox" not in src
            assert "error_sound" not in src


class TestNoTokenOnTheMainThreadIsUnchanged:
    """__init__'s call: before the main loop, sys.exit() ends the process."""

    def test_foreground_shows_the_box_then_exits(self, no_token_anywhere, main_thread):
        boxes = main_thread
        stub = _TokenStub()
        with pytest.raises(SystemExit) as exc:
            stub.retrieve_token()
        assert exc.value.code is None
        assert stub.events == ["sound"]
        (message, title, _style), = boxes
        assert message.startswith("No token. ") and title == "WinZapp error"

    def test_background_exits_silently(self, no_token_anywhere, main_thread):
        stub = _TokenStub(background_mode=True)
        with pytest.raises(SystemExit) as exc:
            stub.retrieve_token()
        assert exc.value.code == 0
        assert main_thread == [] and stub.events == []


class TestPostUiInitWiring:
    def _post(self) -> str:
        src = inspect.getsource(MainWindow.__init__)
        return src[src.index("def _post_ui_init"):]

    def _step1(self) -> str:
        post = self._post()
        return post[:post.index("STEP 2")]

    def _step2(self) -> str:
        post = self._post()
        return post[post.index("STEP 2"):post.index("STEP 3")]

    def test_step_1_uses_the_helper_and_stops_when_it_says_so(self):
        step1 = self._step1()
        assert "if not self._recheck_pairing_after_ui():" in step1
        assert "elif not self.background_mode:" in step1

    def test_step_2_stops_when_retrieve_token_says_so(self):
        assert "if not self.retrieve_token():" in self._step2()

    def test_step_2s_empty_token_guard_leaves_the_re_pairing_flow_in_charge(self):
        """Only a logout on the UI thread empties self.token after
        retrieve_token() succeeded, and that logout has opened the pairing
        dialog: the thread stops, nothing quits under it."""
        step2 = self._step2()
        guard = step2[step2.index("if not self.token:"):]
        assert "return" in guard
        assert "real_exit" not in guard

    def test_no_step_calls_sys_exit_on_its_thread(self):
        assert "sys.exit(" not in self._step1()
        assert "sys.exit(" not in self._step2()
        assert "sys.exit(" not in inspect.getsource(MainWindow._recheck_pairing_after_ui)

    def test_init_still_calls_retrieve_token_on_the_main_thread(self):
        src = inspect.getsource(MainWindow.__init__)
        before_post = src[:src.index("def _post_ui_init")]
        assert "self.retrieve_token()" in before_post
