"""Alpha opt-out stays effective; manual one-time consent uses normal verification."""
from types import SimpleNamespace

import pytest

import updater
import update_channel_choice


def release(version, **extra):
    return {"tag_name": "v" + version, "name": version, "draft": False,
            "assets": [{"name": name, "browser_download_url": "https://example.invalid/" + name}
                       for name in ("WinZapp.zip", "SHA256SUMS.txt", "SHA256SUMS.txt.sig")], **extra}


def make_checker(monkeypatch, local="2.1.0.4258alpha", forced=True, releases=None):
    events, queued = [], []
    text = {"update_alpha_choice_message": "Current {current}, alpha {new}; check once?",
            "update_reinstall_downgrade_warning": "Downgrade {current} to {new}",
            "force_reinstall_confirm_msg": "Reinstall {version}"}
    mw = SimpleNamespace(settings={"general": {"alpha_updates_enabled": False}},
                         i18n=SimpleNamespace(language="en-US", t=lambda key: text.get(key, key)),
                         output=lambda *a, **kw: events.append("speech"))
    obj = updater.UpdateChecker(mw)
    obj._force = forced
    monkeypatch.setattr(updater, "__version__", local)
    monkeypatch.setattr(updater, "resolve_changelog", lambda *a: "changes")
    monkeypatch.setattr(obj, "_fetch_releases", lambda: releases if releases is not None else [
        release("2.1.0.4363alpha"), release("2.0.0.0")])
    monkeypatch.setattr(obj, "_schedule_retry", lambda *a: events.append("retry"))
    monkeypatch.setattr(obj, "_claim_prompt", lambda version: events.append("claim") or True)
    monkeypatch.setattr(obj, "_release_prompt", lambda: events.append("release"))
    monkeypatch.setattr(obj, "_main_window_ready", lambda: True)
    monkeypatch.setattr(obj, "_show_update_dialog", lambda *a, **kw: events.append(("update", a, kw)))
    monkeypatch.setattr(updater.wx, "CallAfter", lambda fn, *a, **kw: queued.append((fn, a, kw)))
    return obj, events, queued


def answer(monkeypatch, response, styles):
    def dialog(parent, message, title, style, announce):
        styles.append(style)
        announce()
        return response
    monkeypatch.setattr(update_channel_choice, "message_box", dialog)


def test_manual_alpha_check_offers_choice_before_update(monkeypatch):
    obj, events, queued = make_checker(monkeypatch)
    obj._check_once()
    assert events == ["claim"]
    assert queued[0][0].__name__ == "_show_alpha_channel_choice"
    assert not obj._alpha_enabled()
    assert obj._channel_choice_pending


def test_consent_hands_exact_release_to_existing_update_dialog(monkeypatch):
    obj, events, queued = make_checker(monkeypatch)
    styles = []
    answer(monkeypatch, updater.wx.YES, styles)
    obj._check_once()
    fn, args, kw = queued.pop()
    fn(*args, **kw)
    assert events[0:2] == ["claim", "speech"]
    kind, args, kw = events[2]
    assert kind == "update" and args[0] == "2.1.0.4363alpha"
    assert args[2].endswith("/WinZapp.zip") and args[3].endswith("/SHA256SUMS.txt")
    assert kw == {"signature_url": "https://example.invalid/SHA256SUMS.txt.sig", "is_alpha": True}
    assert not obj._alpha_enabled()  # This is one offer, never a saved opt-in.
    assert "release" not in events and "retry" not in events
    assert styles[0] & updater.wx.NO_DEFAULT
    assert not obj._channel_choice_pending


def test_decline_keeps_opt_out_and_releases_prompt(monkeypatch):
    obj, events, queued = make_checker(monkeypatch)
    answer(monkeypatch, updater.wx.NO, [])
    obj._check_once()
    fn, args, kw = queued.pop()
    fn(*args, **kw)
    assert events == ["claim", "speech", "release", "retry"]
    assert not obj._alpha_enabled()


def test_automatic_check_never_requests_an_alpha_override(monkeypatch):
    obj, events, queued = make_checker(monkeypatch, forced=False)
    obj._check_once()
    assert events == ["retry"] and queued == []


