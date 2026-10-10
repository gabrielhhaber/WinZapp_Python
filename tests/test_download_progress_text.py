"""Issue #93 — a downloading document row shows percentage and bytes so far."""

import types

from ui.conversation_panel.formatting import FormattingMixin


class _I18n:
    _T = {
        "decimal_separator": ".",
        "downloading_progress": "downloading, {pct}% done",
        "downloading_progress_size": "downloading, {pct}% ({done} of {total})",
    }

    def t(self, key):
        return self._T[key]


def _stub():
    s = types.SimpleNamespace(main_window=types.SimpleNamespace(i18n=_I18n()))
    s._format_filesize = types.MethodType(FormattingMixin._format_filesize, s)
    return s


def test_percent_and_bytes():
    total = int(39.4 * 1024 ** 2)
    text = FormattingMixin._download_progress_text(_stub(), 0.35, total)
    assert text == "downloading, 35% (13.8 mb of 39.4 mb)"


def test_unknown_size_falls_back_to_percent_only():
    for total in (None, 0, "x"):
        assert FormattingMixin._download_progress_text(_stub(), 0.5, total) == (
            "downloading, 50% done")
