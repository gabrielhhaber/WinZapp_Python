"""The WPPConnect update across accounts, and after a failed install.

* Several autostarted accounts would each prompt at 90 s, and accepting in one
  rewrites the shared api/ folder while another account's Node still runs from
  it: one prompt per machine (update_coord's second claim), and the update
  itself refuses while another account's Node is alive.
* The install wipes api/ before it builds, so a newer release that no longer
  compiles used to leave the account without a server until the next start. The
  bundled minimum is reinstalled once in the same session.
"""

import logging

import pytest

import update_coord
import updater
from main_window import updates
from main_window.updates import UpdatesMixin, should_roll_back
from ui.dialogs import api_setup

TAG = "v2.10.20"
MINIMUM = "v2.10.16"


@pytest.fixture
def gd(tmp_path):
    return str(tmp_path)


def _alive(pid, create_time):
    return True


# ── the second claim ─────────────────────────────────────────────────────────

class TestTheWppClaim:
    def test_it_is_independent_of_the_winzapp_prompt_claim(self, gd):
        assert update_coord.try_claim_update_prompt(
            gd, "1", pid=1, create_time=1.0, is_alive=_alive) is not None
        assert update_coord.try_claim_update_prompt(
            gd, "2.10.20", pid=2, create_time=2.0, is_alive=_alive,
            name=update_coord.WPP_PROMPT_FILE) is not None

    def test_another_process_is_refused_while_the_holder_is_live(self, gd):
        kw = dict(is_alive=_alive, name=update_coord.WPP_PROMPT_FILE)
        update_coord.try_claim_update_prompt(gd, "x", pid=1, create_time=1.0, **kw)
        assert update_coord.try_claim_update_prompt(
            gd, "x", pid=2, create_time=2.0, **kw) is None

    def test_a_dead_holder_is_recovered(self, gd):
        name = update_coord.WPP_PROMPT_FILE
        update_coord.try_claim_update_prompt(gd, "x", pid=1, create_time=1.0,
                                             is_alive=_alive, name=name)
        assert update_coord.try_claim_update_prompt(
            gd, "x", pid=2, create_time=2.0, is_alive=lambda p, c: False, name=name) is not None

    def test_a_corrupt_claim_does_not_suppress_the_prompt(self, gd, tmp_path):
        (tmp_path / update_coord.WPP_PROMPT_FILE).write_text("{not json", encoding="utf-8")
        assert update_coord.try_claim_update_prompt(
            gd, "x", pid=2, create_time=2.0, is_alive=_alive,
            name=update_coord.WPP_PROMPT_FILE) is not None

    def test_release_only_touches_its_own_claim(self, gd):
        wpp = update_coord.try_claim_update_prompt(
            gd, "x", pid=1, create_time=1.0, is_alive=_alive, name=update_coord.WPP_PROMPT_FILE)
        # The WinZapp claim's release function, default name: not this file.
        assert update_coord.release_update_prompt(gd, wpp) is False
        assert update_coord.release_update_prompt(
            gd, wpp, name=update_coord.WPP_PROMPT_FILE) is True


# ── the checker holds and releases it ────────────────────────────────────────

class _MW:
    def __init__(self, gd, **flags):
        self.global_dir = gd
        self.i18n = type("I", (), {"t": staticmethod(lambda key: key)})()
        self.output = lambda *a, **k: None
        self.wpp_update_may_run_now = lambda: True
        self.started = []
        self.start_result = True
        self.__dict__.update(flags)

    def _update_wpp_server(self, tag, on_finished=None):
        self.started.append((tag, on_finished))
        return self.start_result


def _checker(mw):
    checker = updater.WppUpdateChecker(mw)
    checker.retries = []
    checker._schedule_retry = lambda interval=None: checker.retries.append(interval)
    return checker


@pytest.fixture
def answer(monkeypatch):
    box = {"result": updater.wx.YES, "shown": 0}

    def _box(mw, message, title, style, announce=None):
        box["shown"] += 1
        return box["result"]

    monkeypatch.setattr(updater, "message_box", _box)
    return box


