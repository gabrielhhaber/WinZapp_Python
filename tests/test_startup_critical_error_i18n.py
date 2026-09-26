"""Tests for main._startup_critical_error_text() — the native MessageBoxW
shown by __main__'s top-level except-block on a startup crash (e.g. issue
#104: a missing constructor argument deep inside a sub-panel's init_UI()).

Reported live: this dialog was always hardcoded to Portuguese, even for a
user running WinZapp in another language — despite startup_critical_title/
startup_critical_message already existing in all 5 language files, unused.
The reason: `frame = MainWindow(...)` in __main__ never completes assigning
`frame` when the constructor itself raises, so the except-block had no
object to read a language preference from. main.py now publishes the
partially-built instance to a module-level `_last_partial_frame` the moment
self.i18n exists (early in __init__, well before the kind of failure in
init_UI() that issue #104 hit), and this function reads it.
"""

import main
from tests.god_modules import patch_main_global


class _FakeI18n:
    _STRINGS = {
        "startup_critical_title": "WinZapp — Erro de inicialização",
        "startup_critical_message": "Detalhes: {path}\n{details}",
    }

    def t(self, key):
        return self._STRINGS[key]


class _BrokenI18n:
    def t(self, key):
        raise RuntimeError("broken translation")


def test_uses_the_partial_frames_language_when_available(monkeypatch):
    frame = type("F", (), {"i18n": _FakeI18n()})()
    patch_main_global(monkeypatch, "_last_partial_frame", frame)

    title, message = main._startup_critical_error_text("C:\\crash.log", "traceback text")

    assert title == "WinZapp — Erro de inicialização"
    assert message == "Detalhes: C:\\crash.log\ntraceback text"


def test_falls_back_to_hardcoded_portuguese_when_no_partial_frame(monkeypatch):
    patch_main_global(monkeypatch, "_last_partial_frame", None)

    title, message = main._startup_critical_error_text("C:\\crash.log", "traceback text")

    assert title == "WinZapp — Erro de inicialização"
    assert "C:\\crash.log" in message
    assert "traceback text" in message


def test_falls_back_when_the_partial_frame_has_no_i18n_yet(monkeypatch):
    frame = type("F", (), {})()  # crashed before self.i18n was ever set
    patch_main_global(monkeypatch, "_last_partial_frame", frame)

    title, message = main._startup_critical_error_text("C:\\crash.log", "tb")

    assert title == "WinZapp — Erro de inicialização"


def test_falls_back_when_translation_itself_raises(monkeypatch):
    """The crash dialog must never itself crash trying to be helpful."""
    frame = type("F", (), {"i18n": _BrokenI18n()})()
    patch_main_global(monkeypatch, "_last_partial_frame", frame)

    title, message = main._startup_critical_error_text("C:\\crash.log", "tb")

    assert title == "WinZapp — Erro de inicialização"
    assert "C:\\crash.log" in message


def test_crash_path_and_traceback_are_both_embedded(monkeypatch):
    frame = type("F", (), {"i18n": _FakeI18n()})()
    patch_main_global(monkeypatch, "_last_partial_frame", frame)

    long_tb = "x" * 2000
    _title, message = main._startup_critical_error_text("C:\\crash.log", long_tb)

    # Mirrors __main__'s own tb[:800] truncation for the hardcoded fallback.
    assert message.count("x") == 800
