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
init_UI() that issue #104 hit), and this function reads it. Without one it
uses the install-wide language (startup_i18n), and only as a last resort a
hardcoded English text.
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


class _GlobalI18n:
    """Stands in for startup_i18n(): the install-wide language from app.json."""

    def t(self, key):
        return {
            "startup_critical_title": "GLOBAL TITLE",
            "startup_critical_message": "GLOBAL {path} {details}",
        }[key]


def _no_global_i18n():
    raise RuntimeError("app.json unreadable")


def test_falls_back_to_the_global_language_when_no_partial_frame(monkeypatch):
    patch_main_global(monkeypatch, "_last_partial_frame", None)
    patch_main_global(monkeypatch, "_startup_i18n", _GlobalI18n)

    title, message = main._startup_critical_error_text("C:\\crash.log", "traceback text")

    assert title == "GLOBAL TITLE"
    assert message == "GLOBAL C:\\crash.log traceback text"


def test_falls_back_when_the_partial_frame_has_no_i18n_yet(monkeypatch):
    frame = type("F", (), {})()  # crashed before self.i18n was ever set
    patch_main_global(monkeypatch, "_last_partial_frame", frame)
    patch_main_global(monkeypatch, "_startup_i18n", _GlobalI18n)

    title, message = main._startup_critical_error_text("C:\\crash.log", "tb")

    assert title == "GLOBAL TITLE"


def test_falls_back_when_translation_itself_raises(monkeypatch):
    """The crash dialog must never itself crash trying to be helpful."""
    frame = type("F", (), {"i18n": _BrokenI18n()})()
    patch_main_global(monkeypatch, "_last_partial_frame", frame)
    patch_main_global(monkeypatch, "_startup_i18n", _GlobalI18n)

    title, message = main._startup_critical_error_text("C:\\crash.log", "tb")

    assert title == "GLOBAL TITLE"
    assert "C:\\crash.log" in message


def test_last_resort_is_hardcoded_english_when_no_i18n_works(monkeypatch):
    patch_main_global(monkeypatch, "_last_partial_frame", None)
    patch_main_global(monkeypatch, "_startup_i18n", _no_global_i18n)

    title, message = main._startup_critical_error_text("C:\\crash.log", "traceback text")

    assert title == "WinZapp — Startup error"
    assert "C:\\crash.log" in message
    assert "traceback text" in message


def test_crash_path_and_traceback_are_both_embedded(monkeypatch):
    frame = type("F", (), {"i18n": _FakeI18n()})()
    patch_main_global(monkeypatch, "_last_partial_frame", frame)

    long_tb = "x" * 2000
    _title, message = main._startup_critical_error_text("C:\\crash.log", long_tb)

    # Mirrors __main__'s own tb[:800] truncation for the hardcoded fallback.
    assert message.count("x") == 800