def _holder(gd):
    return update_coord._read_prompt(gd, update_coord.WPP_PROMPT_FILE)


def test_a_second_account_does_not_open_its_own_prompt(gd, answer, monkeypatch):
    # This very process is the live holder (real pid and create time) ...
    update_coord.try_claim_update_prompt(gd, "x", name=update_coord.WPP_PROMPT_FILE)
    # ... and the checker under test is another process.
    monkeypatch.setattr(update_coord, "_resolve_identity", lambda pid, ct: (12345, 1.0))
    checker = _checker(_MW(gd))
    checker._prompt_update("2.10.16", "2.10.20", TAG)
    assert answer["shown"] == 0
    assert checker.retries == [updater.WppUpdateChecker._PAIRING_RETRY_INTERVAL]


def test_no_releases_the_claim(gd, answer):
    answer["result"] = updater.wx.NO
    checker = _checker(_MW(gd))
    checker._prompt_update("2.10.16", "2.10.20", TAG)
    assert _holder(gd) is None
    assert checker.retries == [None]


def test_yes_keeps_the_claim_until_the_update_ends(gd, answer):
    mw = _MW(gd)
    checker = _checker(mw)
    checker._prompt_update("2.10.16", "2.10.20", TAG)
    assert _holder(gd) is not None
    assert [t for t, _ in mw.started] == [TAG]

    mw.started[0][1](True)      # on_finished
    assert _holder(gd) is None
    assert checker.retries == [None]


def test_an_update_that_refuses_to_start_releases_the_claim(gd, answer):
    mw = _MW(gd)
    mw.start_result = False
    checker = _checker(mw)
    checker._prompt_update("2.10.16", "2.10.20", TAG)
    assert _holder(gd) is None
    assert checker.retries == [None]      # the long interval: not a 5-minute loop


def test_stopping_the_checker_releases_the_claim(gd, answer):
    checker = _checker(_MW(gd))
    checker._prompt_update("2.10.16", "2.10.20", TAG)
    assert _holder(gd) is not None
    checker.stop()
    assert _holder(gd) is None


def test_no_prompt_opens_on_an_app_that_is_quitting(gd, answer):
    checker = _checker(_MW(gd, _shutting_down=True))
    checker._prompt_update("2.10.16", "2.10.20", TAG)
    assert answer["shown"] == 0 and checker.retries == [] and _holder(gd) is None


def test_no_prompt_opens_over_a_running_update(gd, answer):
    checker = _checker(_MW(gd, _wpp_updating=True))
    checker._prompt_update("2.10.16", "2.10.20", TAG)
    assert answer["shown"] == 0
    assert checker.retries == [updater.WppUpdateChecker._PAIRING_RETRY_INTERVAL]
    assert _holder(gd) is None


def test_without_a_global_dir_the_prompt_is_unclaimed(answer):
    checker = _checker(_MW(None))
    checker._prompt_update("2.10.16", "2.10.20", TAG)
    assert answer["shown"] == 1


# ── UpdateChecker retry timer ────────────────────────────────────────────────

class _FakeTimer:
    made = []

    def __init__(self, interval, fn):
        self.interval, self.cancelled, self.daemon = interval, False, False
        _FakeTimer.made.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


def test_arming_a_retry_cancels_the_previous_timer(monkeypatch):
    _FakeTimer.made = []
    monkeypatch.setattr(updater.threading, "Timer", _FakeTimer)
    checker = updater.UpdateChecker(type("MW", (), {})())
    checker._schedule_retry(30)
    checker._schedule_retry()
    first, second = _FakeTimer.made
    assert first.cancelled and not second.cancelled
    assert second.interval == updater.UpdateChecker._RETRY_INTERVAL


def test_a_forced_check_that_fails_does_not_stay_forced(monkeypatch):
    _FakeTimer.made = []
    monkeypatch.setattr(updater.threading, "Timer", _FakeTimer)
    checker = updater.UpdateChecker(type("MW", (), {})())
    checker._force = True
    checker._schedule_retry(30)
    assert checker._force is False