@pytest.mark.parametrize("local", ["2.0.0.0", "2.1.0.4258beta", "2.1.0.4258dev", "unknown"])
def test_non_alpha_builds_never_receive_the_alpha_choice(monkeypatch, local):
    obj, events, queued = make_checker(monkeypatch, local=local)
    obj._check_once()
    assert not obj._channel_choice_pending
    assert queued[0][0].__name__ == "_show_no_update"


def test_alpha_only_repository_can_still_offer_manual_choice(monkeypatch):
    obj, events, queued = make_checker(monkeypatch, releases=[release("2.1.0.4363alpha")])
    obj._check_once()
    assert queued[0][0].__name__ == "_show_alpha_channel_choice"


def test_newer_stable_keeps_the_normal_update_path(monkeypatch):
    obj, events, queued = make_checker(monkeypatch, releases=[release("2.2.0.0"), release("2.1.0.4363alpha")])
    obj._check_once()
    assert queued[0][0].__name__ == "<lambda>"  # stubbed _show_update_dialog
    assert queued[0][1][0] == "2.2.0.0"
    assert not obj._channel_choice_pending


def test_pending_choice_blocks_reentrant_check_and_reinstall(monkeypatch):
    obj, events, queued = make_checker(monkeypatch)
    obj._check_once()
    obj._force = True
    obj._check_once()
    obj._fetch_latest_release_for_reinstall()
    assert len(queued) == 1 and events == ["claim"]


def test_stop_cancels_queued_choice_without_gui_or_new_timer(monkeypatch):
    obj, events, queued = make_checker(monkeypatch)
    obj._check_once()
    obj.stop()
    fn, args, kw = queued.pop()
    fn(*args, **kw)
    assert events == ["claim", "release"]


def test_choice_waits_for_existing_startup_gate(monkeypatch):
    obj, events, queued = make_checker(monkeypatch)
    monkeypatch.setattr(obj, "_main_window_ready", lambda: False)
    delayed = []
    monkeypatch.setattr(updater.wx, "CallLater", lambda delay, fn, *a: delayed.append((fn, a)))
    obj._check_once()
    fn, args, kw = queued.pop()
    fn(*args, **kw)
    assert len(delayed) == 1 and events == ["claim"]
    monkeypatch.setattr(obj, "_main_window_ready", lambda: True)
    answer(monkeypatch, updater.wx.NO, [])
    fn, args = delayed.pop()
    fn(*args)
    assert events[-2:] == ["release", "retry"]


def test_choice_exception_releases_claim_and_retries(monkeypatch):
    obj, events, queued = make_checker(monkeypatch)
    def fail(*a, **kw):
        raise RuntimeError("synthetic dialog failure")
    monkeypatch.setattr(update_channel_choice, "message_box", fail)
    obj._check_once()
    fn, args, kw = queued.pop()
    fn(*args, **kw)
    assert events == ["claim", "release", "retry"]


def test_old_queued_callback_cannot_consume_a_new_choices_claim(monkeypatch):
    obj, events, queued = make_checker(monkeypatch)
    answer(monkeypatch, updater.wx.YES, [])
    obj._check_once()
    old = queued.pop()
    obj.stop()
    obj._channel_choice_cancelled = False
    obj._force = True
    obj._check_once()
    new_token = obj._channel_choice_token
    fn, args, kw = old
    fn(*args, **kw)
    assert events == ["claim", "release", "claim"]
    assert obj._channel_choice_pending and obj._channel_choice_token is new_token
    assert len(queued) == 1


def test_old_modal_answer_cannot_finish_a_new_choice(monkeypatch):
    obj, events, queued = make_checker(monkeypatch)
    def stop_and_replace(*a, **kw):
        obj.stop()
        obj._channel_choice_cancelled = False
        obj._force = True
        obj._check_once()
        return updater.wx.YES
    monkeypatch.setattr(update_channel_choice, "message_box", stop_and_replace)
    obj._check_once()
    fn, args, kw = queued.pop()
    fn(*args, **kw)
    assert events == ["claim", "release", "claim"]
    assert obj._channel_choice_pending and len(queued) == 1


