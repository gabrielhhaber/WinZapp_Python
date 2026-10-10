"""A WinZapp update can download without taking the app away.

An accepted update opened UpdateProgressDialog at once: a modal window, so the
user was kept out of WinZapp for the whole download. With Settings > General >
"download updates in the background" (install-wide, off by default) the
download, the signature check and the extraction run on a thread with no
window; only when the package is ready does WinZapp say it is about to
install, and the install phase alone runs behind the dialog.

UpdateChecker wants a real MainWindow and the dialogs are wx windows, so the
methods are bound onto plain stubs (see tests/test_updater_does_not_quit_
without_installing.py, whose stubs these extend).
"""

import threading
import types

import pytest
import wx

import update_background
import updater
from app_settings import _DEFAULTS, _GENERAL_GLOBAL
from core.utils import DEFAULT_SETTINGS
from update_background import (
    BackgroundDownloadMixin,
    background_downloads_enabled,
    may_interrupt_now,
)
from update_package import UpdatePackage
from updater import UpdateChecker

ARGS = ("2.0.0.1", "https://example.invalid/WinZapp.zip", "sums", "sig", False)


class TestTheOption:
    def test_off_by_default_everywhere_it_is_declared(self):
        assert DEFAULT_SETTINGS["general"]["background_update_downloads"] is False
        assert _DEFAULTS["background_update_downloads"] is False

    def test_it_is_one_choice_for_the_whole_install(self):
        """Every account shares the binary and api/ the updates replace."""
        assert "background_update_downloads" in _GENERAL_GLOBAL

    def test_only_an_explicit_true_turns_it_on(self):
        on = {"general": {"background_update_downloads": True}}
        assert background_downloads_enabled(on) is True
        for value in ("yes", 1, None, False):
            assert background_downloads_enabled(
                {"general": {"background_update_downloads": value}}) is False
        assert background_downloads_enabled({}) is False
        assert background_downloads_enabled(None) is False


class TestWhenTheInstallMayStart:
    def _window(self, ready=True, may_run=True, in_call=False):
        event = threading.Event()
        if ready:
            event.set()
        return types.SimpleNamespace(
            _ui_ready_event=event,
            wpp_update_may_run_now=lambda: may_run,
            _voice_call_in_progress=lambda: in_call,
        )

    def test_normally_yes(self):
        assert may_interrupt_now(self._window()) is True

    def test_not_before_the_main_window_exists(self):
        assert may_interrupt_now(self._window(ready=False)) is False

    def test_not_over_a_pairing(self):
        assert may_interrupt_now(self._window(may_run=False)) is False

    def test_never_in_the_middle_of_a_call(self):
        """The restart would cut it, at a moment the user did not choose."""
        assert may_interrupt_now(self._window(in_call=True)) is False

    def test_not_while_a_call_is_ringing(self):
        window = self._window()
        window._incoming_call_dialogs = {"caller": object()}
        assert may_interrupt_now(window) is False

    def test_a_call_still_ringing_after_its_popup_was_dismissed_counts_too(self):
        window = self._window()
        window._incoming_call_dialogs = {}
        window._active_incoming_calls = {"caller"}
        assert may_interrupt_now(window) is False

    def test_not_while_the_server_is_being_reinstalled_in_place(self):
        """Quitting then would leave api/ half-built with npm still running."""
        window = self._window()
        window._wpp_updating = True
        assert may_interrupt_now(window) is False

    @pytest.mark.parametrize("flag", ["_is_recording", "_recording_starting"])
    def test_not_over_a_voice_message_being_recorded(self, flag):
        """The notice has one button; pressing it would throw the recording away."""
        window = self._window()
        window.conversations_panel = types.SimpleNamespace(**{flag: True})
        assert may_interrupt_now(window) is False

    def test_a_window_without_those_notions_is_not_held_back(self):
        assert may_interrupt_now(types.SimpleNamespace()) is True


