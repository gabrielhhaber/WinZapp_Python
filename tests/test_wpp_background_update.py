"""A WPPConnect Server update can be built while the old server keeps running.

In place, an accepted update stops the server first and keeps WinZapp offline
behind a modal progress window for the whole download, `npm install` and
build: minutes. With Settings > General > "download updates in the background"
the new server is built in api_staging/ by the same ApiSetupDialog code, run
with no window; the user stays online. Only when it is built does WinZapp say
it is about to install, and only then is the server stopped — for two renames
(tests/test_api_staging.py) — and started again.

MainWindow and ApiSetupDialog are wx windows, so their methods are bound onto
plain stubs, as tests/test_wpp_update_machine_claim_and_rollback.py does.
"""

import os
import types

import pytest
import wx

from core import api_staging
from main_window import updates, wpp_background_update
from main_window.updates import UpdatesMixin
from main_window.wpp_background_update import WppBackgroundUpdateMixin
from ui.dialogs import api_setup

TAG = "v2.9.0"


def _busy(window) -> bool:
    """An update is installing, or one is being built in the background."""
    return bool(getattr(window, "_wpp_updating", False)
                or getattr(window, "_wpp_staging", None))


class _Window(WppBackgroundUpdateMixin):
    _update_wpp_server = UpdatesMixin._update_wpp_server

    def __init__(self, background=True):
        self.settings = {"general": {"background_update_downloads": background}}
        self.global_dir, self.account_id = "gd", "acc"
        self.background_mode = self._window_hidden = False
        self._shutting_down = False
        self.i18n = types.SimpleNamespace(
            t=lambda key: key + " {version}" if key == "wpp_update_background_ready_msg" else key)
        self.error_sound = types.SimpleNamespace(play=lambda: self.events.append("error sound"))
        self.spoken, self.events = [], []
        self.wpp_process = object()

    def output(self, text, interrupt=False):
        self.spoken.append(text)

    def _stop_wpp_server(self):
        self.events.append("stop")

    def wait_for_profile_release(self, *a, **k):
        pass

    def ensure_wpp_running(self):
        self.events.append("start")

    def _reconnect_websocket_now(self):
        pass

    def check_wa_connection_http(self):
        pass

    def trigger_sync_if_needed(self):
        pass


@pytest.fixture
def env(monkeypatch, tmp_path):
    state = types.SimpleNamespace(
        builds=[], boxes=[], later=[], discarded=[], swaps=[], pending=[], order=[],
        room=True, swap_error=None, busy=False, modal=[], api=str(tmp_path / "api"),
        build_raises=None)

    class _Thread:
        """Worker threads are queued, so a test decides when each one runs."""

        def __init__(self, target=None, args=(), **_kw):
            self._target, self._args = target, args

        def start(self):
            state.pending.append(lambda: self._target(*self._args))

    class _Build:
        """ApiSetupDialog in background mode: remembered, finished by the test."""

        def __init__(self, parent, title_override=None, forced_tag=None,
                     api_dir=None, on_done=None):
            if on_done is not None and state.build_raises:
                raise state.build_raises
            self.forced_tag, self.api_dir, self.on_done = forced_tag, api_dir, on_done
            self.cancelled = 0
            if on_done is None:
                state.modal.append(forced_tag)
            else:
                state.builds.append(self)

        def ShowModal(self):
            return wx.ID_OK

        def Destroy(self):
            pass

        def cancel_background(self):
            self.cancelled += 1
            self.on_done(False, "", True)

    def _box(window, text, title, style, announce=None):
        state.boxes.append(text)
        window.events.append("notice" if "ready" in text else "box")
        return wx.OK

    def _swap(api_dir, staged_dir, attempts=5, pause=1.0):
        state.swaps.append((api_dir, staged_dir))
        if state.swap_error:
            raise api_staging.SwapError(state.swap_error)
        return api_dir + "_old"

    for module in (updates, wpp_background_update):
        monkeypatch.setattr(module, "threading", types.SimpleNamespace(Thread=_Thread))
        monkeypatch.setattr(module.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))
        monkeypatch.setattr(module, "message_box", _box)
        monkeypatch.setattr(module, "resource_path", lambda *parts: os.path.join(state.api, *parts[1:]))
    monkeypatch.setattr(wpp_background_update.wx, "CallLater",
                        lambda ms, fn, *a: state.later.append((fn, a)))
    monkeypatch.setattr(updates, "bring_to_front_if_hidden", lambda *a, **k: None)
    monkeypatch.setattr(updates, "homologated_wpp_tag", lambda _p: "v2.8.0")
    monkeypatch.setattr(updates, "_server_is_built", lambda: True)
    monkeypatch.setattr("core.wa_version_refresh.other_accounts_node_alive",
                        lambda g, a, ignore_corrupt=False: state.busy)
    monkeypatch.setattr(api_setup, "ApiSetupDialog", _Build)
    def _has_room(api_dir):
        state.order.append("measure")
        return state.room

    def _discard(path):
        state.order.append("discard")
        state.discarded.append(path)

    monkeypatch.setattr(api_staging, "has_room_for_staging", _has_room)
    monkeypatch.setattr(api_staging, "swap_in_staged_api", _swap)
    monkeypatch.setattr(api_staging, "discard", _discard)

    def _run_threads():
        while state.pending:
            state.pending.pop(0)()

    state.run_threads = _run_threads
    return state


