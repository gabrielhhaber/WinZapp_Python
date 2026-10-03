"""What the periodic WPPConnect Server check offers.

The bundled minimum (client/wpp_minimum_version.txt) is the floor of the
offer, not its ceiling. It used to be the only thing the periodic check
compared against, so a user was never told about a newer server until a WinZapp
release raised the minimum (and a background start never ran the check at all).
"""

import pytest

import updater
from updater import WppUpdateChecker, wpp_update_target


@pytest.mark.parametrize("installed, minimum, latest, expected", [
    # latest > installed >= minimum: the newer release, as a recommendation
    ("2.10.16", "v2.10.16", "v2.10.20", ("v2.10.20", False)),
    ("2.10.18", "v2.10.16", "v2.10.20", ("v2.10.20", False)),
    # installed < minimum: required, and the target is at least the minimum
    ("2.10.10", "v2.10.16", "v2.10.20", ("v2.10.20", True)),
    ("2.10.10", "v2.10.16", "", ("v2.10.16", True)),          # GitHub unreachable
    ("2.10.10", "v2.10.16", "v2.10.12", ("v2.10.16", True)),  # latest < minimum
    # nothing to offer
    ("2.10.16", "v2.10.16", "", None),                         # offline, up to minimum
    ("2.10.20", "v2.10.16", "v2.10.20", None),                 # installed == latest
    ("2.10.22", "v2.10.16", "v2.10.20", None),                 # installed ahead
    ("2.10.18", "v2.10.16", "v2.10.12", None),                 # latest < minimum, installed above
    # no minimum bundled (dev checkout): latest only, never "required"
    ("2.10.10", "", "v2.10.12", ("v2.10.12", False)),
    ("2.10.10", "", "", None),
])
def test_offer(installed, minimum, latest, expected):
    assert wpp_update_target(installed, minimum, latest) == expected


@pytest.mark.parametrize("installed, minimum, latest", [
    ("not-a-version", "v2.10.16", "v2.10.20"),
    ("", "v2.10.16", "v2.10.20"),
    ("2.10.16", "v2.10.16", "latest"),
    ("2.10.16", "v2.10.16", "v2.x"),
])
def test_unparseable_versions_never_prompt(installed, minimum, latest):
    assert wpp_update_target(installed, minimum, latest) is None


def test_an_unparseable_minimum_is_ignored_not_trusted():
    assert wpp_update_target("2.10.10", "garbage", "v2.10.12") == ("v2.10.12", False)


class _MW:
    def __init__(self, installed):
        self._installed = installed
        self.i18n = type("I", (), {"t": staticmethod(lambda key: key)})()

    def _get_installed_wpp_version(self):
        return self._installed


def _checker(monkeypatch, installed, minimum, latest):
    monkeypatch.setattr(updater, "homologated_wpp_tag", lambda _p: minimum)
    monkeypatch.setattr("ui.dialogs.api_setup.fetch_latest_wpp_tag", lambda: latest)
    prompts, retries = [], []
    monkeypatch.setattr(updater.wx, "CallAfter", lambda fn, *a: prompts.append(a))
    checker = WppUpdateChecker.__new__(WppUpdateChecker)
    checker._mw = _MW(installed)
    checker._retry_timer = None
    checker._schedule_retry = lambda interval=None: retries.append(interval)
    return checker, prompts, retries


def test_a_recommended_update_reaches_the_prompt_unflagged(monkeypatch):
    checker, prompts, _ = _checker(monkeypatch, "2.10.16", "v2.10.16", "v2.10.20")
    checker._check_once()
    assert prompts == [("2.10.16", "2.10.20", "v2.10.20", False)]


def test_a_server_below_the_minimum_reaches_the_prompt_as_required(monkeypatch):
    checker, prompts, _ = _checker(monkeypatch, "2.10.10", "v2.10.16", "")
    checker._check_once()
    assert prompts == [("2.10.10", "2.10.16", "v2.10.16", True)]


