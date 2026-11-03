"""Slow start and slow Settings, measured on a real account (2026-10-29).

- 9,465 contacts written back on every start, before the window showed,
  because one contact had a bad name;
- aiosqlite echoing each of them twice at DEBUG (an 850 MB log.log);
- an empty browser version directory, left by a download cut short, that the
  installer refuses to install over: 2.3 s lost on every start;
- the Shortcuts tree rebuilt a second time while Settings was being built."""
import os
import pathlib
import time
from types import SimpleNamespace

from core import browser_payload
from main_window.contacts import ContactsMixin
from ui.dialogs.shortcuts_tab import ShortcutsTabMixin

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_only_the_contacts_cleaned_are_written_back():
    written = []
    window = SimpleNamespace(
        contacts={"a@lid": {"name": "BAD"}, "b@lid": {"name": "Ana"}, "c@lid": {"name": "Bia"}},
        _is_bad_contact_name=lambda value: value == "BAD",
        db=SimpleNamespace(upsert_contacts_batch=written.append))
    ContactsMixin._clean_contacts_cached(window)
    assert written == [{"a@lid": {"name": ""}}]


def test_nothing_is_written_when_no_name_was_bad():
    written = []
    window = SimpleNamespace(contacts={"b@lid": {"name": "Ana"}},
                             _is_bad_contact_name=lambda value: False,
                             db=SimpleNamespace(upsert_contacts_batch=written.append))
    ContactsMixin._clean_contacts_cached(window)
    assert written == []


def test_aiosqlite_is_kept_out_of_the_debug_log():
    source = (ROOT / "client" / "main.py").read_text(encoding="utf-8")
    silenced = source[source.index("for _lib in ("):source.index("setLevel(logging.ERROR)")]
    assert '"aiosqlite"' in silenced


def _age(path, seconds):
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_a_version_directory_with_no_browser_is_found_once_old_enough(tmp_path):
    cache = tmp_path / ".cache"
    empty = cache / "chrome" / "win64-148.0.7778.97"
    empty.mkdir(parents=True)
    good = cache / "chrome" / "win64-147.0.1" / "chrome-win64"
    good.mkdir(parents=True)
    (good / "chrome.exe").write_bytes(b"x")
    fresh = cache / "chrome" / "win64-149.0.0"
    fresh.mkdir(parents=True)
    _age(empty, 3600)
    _age(good.parent, 3600)

    found = browser_payload.version_dirs_without_browser(str(cache), ("chrome.exe",))

    # the good one has its browser; the fresh one may be a download running now
    assert found == [str(empty)]


def test_no_cache_finds_nothing(tmp_path):
    assert browser_payload.version_dirs_without_browser(str(tmp_path / "none"), ("chrome.exe",)) == []


def test_the_empty_directory_is_cleared_before_the_install():
    source = (ROOT / "client" / "main_window" / "wpp_server.py").read_text(encoding="utf-8")
    clear = source.index("browser_payload.version_dirs_without_browser(cache_dir, names)")
    install = source.index('"exec", "puppeteer", "browsers", "install"')
    assert clear < install


def test_hotkey_changes_rebuild_the_shortcuts_tree_once_and_later(monkeypatch):
    import wx
    later = []
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *args: later.append(fn))
    built = []
    tab = SimpleNamespace(_refresh_shortcut_rows=lambda: built.append(1))
    tab._deferred_shortcut_refresh = lambda: ShortcutsTabMixin._deferred_shortcut_refresh(tab)
    event = SimpleNamespace(Skip=lambda: None)
    for _ in range(3):
        ShortcutsTabMixin._shortcut_hotkey_changed(tab, event)
    assert built == [] and len(later) == 1
    later[0]()
    assert built == [1]


def test_the_shortcuts_tree_is_built_once_when_settings_open(monkeypatch):
    """Built with the shortcuts window, after its values are loaded; neither
    the page's own build nor the hotkey field filled by the load rebuild it."""
    import inspect
    import wx
    from ui.dialogs.settings_lazy import LazyUnitsMixin
    build = inspect.getsource(ShortcutsTabMixin._build_shortcuts_page)
    assert "self._refresh_shortcut_rows()" not in build
    ensure = inspect.getsource(LazyUnitsMixin._ensure_unit)
    assert ensure.index("self._load_values(unit)") < ensure.index("self._refresh_shortcut_rows()")
    later = []
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *args: later.append(fn))
    tab = SimpleNamespace(_loading_values=True)
    ShortcutsTabMixin._shortcut_hotkey_changed(tab, SimpleNamespace(Skip=lambda: None))
    assert later == []