class _MainWindow:
    def __init__(self, background=True):
        self.settings = {"general": {"background_update_downloads": background}}
        self.i18n = types.SimpleNamespace(
            t=lambda key: key + " {version}" if key == "update_background_ready_msg" else (
                key + " {error}" if key == "update_error_msg" else key))
        self.spoken, self.exits = [], 0
        self._shutting_down = False

    def output(self, text, interrupt=False):
        self.spoken.append(text)

    def real_exit(self):
        self.exits += 1


class _Checker(BackgroundDownloadMixin):
    _do_install = UpdateChecker._do_install
    _check_once = UpdateChecker._check_once

    def __init__(self, background=True):
        self._mw = _MainWindow(background)
        self.released = self.retries = 0
        self._force = False

    def _release_prompt(self):
        self.released += 1

    def _schedule_retry(self):
        self.retries += 1


@pytest.fixture
def world(monkeypatch):
    """Threads and CallAfter run inline; dialogs and boxes are recorded."""
    state = types.SimpleNamespace(dialogs=[], boxes=[], answer=wx.OK, later=[], discarded=[],
                                  package=UpdatePackage(extract_dir="X:/pkg"), download_raises=None,
                                  dialog_result=wx.ID_OK, install_ok=True, downloads=[],
                                  hold=False, held=[])

    class _Thread:
        def __init__(self, target=None, args=(), **_kw):
            self._target, self._args = target, args

        def start(self):
            if state.hold:                            # the download is "still running"
                state.held.append(lambda: self._target(*self._args))
            else:
                self._target(*self._args)

    class _Dialog:
        _error_msg = "boom"

        def __init__(self, *args, **kwargs):
            state.dialogs.append(kwargs.get("extracted_dir", ""))
            self._install_ok = state.install_ok

        def run(self):
            return state.dialog_result

        def Destroy(self):
            pass

    def _download(zip_url, sums, sig, version, is_alpha, i18n, on_progress=None, is_cancelled=None):
        state.downloads.append((version, on_progress, is_cancelled))
        if state.download_raises:
            raise state.download_raises
        return state.package

    def _box(mw, text, title, style, announce=None):
        state.boxes.append((text, title, style))
        return state.answer

    monkeypatch.setattr(update_background.threading, "Thread", _Thread)
    monkeypatch.setattr(update_background.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    monkeypatch.setattr(update_background.wx, "CallLater",
                        lambda ms, fn, *a, **kw: state.later.append((ms, fn, a)))
    monkeypatch.setattr(update_background, "download_update_package", _download)
    monkeypatch.setattr(update_background, "message_box", _box)
    monkeypatch.setattr(update_background, "discard_package", state.discarded.append)
    monkeypatch.setattr(updater, "discard_package", state.discarded.append)
    monkeypatch.setattr(updater, "message_box", _box)
    monkeypatch.setattr(updater, "UpdateProgressDialog", _Dialog)
    monkeypatch.setattr(updater, "bring_to_front_if_hidden", lambda *a, **kw: None)
    return state


class TestWithTheOptionOff:
    def test_the_progress_dialog_opens_at_once_as_before(self, world):
        checker = _Checker(background=False)

        checker._do_install(*ARGS)

        assert world.dialogs == [""] and world.downloads == []
        assert checker._mw.exits == 1


class TestWithTheOptionOn:
    def test_the_download_runs_with_no_window_and_no_gauge(self, world):
        world.answer = wx.OK
        checker = _Checker()

        checker._do_install(*ARGS)

        (version, on_progress, is_cancelled), = world.downloads
        assert version == "2.0.0.1" and on_progress is None
        assert checker._mw.spoken[0] == "update_background_started"

    def test_then_it_says_so_and_only_the_install_runs_behind_the_dialog(self, world):
        checker = _Checker()

        checker._do_install(*ARGS)

        (text, title, style), = world.boxes
        assert text == "update_background_ready_msg 2.0.0.1"
        assert style & wx.OK and not style & wx.YES_NO
        assert world.dialogs == ["X:/pkg"]            # the dialog got the package: no second download
        assert checker._mw.exits == 1

    def test_the_notice_comes_before_the_install_never_after(self, world, monkeypatch):
        order = []
        monkeypatch.setattr(update_background, "message_box",
                            lambda *a, **kw: order.append("notice") or wx.OK)
        checker = _Checker()
        real = checker._do_install

        def _spy(*args, **kwargs):
            if kwargs.get("extracted_dir"):
                order.append("install")
            return real(*args, **kwargs)

        checker._do_install = _spy
        checker._do_install(*ARGS)

        assert order == ["notice", "install"]

    def test_it_waits_while_the_user_is_on_a_call(self, world):
        checker = _Checker()
        checker._mw._voice_call_in_progress = lambda: True

        checker._do_install(*ARGS)

        assert world.boxes == [] and world.dialogs == [] and checker._mw.exits == 0
        (ms, fn, args), = world.later
        assert ms == BackgroundDownloadMixin._READY_RETRY_MS

        checker._mw._voice_call_in_progress = lambda: False
        fn(*args)                                      # the retry, once the call is over
        assert len(world.boxes) == 1 and checker._mw.exits == 1

    def test_only_quitting_cancels_the_download(self, world):
        checker = _Checker()
        checker._do_install(*ARGS)
        _version, _progress, is_cancelled = world.downloads[0]

        assert is_cancelled() is False
        checker._mw._shutting_down = True
        assert is_cancelled() is True


class TestWhenItDoesNotGoWell:
    def test_a_refused_package_asks_whether_to_try_again(self, world):
        world.package = UpdatePackage(error="bad signature")
        world.answer = wx.NO
        checker = _Checker()

        checker._do_install(*ARGS)

        (text, _title, style), = world.boxes
        assert text == "update_error_msg bad signature" and style & wx.YES_NO
        assert world.dialogs == [] and checker._mw.exits == 0
        assert (checker.released, checker.retries) == (1, 1)

    def test_a_network_fault_asks_the_same_question(self, world):
        world.download_raises = ConnectionError("no route")
        world.answer = wx.NO
        checker = _Checker()

        checker._do_install(*ARGS)

        assert world.boxes[0][0] == "update_error_msg no route"
        assert (checker.released, checker.retries) == (1, 1)

    def test_yes_downloads_again_in_the_background(self, world, monkeypatch):
        world.package = UpdatePackage(error="bad signature")
        answers = [wx.YES, wx.NO]
        checker = _Checker()
        monkeypatch.setattr(update_background, "message_box", lambda *a, **kw: answers.pop(0))

        checker._do_install(*ARGS)

        assert len(world.downloads) == 2 and world.dialogs == []

    def test_quitting_mid_download_hands_the_prompt_back(self, world):
        world.package = UpdatePackage(cancelled=True)
        checker = _Checker()

        checker._do_install(*ARGS)

        assert world.boxes == [] and checker.released == 1 and checker._mw.exits == 0

    def test_a_shutdown_windows_cancelled_does_not_lose_the_update(self, world):
        """_shutting_down goes back to False when another program cancels the
        shutdown and WinZapp lives on. The download was cancelled meanwhile;
        without a retry nothing would offer the update again until the next
        launch — and a WinZapp started with Windows is rarely relaunched."""
        world.package = UpdatePackage(cancelled=True)
        checker = _Checker()

        checker._do_install(*ARGS)

        assert checker.retries == 1
        assert checker._background_update_version() == ""

    def test_quitting_before_the_notice_drops_the_package(self, world):
        checker = _Checker()
        real = checker._install_downloaded_update

        def _quit_first(args, extract_dir):
            checker._mw._shutting_down = True
            return real(args, extract_dir)

        checker._install_downloaded_update = _quit_first
        checker._do_install(*ARGS)

        assert world.boxes == [] and world.dialogs == []
        assert world.discarded == ["X:/pkg"] and checker.released == 1
        assert checker.retries == 1

    def test_an_install_that_is_cancelled_drops_the_package(self, world):
        world.dialog_result = wx.ID_CANCEL
        checker = _Checker()

        checker._do_install(*ARGS)

        assert world.discarded == ["X:/pkg"]
        assert checker._mw.exits == 0 and (checker.released, checker.retries) == (1, 1)

    def test_a_failed_install_retried_does_not_download_again(self, world, monkeypatch):
        """UAC declined, say: Yes runs the install again from the same package."""
        world.dialog_result = wx.ID_ABORT
        answers = [wx.OK, wx.YES, wx.NO]              # the notice, retry, give up
        monkeypatch.setattr(update_background, "message_box", lambda *a, **kw: answers.pop(0))
        monkeypatch.setattr(updater, "message_box", lambda *a, **kw: answers.pop(0))
        checker = _Checker()

        checker._do_install(*ARGS)

        assert world.dialogs == ["X:/pkg", "X:/pkg"] and len(world.downloads) == 1
        assert world.discarded == ["X:/pkg"]


class TestOnlyOneAtATime:
    """With the option on the main window is free during the download, so
    "check for updates" and "reinstall" stay within reach the whole time."""

    def _downloading(self, world):
        world.hold = True
        checker = _Checker()
        checker._do_install(*ARGS)
        assert checker._background_update_version() == "2.0.0.1"
        return checker

    def test_asking_to_install_again_starts_no_second_download(self, world):
        checker = self._downloading(world)

        checker._do_install(*ARGS)

        assert len(world.held) == 1
        assert checker._mw.spoken[-1] == "update_background_running"
        assert checker.released == 0                  # the first one still owns the prompt

    def test_a_forced_check_says_it_is_already_downloading(self, world, monkeypatch):
        monkeypatch.setattr(updater.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
        checker = self._downloading(world)
        checker._force = True

        checker._check_once()

        assert checker._mw.spoken[-1] == "update_background_running"
        assert checker._force is False and world.boxes == [] and checker.retries == 1

    def test_an_automatic_check_stays_silent(self, world):
        checker = self._downloading(world)
        spoken = list(checker._mw.spoken)

        checker._check_once()

        assert checker._mw.spoken == spoken and checker.retries == 1

    def test_it_ends_when_the_download_does(self, world):
        world.dialog_result = wx.ID_CANCEL            # the install phase is cancelled
        checker = self._downloading(world)

        world.held.pop()()

        assert checker._background_update_version() == ""

    def test_giving_up_after_a_failure_ends_it_too(self, world):
        world.package = UpdatePackage(error="bad signature")
        world.answer = wx.NO
        checker = self._downloading(world)

        world.held.pop()()

        assert checker._background_update_version() == ""
        assert (checker.released, checker.retries) == (1, 1)


class TestTheDialogSkipsWhatIsAlreadyDone:
    def test_given_a_package_it_downloads_nothing(self, monkeypatch):
        fetched, launched = [], []
        monkeypatch.setattr(updater, "download_update_package",
                            lambda *a, **kw: fetched.append(1))
        monkeypatch.setattr(updater, "_is_frozen", lambda: True)
        monkeypatch.setattr(updater, "_run_batch_installer",
                            lambda extract_dir, *a, **kw: launched.append(extract_dir) or True)
        monkeypatch.setattr(updater.wx, "CallAfter", lambda fn, *a: fn(*a))
        dialog = types.SimpleNamespace(
            _extracted_dir="X:/pkg", _cancelled=False, _install_ok=False, _error_msg="",
            _gauge=types.SimpleNamespace(SetValue=lambda value: None),
            _status_label=types.SimpleNamespace(SetLabel=lambda text: None),
            _main_window=types.SimpleNamespace(i18n=types.SimpleNamespace(t=lambda k: k),
                                               wpp_port=6300),
            _quit_other_accounts=lambda: [], _claim_install_slot=lambda: {},
            _end_install_slot=lambda token: None, EndModal=lambda result: None, IsModal=lambda: True,
        )

        updater.UpdateProgressDialog._worker(dialog)

        assert fetched == [] and launched == ["X:/pkg"]
        assert dialog._install_ok is True