# ── refusing to update under another account's Node ──────────────────────────

class _Threads:
    started = []

    @staticmethod
    def Thread(target=None, daemon=None, name=None, **k):
        class _H:
            def start(_s):
                _Threads.started.append(target)
        return _H()


class _Window:
    _update_wpp_server = UpdatesMixin._update_wpp_server

    def __init__(self, gd="gd"):
        self.global_dir = gd
        self.account_id = "acc"
        self.background_mode = False
        self._window_hidden = False
        self.i18n = type("I", (), {"t": staticmethod(lambda key: key)})()
        self.error_sound = type("S", (), {"play": staticmethod(lambda: None)})()
        self.spoken, self.events = [], []
        self.wpp_process = object()

    def output(self, text, interrupt=False):
        self.spoken.append(text)

    def _discard_wpp_staging_leftovers(self):
        pass

    def _stop_wpp_server(self):
        pass

    def wait_for_profile_release(self, *a, **k):
        pass

    def ensure_wpp_running(self):
        self.events.append("restart")

    def _reconnect_websocket_now(self):
        pass

    def check_wa_connection_http(self):
        pass

    def trigger_sync_if_needed(self):
        pass


@pytest.fixture
def env(monkeypatch):
    _Threads.started = []
    monkeypatch.setattr(updates, "threading", _Threads)
    monkeypatch.setattr(updates.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))
    boxes = []
    errors = []
    monkeypatch.setattr(updates, "message_box", lambda *a, **k: boxes.append(a[2]) or 0)
    def _error(parent, i18n, message, title, details="", **kwargs):
        boxes.append(title)
        errors.append(details)
    monkeypatch.setattr(updates, "show_error_details", _error)
    monkeypatch.setattr("core.wa_version_refresh.other_accounts_node_alive",
                        lambda g, a, ignore_corrupt=False: False)
    monkeypatch.setattr(updates, "homologated_wpp_tag", lambda _p: MINIMUM)
    monkeypatch.setattr(updates, "_server_is_built", lambda: False)
    monkeypatch.setattr(updates.api_staging, "discard", lambda _p: None)
    monkeypatch.setattr(updates.api_staging, "has_room_for_staging", lambda _p: True)
    monkeypatch.setattr(updates.api_staging, "swap_in_staged_api", lambda *a, **k: "backup")
    monkeypatch.setattr(updates, "restart_api_after_update",
                        lambda window, api, backup, done: (window.ensure_wpp_running(), done(True)))
    tags, results, cancelled = [], [], [False]

    class _Dialog:
        def __init__(self, parent, title_override=None, forced_tag=None, api_dir=None):
            tags.append(forced_tag)

        def ShowModal(self):
            result = results.pop(0)
            if result != updates.wx.ID_OK:
                self._cancelled = cancelled[0]
                self._error_details = "tsc: error TS2345"
            return result

        def Destroy(self):
            pass

    monkeypatch.setattr(api_setup, "ApiSetupDialog", _Dialog)
    return type("Env", (), {"boxes": boxes, "errors": errors, "tags": tags, "results": results,
                            "cancelled": cancelled})


def test_it_refuses_while_another_accounts_node_is_alive(env, monkeypatch):
    monkeypatch.setattr("core.wa_version_refresh.other_accounts_node_alive",
                        lambda g, a, ignore_corrupt=False: True)
    finished = []
    window = _Window()
    assert window._update_wpp_server(TAG, on_finished=lambda ok: finished.append(1)) is False
    assert _Threads.started == [] and finished == []
    assert env.boxes == ["update_error_title"]
    assert getattr(window, "_wpp_updating", False) is False


def test_it_starts_and_reports_the_end(env):
    env.results.append(updates.wx.ID_OK)
    finished = []
    window = _Window()
    assert window._update_wpp_server(TAG, on_finished=lambda ok: finished.append(1)) is True
    assert finished == []
    _Threads.started.pop(0)()                    # the stop phase, which resumes inline
    assert env.tags == [TAG] and finished == [1]


