"""What the periodic WPPConnect Server check offers: the pin, or an approved release.

WinZapp's patches follow the server by hand, so the newest upstream release is
never offered. The check knows two versions: the bundled pin
(client/wpp_minimum_version.txt) and the newest release the maintainers
approved for that pin (client/wpp_approved_versions.json, read from main). An
installed server below the pin is the mandatory kind; one at or above it but
behind an approved release is the recommended kind, which a No silences for
the session.
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
def test_offer_without_an_approved_release(installed, minimum, expected):
    assert wpp_update_target(installed, minimum) == expected
    assert wpp_update_target(installed, minimum, "") == expected


@pytest.mark.parametrize("installed, expected", [
    ("2.10.16", ("v2.10.18", False)),   # at the pin, approved is newer: recommended
    ("2.10.17", ("v2.10.18", False)),
    ("2.10.18", None),                  # already on the approved release
    ("2.10.20", None),                  # ahead of it: never moved back
    ("2.10.10", ("v2.10.18", True)),    # below the pin: straight to the approved one
])
def test_offer_with_an_approved_release(installed, expected):
    assert wpp_update_target(installed, "v2.10.16", "v2.10.18") == expected


def test_an_approved_release_not_newer_than_the_pin_is_ignored():
    assert wpp_update_target("2.10.10", "v2.10.16", "v2.10.14") == ("v2.10.16", True)
    assert wpp_update_target("2.10.16", "v2.10.16", "v2.10.16") is None


@pytest.mark.parametrize("installed, minimum", [
    ("not-a-version", "v2.10.16"),
    ("", "v2.10.16"),
    ("2.10.10", ""),          # no pin bundled (a dev checkout)
    ("2.10.10", "garbage"),
])
def test_unparseable_versions_never_prompt(installed, minimum):
    assert wpp_update_target(installed, minimum, "v2.10.30") is None


def test_unparseable_approved_release_falls_back_to_the_pin():
    assert wpp_update_target("2.10.10", "v2.10.16", "garbage") == ("v2.10.16", True)
    assert wpp_update_target("2.10.16", "v2.10.16", "garbage") is None


@pytest.mark.parametrize("requirement, expected", [
    ("1.0.0.0", True),
    ("999.0.0.0", False),
    ("not-a-version", False),   # a typo in the list must not offer to everyone
])
def test_min_winzapp_requirement(monkeypatch, requirement, expected):
    monkeypatch.setattr(updater, "__version__", "2.1.0.4391")
    assert updater._winzapp_satisfies(requirement) is expected


class _MW:
    def __init__(self, installed):
        self._installed = installed
        self.i18n = type("I", (), {"t": staticmethod(lambda key: key)})()

    def _get_installed_wpp_version(self):
        return self._installed


def _checker(monkeypatch, installed, minimum, approved=("", True)):
    monkeypatch.setattr(updater, "homologated_wpp_tag", lambda _p: minimum)
    prompts, retries = [], []
    monkeypatch.setattr(updater.wx, "CallAfter", lambda fn, *a: prompts.append(a))
    checker = WppUpdateChecker.__new__(WppUpdateChecker)
    checker._mw = _MW(installed)
    checker._retry_timer = None
    checker._prompt_token = None
    checker._declined_tag = None
    checker._approved_tag = lambda pin: approved
    checker._other_account_busy = lambda: False
    checker._schedule_retry = lambda interval=None: retries.append(interval)
    return checker, prompts, retries


def test_the_check_never_asks_github_for_the_newest_release():
    import ui.dialogs.api_setup as api_setup
    assert not hasattr(api_setup, "fetch_latest_wpp_tag")


def test_below_the_pin_the_required_update_is_offered(monkeypatch):
    checker, prompts, _ = _checker(monkeypatch, "2.10.10", "v2.10.16")
    checker._check_once()
    assert prompts == [("2.10.10", "2.10.16", "v2.10.16", True)]


def test_an_approved_release_is_recommended(monkeypatch):
    checker, prompts, _ = _checker(monkeypatch, "2.10.16", "v2.10.16", ("v2.10.18", True))
    checker._check_once()
    assert prompts == [("2.10.16", "2.10.18", "v2.10.18", False)]


def test_nothing_approved_checks_again_later(monkeypatch):
    checker, prompts, retries = _checker(monkeypatch, "2.10.16", "v2.10.16")
    checker._check_once()
    assert prompts == []
    assert retries == [None]   # the list can grow while WinZapp runs


def test_an_unreachable_list_is_read_again_sooner(monkeypatch):
    checker, prompts, retries = _checker(monkeypatch, "2.10.16", "v2.10.16", ("", False))
    checker._check_once()
    assert prompts == []
    assert retries == [WppUpdateChecker._ERROR_RETRY_INTERVAL]


def test_an_unreachable_list_still_offers_the_pin(monkeypatch):
    checker, prompts, _ = _checker(monkeypatch, "2.10.10", "v2.10.16", ("", False))
    checker._check_once()
    assert prompts == [("2.10.10", "2.10.16", "v2.10.16", True)]


def test_a_server_ahead_of_everything_is_not_touched(monkeypatch):
    checker, prompts, _ = _checker(monkeypatch, "2.10.20", "v2.10.16", ("v2.10.18", True))
    checker._check_once()
    assert prompts == []


def test_without_a_pin_nothing_is_fetched_or_offered(monkeypatch):
    checker, prompts, retries = _checker(monkeypatch, "2.10.10", "")
    checker._approved_tag = lambda pin: pytest.fail("no pin, no lookup")
    checker._check_once()
    assert prompts == [] and retries == []


def test_a_required_update_is_asked_again_after_a_no(monkeypatch):
    checker, prompts, _ = _checker(monkeypatch, "2.10.10", "v2.10.16")
    checker._declined_tag = "v2.10.16"
    checker._check_once()
    assert len(prompts) == 1


def test_a_required_update_whose_approved_target_failed_offers_the_pin(monkeypatch):
    checker, prompts, _ = _checker(monkeypatch, "2.10.10", "v2.10.16", ("v2.10.18", True))
    checker._declined_tag = "v2.10.18"
    checker._check_once()
    assert prompts == [("2.10.10", "2.10.16", "v2.10.16", True)]


def test_a_failed_pin_is_still_asked_again(monkeypatch):
    checker, prompts, _ = _checker(monkeypatch, "2.10.10", "v2.10.16", ("", True))
    checker._declined_tag = "v2.10.16"
    checker._check_once()
    assert prompts == [("2.10.10", "2.10.16", "v2.10.16", True)]


def _force_reinstall(monkeypatch, pending_timer):
    calls, retries, started = [], [], []
    monkeypatch.setattr(updater.wx, "CallAfter", lambda fn, *a: calls.append(a))
    monkeypatch.setattr(updater.threading, "Thread",
                        lambda target, daemon: type("T", (), {"start": lambda s: started.append(target)})())
    checker = WppUpdateChecker.__new__(WppUpdateChecker)
    checker._stopped = False
    checker._resume_periodic = False
    checker._retry_timer = type("Timer", (), {"cancel": lambda s: None})() if pending_timer else None
    checker._reinstall_tag = lambda: "v2.10.18"
    checker._schedule_retry = lambda interval=None: retries.append(interval)
    checker.force_reinstall()
    started[0]()   # the worker thread's body
    assert calls == [("2.10.18", "v2.10.18")]
    return retries


def test_force_reinstall_puts_back_the_periodic_check_it_cancelled(monkeypatch):
    assert _force_reinstall(monkeypatch, pending_timer=True) == [None]


def test_force_reinstall_starts_no_periodic_check_of_its_own(monkeypatch):
    """With updates turned off the checker exists only for Force Reinstall;
    a 12 h timer would start prompting a user who disabled them."""
    assert _force_reinstall(monkeypatch, pending_timer=False) == []


def test_a_declined_approved_release_is_not_asked_again(monkeypatch):
    checker, prompts, retries = _checker(monkeypatch, "2.10.16", "v2.10.16", ("v2.10.18", True))
    checker._declined_tag = "v2.10.18"
    checker._check_once()
    assert prompts == [] and retries == [None]


def test_a_newer_approved_release_is_asked_after_declining_an_older_one(monkeypatch):
    checker, prompts, _ = _checker(monkeypatch, "2.10.16", "v2.10.16", ("v2.10.19", True))
    checker._declined_tag = "v2.10.18"
    checker._check_once()
    assert len(prompts) == 1


def test_another_accounts_running_server_defers_the_offer(monkeypatch):
    checker, prompts, retries = _checker(monkeypatch, "2.10.10", "v2.10.16")
    checker._other_account_busy = lambda: True
    checker._check_once()
    assert prompts == [] and retries == [None]


def _prompt(monkeypatch, answer, required=True, tag="v2.10.16"):
    shown = []

    def _box(mw, message, title, style, announce=None):
        shown.append((message, title))
        return answer

    monkeypatch.setattr(updater, "message_box", _box)
    mw = type("MW", (), {})()
    mw.i18n = type("I", (), {"t": lambda self, key: key + " {current}"})()
    mw.wpp_update_may_run_now = lambda: True
    mw.updated = []
    mw._update_wpp_server = lambda tag, on_finished=None: mw.updated.append(tag) or True
    checker = WppUpdateChecker.__new__(WppUpdateChecker)
    checker._mw = mw
    checker._retry_timer = None
    checker._prompt_token = None
    checker._declined_tag = None
    checker._other_account_busy = lambda: False
    checker._schedule_retry = lambda interval=None: None
    checker._prompt_update("2.10.10", tag.lstrip("v"), tag, required)
    return checker, shown, mw


def test_the_required_prompt_uses_the_mandatory_wording(monkeypatch):
    _, shown, _ = _prompt(monkeypatch, updater.wx.NO)
    assert shown[0][1].startswith("api_update_outdated_title")
    assert shown[0][0].startswith("api_update_outdated_message")


def test_the_recommended_prompt_uses_its_own_wording(monkeypatch):
    _, shown, _ = _prompt(monkeypatch, updater.wx.NO, required=False, tag="v2.10.18")
    assert shown[0][1].startswith("wpp_update_available_title")
    assert shown[0][0].startswith("wpp_update_available_msg")


def test_saying_no_installs_nothing(monkeypatch):
    checker, _, mw = _prompt(monkeypatch, updater.wx.NO)
    assert mw.updated == []
    assert checker._declined_tag is None   # a required update is never silenced


def test_saying_no_to_a_recommended_update_remembers_it(monkeypatch):
    checker, _, mw = _prompt(monkeypatch, updater.wx.NO, required=False, tag="v2.10.18")
    assert mw.updated == []
    assert checker._declined_tag == "v2.10.18"


def test_yes_installs_the_offered_tag(monkeypatch):
    _, _, mw = _prompt(monkeypatch, updater.wx.YES)
    assert mw.updated == ["v2.10.16"]
    _, _, mw = _prompt(monkeypatch, updater.wx.YES, required=False, tag="v2.10.18")
    assert mw.updated == ["v2.10.18"]


def test_a_failed_update_is_not_retried_this_session(monkeypatch):
    checker = WppUpdateChecker.__new__(WppUpdateChecker)
    checker._declined_tag = None
    checker._release_prompt = lambda: None
    checker._schedule_retry = lambda interval=None: None
    checker._update_finished("v2.10.18", False)
    assert checker._declined_tag == "v2.10.18"


def test_force_reinstall_falls_back_to_the_pin(monkeypatch):
    monkeypatch.setattr(updater, "homologated_wpp_tag", lambda _p: "v2.10.18")
    assert WppUpdateChecker._pinned_tag() == "v2.10.18"


def test_force_reinstall_without_a_pin_has_no_tag(monkeypatch):
    monkeypatch.setattr(updater, "homologated_wpp_tag", lambda _p: "")
    checker = WppUpdateChecker.__new__(WppUpdateChecker)
    assert checker._reinstall_tag() == ""


def test_the_approved_lookup_never_raises(monkeypatch):
    from core import wpp_approved

    def _boom(*a, **k):
        raise OSError("offline")

    monkeypatch.setattr(wpp_approved, "fetch_manifest", _boom)
    assert WppUpdateChecker._approved_tag("v2.10.16") == ("", False)


def test_the_approved_lookup_selects_for_the_pin(monkeypatch):
    from core import wpp_approved
    manifest = {"schema": 1, "approved": [
        {"version": "2.10.18", "min_pin": "2.10.16"},
        {"version": "2.10.25", "min_pin": "2.10.20"},
    ]}
    monkeypatch.setattr(wpp_approved, "fetch_manifest", lambda repo, ua: manifest)
    assert WppUpdateChecker._approved_tag("v2.10.16") == ("v2.10.18", True)
    assert WppUpdateChecker._approved_tag("v2.10.21") == ("v2.10.25", True)
    assert WppUpdateChecker._approved_tag("") == ("", True)
