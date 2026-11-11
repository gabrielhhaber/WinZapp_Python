"""Settings > General > "download updates automatically and install them when
I exit WinZapp" (client/update_on_exit.py).

The update downloads on its own, waits, and installs when the user chooses
Exit — and WinZapp is not started again. The generated scripts are text, so
what they say about relaunching is asserted without launching anything; the
checker's methods are bound onto plain stubs.
"""

import types

import pytest

import update_on_exit
from app_settings import _DEFAULTS, _GENERAL_GLOBAL
from core.update_recovery import build_guarded_waiter
from core.utils import DEFAULT_SETTINGS
from update_on_exit import UpdateOnExitMixin, update_on_exit_enabled
from update_relaunch import build_relaunch_waiter_script, relaunch_lines

EXE = r"C:\WinZapp\WinZapp.exe"


def test_only_an_explicit_true_enables_it():
    assert update_on_exit_enabled({"general": {"update_on_exit": True}}) is True
    for value in (False, "true", 1, None):
        assert update_on_exit_enabled({"general": {"update_on_exit": value}}) is False
    assert update_on_exit_enabled({}) is False
    assert update_on_exit_enabled(None) is False


def test_the_option_is_off_by_default_and_install_wide():
    assert _DEFAULTS["update_on_exit"] is False
    assert "update_on_exit" in _GENERAL_GLOBAL
    assert DEFAULT_SETTINGS["general"]["update_on_exit"] is False


class TestNoRelaunch:
    def test_the_installer_script_line_does_not_start_the_app(self):
        assert 'start ""' in relaunch_lines(EXE, "log")
        text = relaunch_lines(EXE, "log", relaunch=False)
        assert 'start ""' not in text
        assert "not relaunching" in text

    def test_the_handoff_variant_does_not_start_the_app(self):
        assert 'start ""' not in relaunch_lines(
            EXE, "log", "handoff", waiter_required=True, relaunch=False)

    def test_the_legacy_waiter_does_not_start_the_app(self):
        assert 'start "" /normal' in build_relaunch_waiter_script(5, EXE, "log", "handoff")
        text = build_relaunch_waiter_script(5, EXE, "log", "handoff", relaunch=False)
        assert 'start "" /normal' not in text
        assert ":RELAUNCH" in text  # the labels the rest of the script jumps to stay

    def test_the_guarded_waiter_does_not_start_the_app(self):
        args = (5, EXE, "log", "handoff", 30, 0, "done", "powershell.exe", "")
        assert 'start "" /normal' in build_guarded_waiter(*args)
        text = build_guarded_waiter(*args, relaunch=False)
        assert 'start "" /normal' not in text
        assert ":RELAUNCH_CONFIRMED" in text


class _Checker:
    install_staged_on_exit = UpdateOnExitMixin.install_staged_on_exit
    has_staged_update = UpdateOnExitMixin.has_staged_update

    def __init__(self, staged=None):
        self._staged_update = staged
        self._background_version = "9.9"
        self._install_pending = False
        self.installs = []
        self.released = 0

    def _do_install(self, *args, **kwargs):
        self.installs.append((args, kwargs))

    def _release_prompt(self):
        self.released += 1


class TestInstallOnExit:
    def test_nothing_staged_lets_the_normal_quit_go_ahead(self):
        checker = _Checker()
        assert checker.install_staged_on_exit() is False
        assert checker.installs == []

    def test_a_staged_package_installs_without_relaunching(self, tmp_path):
        args = ("9.9", "zip", "sums", "sig", True)
        checker = _Checker((args, str(tmp_path)))
        assert checker.install_staged_on_exit() is True
        assert checker.installs == [(args, {"extracted_dir": str(tmp_path),
                                            "relaunch_app": False})]
        assert checker.has_staged_update() is False
        assert checker._background_version == ""
        # A second Exit does not install the same package twice.
        assert checker.install_staged_on_exit() is False

    def test_a_vanished_package_quits_normally_and_hands_the_claim_back(self, tmp_path):
        checker = _Checker((("9.9", "zip", "", "", False), str(tmp_path / "gone")))
        assert checker.install_staged_on_exit() is False
        assert checker.installs == []
        assert checker.released == 1

    def test_an_install_already_running_is_left_alone(self, tmp_path):
        checker = _Checker((("9.9", "", "", "", False), str(tmp_path)))
        checker._install_pending = True
        assert checker.install_staged_on_exit() is False
        assert checker.installs == []
