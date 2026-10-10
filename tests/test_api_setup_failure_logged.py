"""A failed server install/build used to be shown to the user and never logged,
so a newer wppconnect-server release that no longer compiles against
WinZapp's overwritten source files left no trace in the log to diagnose."""

import logging
import sys
from types import SimpleNamespace

from ui.dialogs import api_setup


class _Timer:
    def Stop(self):
        pass


class _I18n:
    def t(self, key):
        return key


class _Dialog:
    _finish_error = api_setup.ApiSetupDialog._finish_error

    def __init__(self):
        self._cancelled = False
        self._finished = False
        self._timer = _Timer()
        self._i18n = _I18n()
        self.ended = []

    def _is_modal_active(self):
        return True

    def _end_modal_safely(self, code):
        self.ended.append(code)


def test_the_build_error_is_logged_and_shown(monkeypatch, caplog):
    boxes = []
    monkeypatch.setattr(api_setup, "show_error_details", lambda *a: boxes.append(a))
    dlg = _Dialog()

    with caplog.at_level(logging.ERROR):
        dlg._finish_error("tsc: error TS2345")

    assert "tsc: error TS2345" in caplog.text
    assert len(boxes) == 1
    assert boxes[0][-1] == "tsc: error TS2345"
    assert dlg.ended == [api_setup.wx.ID_CANCEL]


def test_staged_failure_preserves_details_without_a_duplicate_dialog(monkeypatch):
    monkeypatch.setattr(api_setup, "show_error_details",
                        lambda *a: (_ for _ in ()).throw(AssertionError("duplicate dialog")))
    dlg = _Dialog()
    dlg._api_dir = "api_staging"
    dlg._finish_error("npm error ETARGET")
    assert dlg._error_details == "npm error ETARGET"
    assert dlg.ended == [api_setup.wx.ID_CANCEL]


def test_failed_compiler_output_is_preserved_from_both_streams():
    # TypeScript reports diagnostics on stdout, while npm reports on stderr.
    # Call the worker on a plain stub; no dialog or desktop window is created.
    worker = SimpleNamespace(_cancelled=False)
    ok, details = api_setup.ApiSetupDialog._run_subprocess(worker, [
        sys.executable, "-c",
        "import sys; print('error TS2345', flush=True); "
        "print('npm build failed', file=sys.stderr, flush=True); sys.exit(1)",
    ])
    assert not ok
    assert "error TS2345" in details
    assert "npm build failed" in details