def test_a_failed_build_reinstalls_the_minimum_once(env, caplog):
    env.results.extend([updates.wx.ID_CANCEL, updates.wx.ID_OK])
    window = _Window()
    window._update_wpp_server(TAG)
    with caplog.at_level(logging.INFO):
        _Threads.started.pop(0)()
    assert env.tags == [TAG, MINIMUM]
    assert window.events == ["restart"]
    assert "reinstalling the bundled minimum" in caplog.text
    assert "Restored WPPConnect Server" in caplog.text


def test_a_failed_rollback_is_logged_and_does_not_loop(env, caplog):
    env.results.extend([updates.wx.ID_CANCEL, updates.wx.ID_CANCEL])
    window = _Window()
    window._update_wpp_server(TAG)
    with caplog.at_level(logging.ERROR):
        _Threads.started.pop(0)()
    assert env.tags == [TAG, MINIMUM]
    assert "stays down" in caplog.text
    assert window.events == ["restart"]


def test_no_rollback_when_a_server_is_still_there(env, monkeypatch):
    monkeypatch.setattr(updates, "_server_is_built", lambda: True)
    env.results.append(updates.wx.ID_CANCEL)
    _Window()._update_wpp_server(TAG)
    _Threads.started.pop(0)()
    assert env.tags == [TAG]


@pytest.mark.parametrize("built, target, minimum, expected", [
    (False, TAG, MINIMUM, True),
    (True, TAG, MINIMUM, False),
    (False, MINIMUM, MINIMUM, False),   # the minimum itself failed: nothing older to go to
    (False, TAG, "", False),            # no minimum bundled
])
def test_should_roll_back(built, target, minimum, expected):
    assert should_roll_back(built, target, minimum) is expected


# ── second review round ──────────────────────────────────────────────────────

def _busy(monkeypatch, value=True):
    monkeypatch.setattr("core.wa_version_refresh.other_accounts_node_alive",
                        lambda g, a, ignore_corrupt=False: value)


def test_another_accounts_node_means_no_prompt_and_the_long_retry(gd, answer, monkeypatch):
    _busy(monkeypatch)
    checker = _checker(_MW(gd))
    checker._prompt_update("2.10.16", "2.10.20", TAG)
    assert answer["shown"] == 0 and _holder(gd) is None
    assert checker.retries == [None]          # the 12 h interval, not 5 minutes


def test_the_check_itself_stays_silent_while_another_accounts_node_runs(monkeypatch):
    _busy(monkeypatch)
    prompts = []
    monkeypatch.setattr(updater.wx, "CallAfter", lambda fn, *a: prompts.append(a))
    monkeypatch.setattr(updater, "homologated_wpp_tag", lambda _p: MINIMUM)
    monkeypatch.setattr("ui.dialogs.api_setup.fetch_latest_wpp_tag", lambda: TAG)
    mw = _MW(None)
    mw._get_installed_wpp_version = lambda: "2.10.16"
    checker = _checker(mw)
    checker._check_once()
    assert prompts == [] and checker.retries == [None]


def test_a_refusal_after_yes_is_not_asked_again_this_session(gd, answer):
    mw = _MW(gd)
    mw.start_result = False
    checker = _checker(mw)
    checker._prompt_update("2.10.16", "2.10.20", TAG)
    assert checker._declined_tag == TAG and _holder(gd) is None
    assert checker.retries == [None]


def test_a_failed_update_is_remembered_and_a_successful_one_is_not(gd):
    checker = _checker(_MW(gd))
    checker._update_finished(TAG, False)
    assert checker._declined_tag == TAG
    checker._declined_tag = None
    checker._update_finished(TAG, True)
    assert checker._declined_tag is None
    assert checker.retries == [None, None]