def _started(env, window, finished=None):
    """Accept the update and let the background build begin."""
    result = window._update_wpp_server(
        TAG, on_finished=(finished.append if finished is not None else None))
    env.run_threads()
    return result


class TestTheBuildHappensBehindARunningServer:
    def test_nothing_is_stopped_and_winzapp_is_not_updating_yet(self, env):
        window = _Window()

        assert _started(env, window) is True

        assert window.events == []
        assert getattr(window, "_wpp_updating", False) is False
        assert window.spoken == ["wpp_update_background_started"]

    def test_it_is_built_next_to_the_server_by_the_setup_code_with_no_window(self, env):
        window = _Window()
        _started(env, window)

        build, = env.builds
        assert build.forced_tag == TAG
        assert build.api_dir == env.api + "_staging"
        assert env.modal == []                        # no progress window was opened

    def test_what_an_interrupted_build_left_is_cleared_first(self, env):
        _started(env, _Window())
        assert env.discarded[:2] == [env.api + "_staging", env.api + "_old"]

    def test_a_second_update_is_refused_meanwhile_and_says_why(self, env):
        """A reinstall asked from the menu must not vanish in silence."""
        window = _Window()
        _started(env, window)

        assert window._update_wpp_server(TAG) is False
        assert len(env.builds) == 1
        assert window.spoken[-1] == "wpp_update_background_running"

    def test_with_the_option_off_it_updates_in_place_as_before(self, env):
        window = _Window(background=False)

        assert _started(env, window) is True

        assert env.builds == [] and env.modal == [TAG]
        assert window.events[0] == "stop"

    def test_without_room_for_a_second_server_it_updates_in_place(self, env):
        env.room = False
        finished = []
        window = _Window()

        assert _started(env, window, finished) is True

        assert env.builds == [] and env.modal == [TAG]
        assert "wpp_update_background_started" not in window.spoken
        assert window.events[0] == "stop" and finished == [True]
        assert _busy(window) is False

    def test_the_room_is_measured_after_the_leftovers_are_gone(self, env):
        """An interrupted build's 1.3 GB would otherwise count against the
        very update that is about to remove it."""
        _started(env, _Window())
        assert env.order[:3] == ["discard", "discard", "measure"]

    def test_an_update_in_place_sweeps_the_leftovers_too(self, env):
        """With the option off nothing else would ever remove them."""
        window = _Window(background=False)

        _started(env, window)

        assert env.api + "_staging" in env.discarded and env.api + "_old" in env.discarded

    def test_startup_deletes_only_what_a_swap_moved_aside(self, env):
        """api_old and api_staging belong to an update in progress, possibly
        another account's; only the .stale-* directories are safe at startup."""
        stale = env.api + "_old.stale-1-abc"
        os.makedirs(stale)
        window = _Window()

        window._discard_stale_wpp_servers_async()
        env.run_threads()

        assert env.discarded == [stale]

    def test_a_build_that_cannot_even_start_does_not_stay_running(self, env):
        env.build_raises = RuntimeError("no window")
        finished = []
        window = _Window()

        _started(env, window, finished)

        assert finished == [False] and _busy(window) is False
        assert env.boxes == ["wpp_update_failed_msg"] and "stop" not in window.events


class TestOnlyThenItInstalls:
    def test_it_says_so_then_stops_swaps_and_starts(self, env):
        finished = []
        window = _Window()
        _started(env, window, finished)

        env.builds[0].on_done(True, "", False)
        env.run_threads()

        assert env.boxes == ["wpp_update_background_ready_msg 2.9.0"]
        assert window.events[:3] == ["notice", "stop", "start"]
        assert env.swaps == [(env.api, env.api + "_staging")]
        assert finished == [True]
        assert getattr(window, "_wpp_updating", False) is False
        assert "wpp_update_complete" in window.spoken

    def test_the_replaced_server_is_deleted_off_the_main_thread(self, env):
        window = _Window()
        _started(env, window)
        env.discarded.clear()

        env.builds[0].on_done(True, "", False)
        assert env.api + "_old" not in env.discarded  # queued, not done inline
        env.run_threads()

        assert env.api + "_old" in env.discarded

    def test_it_waits_while_the_user_is_on_a_call(self, env):
        window = _Window()
        window._voice_call_in_progress = lambda: True
        _started(env, window)

        env.builds[0].on_done(True, "", False)

        assert env.boxes == [] and window.events == []
        (fn, args), = env.later
        window._voice_call_in_progress = lambda: False
        fn(*args)
        env.run_threads()
        assert window.events[:3] == ["notice", "stop", "start"]

    def test_another_update_may_start_once_this_one_ended(self, env):
        window = _Window()
        _started(env, window)
        env.builds[0].on_done(True, "", False)
        env.run_threads()

        assert _busy(window) is False


