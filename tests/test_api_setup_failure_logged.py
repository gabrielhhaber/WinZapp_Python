"""A failed server install/build used to be shown to the user and never logged,
so a newer wppconnect-server release that no longer compiles against
WinZapp's overwritten source files left no trace in the log to diagnose."""

import logging

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
    monkeypatch.setattr(api_setup.wx, "MessageBox", lambda *a: boxes.append(a))
    dlg = _Dialog()

    with caplog.at_level(logging.ERROR):
        dlg._finish_error("tsc: error TS2345")

    assert "tsc: error TS2345" in caplog.text
    assert len(boxes) == 1
    assert dlg.ended == [api_setup.wx.ID_CANCEL]