def test_reinstall_downgrade_is_explicit_and_defaults_to_no(monkeypatch):
    obj, events, queued = make_checker(monkeypatch)
    dialogs = []
    def dialog(parent, msg, title, style, announce):
        dialogs.append((msg, style))
        announce()
        return updater.wx.NO
    monkeypatch.setattr(updater, "message_box", dialog)
    monkeypatch.setattr(obj, "_do_install", lambda *a: pytest.fail("declined downgrade"))
    obj._confirm_and_reinstall("2.0.0.0", "https://example.invalid/WinZapp.zip")
    assert "Downgrade 2.1.0.4258alpha to 2.0.0.0" in dialogs[0][0]
    assert dialogs[0][1] & updater.wx.NO_DEFAULT
    assert events == ["speech"]  # announced, so a tray-hidden user hears it


def test_alpha_listing_failure_does_not_report_up_to_date(monkeypatch):
    obj, events, queued = make_checker(monkeypatch)
    obj._mw.settings["general"]["alpha_updates_enabled"] = True
    def get_json(url, params=None):
        if url == updater.GITHUB_API_LATEST_RELEASE:
            raise RuntimeError("synthetic listing failure")
        return release("2.0.0.0")
    monkeypatch.setattr(obj, "_get_json", get_json)
    monkeypatch.setattr(obj, "_fetch_releases", lambda: updater.UpdateChecker._fetch_releases(obj))
    obj._check_once()
    assert events == ["retry"]
    # The manual check is answered with the error, never with "no update".
    assert [fn.__name__ for fn, _, _ in queued] == ["_show_reinstall_error"]
    assert "synthetic listing failure" in queued[0][1][0]


def test_a_failed_automatic_check_stays_silent(monkeypatch):
    obj, events, queued = make_checker(monkeypatch, forced=False)
    def fail():
        raise RuntimeError("offline")
    monkeypatch.setattr(obj, "_fetch_releases", fail)
    obj._check_once()
    assert events == ["retry"] and queued == []


# ── newer_excluded_alpha: the rule on its own ─────────────────────────────────

@pytest.mark.parametrize("releases, local, include_alpha, expected", [
    # An alpha build with the channel off, a newer alpha out: offered.
    ([release("2.1.0.4363alpha"), release("2.0.0.0")], "2.1.0.4258alpha", False, "v2.1.0.4363alpha"),
    # Channel on: the alpha is the normal update, nothing is excluded.
    ([release("2.1.0.4363alpha")], "2.1.0.4258alpha", True, None),
    # A stable build never opted in: never offered an alpha.
    ([release("2.1.0.4363alpha")], "2.0.0.0", False, None),
    ([release("2.1.0.4363alpha")], "2.1.0.4258beta", False, None),
    ([release("2.1.0.4363alpha")], "not a version", False, None),
    # A newer stable wins: that is the normal update.
    ([release("2.2.0.0"), release("2.1.0.4363alpha")], "2.1.0.4258alpha", False, None),
    # The newest alpha is not newer than the running one.
    ([release("2.1.0.4258alpha")], "2.1.0.4258alpha", False, None),
    ([release("2.1.0.4000alpha")], "2.1.0.4258alpha", False, None),
    ([], "2.1.0.4258alpha", False, None),
    # An alpha without a ZIP cannot be installed, so it is not offered.
    ([dict(release("2.1.0.4363alpha"), assets=[])], "2.1.0.4258alpha", False, None),
])
def test_newer_excluded_alpha(releases, local, include_alpha, expected):
    found = updater.newer_excluded_alpha(releases, local, include_alpha)
    assert (found or {}).get("tag_name") == expected


def test_a_failed_manual_check_shows_one_dialog_not_one_per_quick_retry(monkeypatch):
    obj, events, queued = make_checker(monkeypatch)
    monkeypatch.setattr(obj, "_schedule_retry",
                        lambda *a: updater.UpdateChecker._schedule_retry(obj, *a))
    timers = []

    class _Timer:
        def __init__(self, interval, fn):
            timers.append(fn)
            self.daemon = False
        def start(self):
            pass
        def cancel(self):
            pass

    monkeypatch.setattr(updater.threading, "Timer", _Timer)
    def offline():
        raise RuntimeError("offline")
    monkeypatch.setattr(obj, "_fetch_releases", offline)
    obj._check_once()
    for _ in range(3):           # the quick retries, still offline
        timers.pop()()
    assert [fn.__name__ for fn, _, _ in queued] == ["_show_reinstall_error"]