class TestWhenItDoesNotGoWell:
    def test_a_failed_build_never_touched_the_installed_server(self, env):
        finished = []
        window = _Window()
        _started(env, window, finished)

        env.builds[0].on_done(False, "npm ERR!", False)
        env.run_threads()

        assert "stop" not in window.events and env.swaps == []
        assert env.boxes == ["wpp_update_failed_msg"]
        assert env.api + "_staging" in env.discarded
        assert finished == [False] and _busy(window) is False

    def test_a_swap_that_fails_brings_the_old_server_back_up(self, env):
        env.swap_error = "in use"
        finished = []
        window = _Window()
        _started(env, window, finished)
        env.discarded.clear()

        env.builds[0].on_done(True, "", False)
        env.run_threads()

        assert window.events[:2] == ["notice", "stop"] and window.events[-1] == "start"
        assert env.discarded == [env.api + "_staging"]   # the unused build does not stay on disk
        assert "wpp_update_failed_msg" in env.boxes
        assert env.modal == []                        # the built old server needs no rollback
        assert finished == [False]

    def test_another_accounts_server_coming_up_meanwhile_stops_the_install(self, env):
        """The swap replaces the api/ that account's Node is running from."""
        finished = []
        window = _Window()
        _started(env, window, finished)
        env.busy = True

        env.builds[0].on_done(True, "", False)
        env.run_threads()

        assert "stop" not in window.events and env.swaps == []
        assert "wpp_update_other_account_msg" in env.boxes
        assert env.api + "_staging" in env.discarded and finished == [False]


class TestQuitting:
    def test_the_build_is_cancelled_so_npm_does_not_outlive_the_app(self, env):
        finished = []
        window = _Window()
        _started(env, window, finished)

        window._shutting_down = True
        window.cancel_wpp_background_update()
        env.run_threads()

        assert env.builds[0].cancelled == 1
        assert env.boxes == [] and "stop" not in window.events
        assert finished == [False]

    def test_with_nothing_being_built_there_is_nothing_to_cancel(self, env):
        _Window().cancel_wpp_background_update()

    def test_a_build_that_ends_while_quitting_installs_nothing(self, env):
        window = _Window()
        _started(env, window)
        window._shutting_down = True

        env.builds[0].on_done(True, "", False)
        env.run_threads()

        assert env.boxes == [] and env.swaps == [] and "stop" not in window.events


# ── The setup dialog, run with no window ────────────────────────────────────


class _Timer:
    stopped = 0

    def Stop(self):
        self.stopped += 1


class _BackgroundSetup:
    _on_cancel = api_setup.ApiSetupDialog._on_cancel
    cancel_background = api_setup.ApiSetupDialog.cancel_background
    _after_background_cancel = api_setup.ApiSetupDialog._after_background_cancel
    _finish_success = api_setup.ApiSetupDialog._finish_success
    _finish_error = api_setup.ApiSetupDialog._finish_error
    _finish_in_background = api_setup.ApiSetupDialog._finish_in_background

    def __init__(self):
        self._cancelled = self._finished = False
        self._trickling = True
        self._timer = _Timer()
        self.done, self.destroyed, self.killed = [], 0, 0
        self._on_done = lambda ok, details, cancelled: self.done.append((ok, details, cancelled))

    def Destroy(self):
        self.destroyed += 1

    def _kill_proc_tree(self):
        self.killed += 1


@pytest.fixture
def no_boxes(monkeypatch):
    shown = []
    monkeypatch.setattr(api_setup.wx, "MessageBox", lambda *a, **k: shown.append(a))
    return shown