def test_offline_with_nothing_to_offer_retries_soon(monkeypatch):
    checker, prompts, retries = _checker(monkeypatch, "2.10.16", "v2.10.16", "")
    checker._check_once()
    assert prompts == []
    assert retries == [WppUpdateChecker._OFFLINE_RETRY_INTERVAL]
    assert WppUpdateChecker._OFFLINE_RETRY_INTERVAL < WppUpdateChecker._RETRY_INTERVAL


def test_up_to_date_waits_the_normal_interval(monkeypatch):
    checker, prompts, retries = _checker(monkeypatch, "2.10.20", "v2.10.16", "v2.10.20")
    checker._check_once()
    assert prompts == [] and retries == [None]


def test_a_declined_recommendation_is_not_asked_again_this_session(monkeypatch):
    checker, prompts, retries = _checker(monkeypatch, "2.10.16", "v2.10.16", "v2.10.20")
    checker._declined_tag = "v2.10.20"
    checker._check_once()
    assert prompts == [] and retries == [None]


def test_a_newer_release_than_the_declined_one_is_offered(monkeypatch):
    checker, prompts, _ = _checker(monkeypatch, "2.10.16", "v2.10.16", "v2.10.21")
    checker._declined_tag = "v2.10.20"
    checker._check_once()
    assert len(prompts) == 1


def test_a_required_update_is_never_silenced_by_an_earlier_no(monkeypatch):
    checker, prompts, _ = _checker(monkeypatch, "2.10.10", "v2.10.16", "")
    checker._declined_tag = "v2.10.16"
    checker._check_once()
    assert len(prompts) == 1


def _prompt(monkeypatch, required, answer):
    shown = []

    def _box(mw, message, title, style, announce=None):
        shown.append((message, title))
        return answer

    monkeypatch.setattr(updater, "message_box", _box)
    mw = type("MW", (), {})()
    mw.i18n = type("I", (), {"t": lambda self, key: key + (
        " {current} {required}" if key.startswith("api_") else " {current} {new}")})()
    mw.wpp_update_may_run_now = lambda: True
    mw.updated = []
    mw._update_wpp_server = mw.updated.append
    checker = WppUpdateChecker.__new__(WppUpdateChecker)
    checker._mw = mw
    checker._retry_timer = None
    checker._schedule_retry = lambda interval=None: None
    checker._prompt_update("2.10.10", "2.10.16", "v2.10.16", required)
    return checker, shown, mw


def test_the_two_kinds_use_different_wording(monkeypatch):
    _, optional, _ = _prompt(monkeypatch, False, updater.wx.NO)
    _, mandatory, _ = _prompt(monkeypatch, True, updater.wx.NO)
    assert optional[0][1].startswith("wpp_update_available_title")
    assert mandatory[0][1].startswith("api_update_outdated_title")
    assert optional[0][0].startswith("wpp_update_available_msg")
    assert mandatory[0][0].startswith("api_update_outdated_message")


def test_saying_no_to_a_recommendation_remembers_it(monkeypatch):
    checker, _, mw = _prompt(monkeypatch, False, updater.wx.NO)
    assert checker._declined_tag == "v2.10.16" and mw.updated == []


def test_saying_no_to_a_required_update_remembers_nothing(monkeypatch):
    checker, _, _ = _prompt(monkeypatch, True, updater.wx.NO)
    assert getattr(checker, "_declined_tag", None) is None


def test_yes_installs_the_offered_tag(monkeypatch):
    _, _, mw = _prompt(monkeypatch, False, updater.wx.YES)
    assert mw.updated == ["v2.10.16"]


def test_force_reinstall_still_never_downgrades(monkeypatch):
    monkeypatch.setattr(updater, "homologated_wpp_tag", lambda _p: "v2.10.18")
    monkeypatch.setattr("ui.dialogs.api_setup.fetch_latest_wpp_tag", lambda: "v2.10.16")
    assert WppUpdateChecker._newest_available_tag() == "v2.10.18"