def test_an_exception_in_the_prompt_hands_the_claim_back(gd, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("dialog failed")

    monkeypatch.setattr(updater, "message_box", _boom)
    checker = _checker(_MW(gd))
    with pytest.raises(RuntimeError):
        checker._prompt_update("2.10.16", "2.10.20", TAG)
    assert _holder(gd) is None


class TestCorruptLeases:
    @staticmethod
    def _leases(monkeypatch, value):
        import node_coord

        def _live(global_dir, is_alive=None):
            if isinstance(value, Exception):
                raise value
            return value

        monkeypatch.setattr(node_coord, "live_node_leases", _live)

    def test_corrupt_counts_as_alive_by_default(self, monkeypatch):
        from core.wa_version_refresh import other_accounts_node_alive
        self._leases(monkeypatch, [{"_corrupt": True, "account_id": "x"}])
        assert other_accounts_node_alive("gd", "me") is True

    def test_corrupt_is_logged_and_ignored_for_the_update_check(self, monkeypatch, caplog):
        from core.wa_version_refresh import other_accounts_node_alive
        self._leases(monkeypatch, [{"_corrupt": True, "account_id": "x"}])
        with caplog.at_level(logging.WARNING):
            assert other_accounts_node_alive("gd", "me", ignore_corrupt=True) is False
        assert "unreadable node lease" in caplog.text

    def test_a_real_other_account_still_counts(self, monkeypatch):
        from core.wa_version_refresh import other_accounts_node_alive
        self._leases(monkeypatch, [{"_corrupt": True}, {"account_id": "other"}])
        assert other_accounts_node_alive("gd", "me", ignore_corrupt=True) is True

    def test_our_own_lease_does_not(self, monkeypatch):
        from core.wa_version_refresh import other_accounts_node_alive
        self._leases(monkeypatch, [{"account_id": "me"}])
        assert other_accounts_node_alive("gd", "me", ignore_corrupt=True) is False

    def test_a_failing_lookup_stays_fail_closed(self, monkeypatch):
        from core.wa_version_refresh import other_accounts_node_alive
        self._leases(monkeypatch, OSError("disk"))
        assert other_accounts_node_alive("gd", "me", ignore_corrupt=True) is True


# ── cancel vs failure, and what on_finished is told ──────────────────────────

def test_a_failed_install_tells_on_finished_false_and_shows_the_error(env):
    env.results.extend([updates.wx.ID_CANCEL, updates.wx.ID_OK])
    done = []
    _Window()._update_wpp_server(TAG, on_finished=done.append)
    _Threads.started.pop(0)()
    assert done == [False]
    assert "update_error_title" in env.boxes
    assert env.errors == ["tsc: error TS2345"]


def test_a_successful_install_tells_on_finished_true(env):
    env.results.append(updates.wx.ID_OK)
    done = []
    _Window()._update_wpp_server(TAG, on_finished=done.append)
    _Threads.started.pop(0)()
    assert done == [True]


def test_success_is_not_announced_until_http_validation_finishes(env, monkeypatch):
    callbacks, done = [], []
    monkeypatch.setattr(updates, "restart_api_after_update",
                        lambda window, api, backup, cb: callbacks.append(cb))
    env.results.append(updates.wx.ID_OK)
    window = _Window()
    window._update_wpp_server(TAG, on_finished=done.append)
    _Threads.started.pop(0)()
    assert done == [] and window._wpp_updating is True
    assert "wpp_update_complete" not in window.spoken
    callbacks.pop()(False)
    assert done == [False] and window._wpp_updating is False
    assert "wpp_update_complete" not in window.spoken


def test_a_profile_still_in_use_prevents_the_install(env):
    window = _Window()
    window.wait_for_profile_release = lambda *a, **k: False
    window._update_wpp_server(TAG)
    _Threads.started.pop(0)()
    assert env.tags == [] and "wpp_update_complete" not in window.spoken


def test_a_user_cancel_shows_no_error_box_but_still_restores_a_missing_server(env):
    env.cancelled[0] = True
    env.results.extend([updates.wx.ID_CANCEL, updates.wx.ID_OK])
    done = []
    _Window()._update_wpp_server(TAG, on_finished=done.append)
    _Threads.started.pop(0)()
    assert env.boxes == []
    assert env.tags == [TAG, MINIMUM]
    assert done == [False]
