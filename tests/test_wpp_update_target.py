"""What the periodic WPPConnect Server check offers: only the pinned version.

WinZapp's patches follow the server by hand, so a release newer than the
bundled pin (client/wpp_minimum_version.txt) is one it was never validated
against. The check asks nobody but the pin: it never reaches GitHub and never
recommends an optional update. The pin moves only when a WinZapp release
raises it, and an installed server below it is the mandatory kind.
"""

import pytest

import updater
from updater import WppUpdateChecker, wpp_update_target


@pytest.mark.parametrize("installed, minimum, expected", [
    ("2.10.10", "v2.10.16", ("v2.10.16", True)),   # below the pin: required
    ("2.10.15", "2.10.16", ("2.10.16", True)),
    ("2.10.16", "v2.10.16", None),                 # at the pin
    ("2.10.20", "v2.10.16", None),                 # ahead of the pin: left alone
])
def test_offer(installed, minimum, expected):
    assert wpp_update_target(installed, minimum) == expected


@pytest.mark.parametrize("installed, minimum", [
    ("not-a-version", "v2.10.16"),
    ("", "v2.10.16"),
    ("2.10.10", ""),          # no pin bundled (a dev checkout)
    ("2.10.10", "garbage"),
])
def test_unparseable_versions_never_prompt(installed, minimum):
    assert wpp_update_target(installed, minimum) is None


class _MW:
    def __init__(self, installed):
        self._installed = installed
        self.i18n = type("I", (), {"t": staticmethod(lambda key: key)})()

    def _get_installed_wpp_version(self):
        return self._installed


def _checker(monkeypatch, installed, minimum):
    monkeypatch.setattr(updater, "homologated_wpp_tag", lambda _p: minimum)
    prompts, retries = [], []
    monkeypatch.setattr(updater.wx, "CallAfter", lambda fn, *a: prompts.append(a))
    checker = WppUpdateChecker.__new__(WppUpdateChecker)
    checker._mw = _MW(installed)
    checker._retry_timer = None
    checker._prompt_token = None
    checker._other_account_busy = lambda: False
    checker._schedule_retry = lambda interval=None: retries.append(interval)
    return checker, prompts, retries


def test_the_check_never_asks_github(monkeypatch):
    import ui.dialogs.api_setup as api_setup
    assert not hasattr(api_setup, "fetch_latest_wpp_tag")
    checker, prompts, _ = _checker(monkeypatch, "2.10.10", "v2.10.16")
    checker._check_once()
    assert prompts == [("2.10.10", "2.10.16", "v2.10.16")]


def test_a_newer_upstream_release_is_not_offered(monkeypatch):
    checker, prompts, retries = _checker(monkeypatch, "2.10.16", "v2.10.16")
    checker._check_once()
    assert prompts == []
    assert retries == []   # nothing can change until WinZapp is updated


def test_a_server_ahead_of_the_pin_is_not_touched(monkeypatch):
    checker, prompts, retries = _checker(monkeypatch, "2.10.20", "v2.10.16")
    checker._check_once()
    assert prompts == [] and retries == []


def test_a_required_update_is_asked_again_after_a_no(monkeypatch):
    checker, prompts, _ = _checker(monkeypatch, "2.10.10", "v2.10.16")
    checker._check_once()
    checker._check_once()
    assert len(prompts) == 2


def test_another_accounts_running_server_defers_the_offer(monkeypatch):
    checker, prompts, retries = _checker(monkeypatch, "2.10.10", "v2.10.16")
    checker._other_account_busy = lambda: True
    checker._check_once()
    assert prompts == [] and retries == [None]


def _prompt(monkeypatch, answer):
    shown = []

    def _box(mw, message, title, style, announce=None):
        shown.append((message, title))
        return answer

    monkeypatch.setattr(updater, "message_box", _box)
    mw = type("MW", (), {})()
    mw.i18n = type("I", (), {"t": lambda self, key: key + " {current} {required}"})()
    mw.wpp_update_may_run_now = lambda: True
    mw.updated = []
    mw._update_wpp_server = lambda tag, on_finished=None: mw.updated.append(tag) or True
    checker = WppUpdateChecker.__new__(WppUpdateChecker)
    checker._mw = mw
    checker._retry_timer = None
    checker._prompt_token = None
    checker._other_account_busy = lambda: False
    checker._schedule_retry = lambda interval=None: None
    checker._prompt_update("2.10.10", "2.10.16", "v2.10.16")
    return checker, shown, mw


def test_the_prompt_uses_the_mandatory_wording(monkeypatch):
    _, shown, _ = _prompt(monkeypatch, updater.wx.NO)
    assert shown[0][1].startswith("api_update_outdated_title")
    assert shown[0][0].startswith("api_update_outdated_message")


def test_saying_no_installs_nothing(monkeypatch):
    _, _, mw = _prompt(monkeypatch, updater.wx.NO)
    assert mw.updated == []


def test_yes_installs_the_pinned_tag(monkeypatch):
    _, _, mw = _prompt(monkeypatch, updater.wx.YES)
    assert mw.updated == ["v2.10.16"]


def test_force_reinstall_uses_the_pin_not_the_newest_release(monkeypatch):
    monkeypatch.setattr(updater, "homologated_wpp_tag", lambda _p: "v2.10.18")
    assert WppUpdateChecker._pinned_tag() == "v2.10.18"


def test_force_reinstall_without_a_pin_has_no_tag(monkeypatch):
    monkeypatch.setattr(updater, "homologated_wpp_tag", lambda _p: "")
    assert WppUpdateChecker._pinned_tag() == ""
