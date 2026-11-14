"""Downloaded updates can be installed later (client/pending_install.py).

The background download of a WinZapp or WPPConnect update ended in a notice
with an OK button only, and both installs take the app or its API down. Now
the notice asks "Install now" / "Install later", and Help > "Install
downloaded update" installs what was left for later."""
from types import SimpleNamespace

import pytest

import pending_install
import update_background
from main_window import wpp_background_update as wbu


class _I18n:
    def t(self, key):
        return key


class _Item:
    def __init__(self):
        self.enabled = None

    def Enable(self, on):
        self.enabled = on


class _Window(SimpleNamespace):
    def __init__(self, **kw):
        kw.setdefault("_update_checker", None)
        kw.setdefault("_pending_wpp_install", None)
        super().__init__(i18n=_I18n(), said=[], item=_Item(), _ID_INSTALL_PENDING=7, **kw)

    def output(self, text, interrupt=False):
        self.said.append(text)

    def GetMenuBar(self):
        return SimpleNamespace(FindItemById=lambda i: self.item if i == 7 else None)


def test_the_menu_item_is_enabled_only_while_something_waits():
    mw = _Window()
    pending_install.refresh_menu(mw)
    assert mw.item.enabled is False
    mw._pending_wpp_install = ("v2", "staged", None)
    pending_install.refresh_menu(mw)
    assert mw.item.enabled is True


def test_later_says_where_to_find_it():
    mw = _Window(_pending_wpp_install=("v2", "staged", None))
    pending_install.said_later(mw)
    assert mw.said == ["update_install_later_said"]
    assert mw.item.enabled is True


def test_nothing_waiting_or_a_bad_moment_installs_nothing():
    mw = _Window()
    assert pending_install.install_pending(mw, may_interrupt=lambda w: True) is False
    mw._pending_wpp_install = ("v2", "staged", None)
    assert pending_install.install_pending(mw, may_interrupt=lambda w: False) is False
    assert mw.said == ["update_install_not_now"]
    assert mw._pending_wpp_install is not None


def test_the_wpp_server_goes_first_and_the_app_after_it(monkeypatch):
    import wx
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *a: fn(*a))
    installed = []
    checker = SimpleNamespace(_pending_install=(("3.1",), "dir"))
    checker.install_pending_update = lambda: installed.append("app") or setattr(
        checker, "_pending_install", None)
    mw = _Window(_update_checker=checker, _pending_wpp_install=("v2", "staged", None))
    mw._install_staged_wpp_now = lambda tag, staged, done: (installed.append(tag), done(True))
    assert pending_install.install_pending(mw, may_interrupt=lambda w: True)
    assert installed == ["v2", "app"]
    assert mw._pending_wpp_install is None and mw.item.enabled is False


# ---------------------------------------------------- the two ready notices
class _Checker(update_background.BackgroundDownloadMixin):
    def __init__(self, mw):
        self._mw = mw
        self._background_version = "3.1"
        self.installed = []

    def _do_install(self, *args, extracted_dir=None):
        self.installed.append((args, extracted_dir))


@pytest.fixture
def ready(monkeypatch):
    monkeypatch.setattr(update_background, "may_interrupt_now", lambda mw: True)
    answers = []
    monkeypatch.setattr(pending_install, "ask_install_now",
                        lambda mw, text, title: answers.pop(0))
    return answers


def test_app_update_later_keeps_the_package_and_the_menu_installs_it(ready):
    mw = _Window(_shutting_down=False)
    checker = _Checker(mw)
    mw._update_checker = checker
    ready.append(False)
    checker._install_downloaded_update(("3.1", "url"), "extracted")
    assert checker.installed == []
    assert checker._pending_install == (("3.1", "url"), "extracted")
    assert checker._background_version == "3.1"      # no second download meanwhile
    assert pending_install.has_pending(mw)
    assert checker.install_pending_update() is True
    assert checker.installed == [(("3.1", "url"), "extracted")]
    assert checker._pending_install is None


def test_app_update_now_installs_at_once(ready):
    mw = _Window(_shutting_down=False)
    checker = _Checker(mw)
    ready.append(True)
    checker._install_downloaded_update(("3.1", "url"), "extracted")
    assert checker.installed == [(("3.1", "url"), "extracted")]


class _Wpp(wbu.WppBackgroundUpdateMixin):
    def __init__(self):
        self.i18n = _I18n()
        self.said = []
        self.updated = []
        self._wpp_staging = {"tag": "v2", "dialog": None}
        self._pending_wpp_install = None

    def output(self, text, interrupt=False):
        self.said.append(text)

    def GetMenuBar(self):
        return None

    def _update_wpp_server(self, tag, on_finished=None, staged_dir=None):
        self.updated.append((tag, staged_dir))
        return True


def test_wpp_update_later_keeps_the_build_and_blocks_a_second_one(monkeypatch, ready):
    monkeypatch.setattr(wbu, "may_interrupt_now", lambda mw: True)
    w = _Wpp()
    ready.append(False)
    w._install_staged_wpp_when_ready("v2", "staged", None)
    assert w.updated == []
    assert w._pending_wpp_install == ("v2", "staged", None)
    assert w._wpp_staging            # a new build may not sweep the waiting one
    w._install_staged_wpp_now("v2", "staged", None)
    assert w.updated == [("v2", "staged")]
    assert w._wpp_staging is None and w._pending_wpp_install is None