class TestTheSetupReportsInsteadOfShowing:
    def test_success_is_reported_with_no_message_box(self, no_boxes):
        setup = _BackgroundSetup()

        setup._finish_success()
        setup._finish_success()                       # a late duplicate

        assert setup.done == [(True, "", False)]
        assert no_boxes == [] and setup.destroyed == 1 and setup._timer.stopped == 1

    def test_a_failure_carries_its_details(self, no_boxes, caplog):
        setup = _BackgroundSetup()

        setup._finish_error("npm ERR! code E404")

        assert setup.done == [(False, "npm ERR! code E404", False)]
        assert no_boxes == [] and setup.destroyed == 1
        assert "npm ERR! code E404" in caplog.text

    def test_a_cancel_kills_npm_and_says_it_was_a_cancel(self, no_boxes):
        setup = _BackgroundSetup()

        setup._on_cancel()
        setup._finish_success()                       # the worker finishing late

        assert setup.killed == 1 and setup.done == [(False, "", True)]

    def test_quitting_kills_npm_at_once_and_leaves_wx_to_the_main_thread(self, no_boxes,
                                                                          monkeypatch):
        """cancel_background() runs on the shutdown thread. Killing npm is what
        must happen there; the timer, the report and Destroy are wx and wait
        for the main thread."""
        queued = []
        monkeypatch.setattr(api_setup.wx, "CallAfter", lambda fn, *a: queued.append((fn, a)))
        setup = _BackgroundSetup()

        setup.cancel_background()

        assert setup.killed == 1 and setup._cancelled is True
        assert setup._timer.stopped == 0 and setup.destroyed == 0 and setup.done == []

        (fn, args), = queued
        fn(*args)                                     # the main thread, if it ever runs
        assert setup.done == [(False, "", True)] and setup.destroyed == 1

        setup.cancel_background()                     # a second quit path
        assert setup.killed == 1 and len(queued) == 1

    def test_the_dialog_goes_away_even_if_the_listener_raises(self, no_boxes):
        setup = _BackgroundSetup()
        setup._on_done = lambda *a: (_ for _ in ()).throw(RuntimeError("listener"))

        with pytest.raises(RuntimeError):
            setup._finish_success()

        assert setup.destroyed == 1


class TestTheSetupBuildsWhereItIsTold:
    """_run_setup() with every slow step replaced: where does it work?"""

    def _run(self, monkeypatch, tmp_path, api_dir):
        live = tmp_path / "live"
        (live / "api" / "src").mkdir(parents=True)
        (live / "api" / "dist").mkdir()
        (live / "api" / "dist" / "server.js").write_text("installed", encoding="utf-8")
        seen = types.SimpleNamespace(cwds=[], caches=[], extracted=[], patched=[], ended=[])
        monkeypatch.setattr(api_setup, "resource_path", lambda *parts: str(live.joinpath(*parts)))
        monkeypatch.setattr(api_setup.wx, "CallAfter", lambda fn, *a: fn(*a))

        def _subprocess(cmd, cwd=None, env=None):
            seen.cwds.append(cwd)
            seen.caches.append((env or {}).get("PUPPETEER_CACHE_DIR"))
            return True, ""

        setup = types.SimpleNamespace(
            _api_dir=api_dir, _forced_tag=TAG, _cancelled=False,
            _i18n=types.SimpleNamespace(t=lambda key: key),
            _STAGES_FULL=api_setup.ApiSetupDialog._STAGES_FULL,
            _STAGES_MODULES_ONLY=api_setup.ApiSetupDialog._STAGES_MODULES_ONLY,
            _set_stage=lambda *a, **k: None,
            _download_zip=lambda url, dest, *a: True,
            _extract_zip=lambda zip_path, target: seen.extracted.append(target) or True,
            _merge_package_json_dependencies=lambda api, patches: None,
            _apply_node_modules_patches=seen.patched.append,
            _run_subprocess=_subprocess,
            _finish_success=lambda: seen.ended.append("ok"),
            _finish_error=lambda details="": seen.ended.append("error: " + details),
        )
        api_setup.ApiSetupDialog._run_setup(setup)
        return seen, live / "api"

    def test_in_a_staging_directory_the_installed_server_is_left_alone(self, monkeypatch, tmp_path):
        staging = str(tmp_path / "live" / "api_staging")

        seen, live_api = self._run(monkeypatch, tmp_path, staging)

        assert seen.ended == ["ok"]
        assert seen.extracted == [staging] and seen.patched == [staging]
        assert set(seen.cwds) == {staging}
        assert set(seen.caches) == {os.path.join(staging, ".cache")}
        assert (live_api / "dist" / "server.js").read_text(encoding="utf-8") == "installed"

    def test_without_one_it_rebuilds_in_place_as_before(self, monkeypatch, tmp_path):
        seen, live_api = self._run(monkeypatch, tmp_path, None)

        assert seen.ended == ["ok"]
        assert seen.extracted == [str(live_api)] and set(seen.cwds) == {str(live_api)}
        assert set(seen.caches) == {str(live_api / ".cache")}
        assert not (live_api / "dist").exists()       # the in-place clean step wiped it
