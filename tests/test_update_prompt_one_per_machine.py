"""One update prompt per machine, not one per open account.

Every account runs in its own process with its own UpdateChecker, so a release
newer than the running build was found N times and asked about N times: two
accounts open meant two "a new version is available" windows for one update.
Only one of them could ever have installed it — try_begin_update() refuses while
any other account's runtime lease is live — so the extra dialogs were never a
second chance at anything, just a second thing to dismiss.

The claim mirrors update_state.json: same updater_lock, same atomic write, same
(pid, create_time) liveness so a crashed holder is recovered rather than
blocking every account forever. Where it deliberately differs is the failure
direction — see test_a_corrupt_claim_does_not_suppress_the_prompt.
"""

import inspect
import json
import os

import pytest

import update_coord
from updater import UpdateChecker


@pytest.fixture
def gd(tmp_path):
    return str(tmp_path)


def _alive(pid, create_time):
    return True


def _dead(pid, create_time):
    return False


class TestTheClaim:
    def test_the_first_caller_gets_it(self, gd):
        token = update_coord.try_claim_update_prompt(gd, "0.26.0.0beta", pid=111,
                                                     create_time=1.0, is_alive=_alive)
        assert token is not None
        assert len(token["owner_token"]) == 32

    def test_a_second_process_is_refused_while_the_first_is_live(self, gd):
        update_coord.try_claim_update_prompt(gd, "0.26.0.0beta", pid=111,
                                             create_time=1.0, is_alive=_alive)
        assert update_coord.try_claim_update_prompt(
            gd, "0.26.0.0beta", pid=222, create_time=2.0, is_alive=_alive) is None

    def test_it_blocks_regardless_of_the_version_being_offered(self, gd):
        """While a dialog is on screen there is nothing a second one can add,
        even for a newer alpha. The blocked account re-checks on its own retry
        timer."""
        update_coord.try_claim_update_prompt(gd, "0.26.0.0beta", pid=111,
                                             create_time=1.0, is_alive=_alive)
        assert update_coord.try_claim_update_prompt(
            gd, "0.27.0.0beta", pid=222, create_time=2.0, is_alive=_alive) is None

    def test_a_dead_holder_never_blocks_anyone(self, gd):
        """The install path exits the process while still holding the claim (it
        relaunches the whole install directory), so the file is routinely left
        behind by a process that no longer exists. If that blocked the next
        launch, updates would stop after the first one."""
        update_coord.try_claim_update_prompt(gd, "0.26.0.0beta", pid=111,
                                             create_time=1.0, is_alive=_alive)
        assert update_coord.try_claim_update_prompt(
            gd, "0.26.0.0beta", pid=222, create_time=2.0, is_alive=_dead) is not None

    def test_the_same_process_may_re_claim(self, gd):
        """A checker that somehow asks twice replaces its own claim rather than
        deadlocking against itself."""
        first = update_coord.try_claim_update_prompt(gd, "0.26.0.0beta", pid=111,
                                                     create_time=1.0, is_alive=_alive)
        second = update_coord.try_claim_update_prompt(gd, "0.26.0.0beta", pid=111,
                                                      create_time=1.0, is_alive=_alive)
        assert second is not None
        assert second["owner_token"] != first["owner_token"]


class TestTheRelease:
    def test_releasing_lets_the_next_account_ask(self, gd):
        token = update_coord.try_claim_update_prompt(gd, "0.26.0.0beta", pid=111,
                                                     create_time=1.0, is_alive=_alive)
        assert update_coord.release_update_prompt(gd, token) is True
        assert update_coord.try_claim_update_prompt(
            gd, "0.26.0.0beta", pid=222, create_time=2.0, is_alive=_alive) is not None

    def test_a_stale_token_cannot_release_a_newer_claim(self, gd):
        """A checker that declines, then claims again three hours later, must
        not have its new dialog cleared by the old one's late release."""
        old = update_coord.try_claim_update_prompt(gd, "0.26.0.0beta", pid=111,
                                                   create_time=1.0, is_alive=_alive)
        update_coord.release_update_prompt(gd, old)
        update_coord.try_claim_update_prompt(gd, "0.26.0.0beta", pid=111,
                                             create_time=1.0, is_alive=_alive)

        assert update_coord.release_update_prompt(gd, old) is False
        assert update_coord.update_prompt_holder(gd, is_alive=_alive) is not None


class TestItFailsInTheSafeDirection:
    def test_a_corrupt_claim_does_not_suppress_the_prompt(self, gd):
        """The opposite of update_state.json, deliberately. An unreadable state
        file must block an INSTALL, because installing twice corrupts the
        installation. An unreadable prompt claim must NOT block the PROMPT: the
        worst it guards against is a second dialog, while treating it as held
        would silently stop offering updates on every account, forever, with
        nothing to show the user why."""
        path = os.path.join(gd, "update_prompt.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ not json at all")

        assert update_coord.try_claim_update_prompt(
            gd, "0.26.0.0beta", pid=111, create_time=1.0, is_alive=_alive) is not None

    def test_a_claim_with_a_bad_owner_is_cleared_rather_than_trusted(self, gd):
        path = os.path.join(gd, "update_prompt.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"owner_pid": "not-a-pid", "owner_create_time": 1.0,
                       "owner_token": "0" * 32}, f)

        assert update_coord.try_claim_update_prompt(
            gd, "0.26.0.0beta", pid=111, create_time=1.0, is_alive=_alive) is not None


class _StubChecker:
    """UpdateChecker's claim helpers against a stub main window — the checker
    itself is constructed with one, so no wx.App is involved.

    ``pid`` stands in for the separate account PROCESS each checker really runs
    in; the tests below share one interpreter, so without it every stub would
    take the same-process re-claim path rather than the cross-account one.
    """

    def __init__(self, global_dir, pid=None):
        self._mw = type("MW", (), {"global_dir": global_dir})()
        self._prompt_token = None
        self._pid = pid

    _global_dir = UpdateChecker._global_dir
    _claim_prompt = UpdateChecker._claim_prompt
    _release_prompt = UpdateChecker._release_prompt


@pytest.fixture
def as_separate_processes(monkeypatch):
    """Route the checker's claim through the real coordinator, but with each
    stub's own pid and a liveness answer that does not depend on those pids
    existing on the machine running the tests."""
    real = update_coord.try_claim_update_prompt

    def _claim(global_dir, version, **kw):
        import inspect as _inspect
        caller = _inspect.currentframe().f_back.f_locals.get("self")
        pid = getattr(caller, "_pid", None) or 111
        return real(global_dir, version, pid=pid, create_time=float(pid),
                    is_alive=_alive)

    monkeypatch.setattr(update_coord, "try_claim_update_prompt", _claim)


class TestTheCheckerUsesIt:
    def test_two_accounts_only_one_prompt(self, gd, as_separate_processes):
        a, b = _StubChecker(gd, pid=111), _StubChecker(gd, pid=222)
        assert a._claim_prompt("0.26.0.0beta") is True
        assert b._claim_prompt("0.26.0.0beta") is False

    def test_declining_hands_it_back(self, gd, as_separate_processes):
        a, b = _StubChecker(gd, pid=111), _StubChecker(gd, pid=222)
        a._claim_prompt("0.26.0.0beta")
        a._release_prompt()
        assert b._claim_prompt("0.26.0.0beta") is True

    def test_no_global_dir_means_no_coordination(self, tmp_path):
        """A single-account or dev run has no shared directory — and no second
        account to duplicate the dialog for."""
        checker = _StubChecker(None)
        assert checker._claim_prompt("0.26.0.0beta") is True
        assert checker._prompt_token is None

    def test_it_fails_open(self, gd, monkeypatch):
        """A prompt that cannot be coordinated is worth far more than a prompt
        suppressed by a bug in the coordination."""
        def _boom(*a, **kw):
            raise RuntimeError("coordination is broken")
        monkeypatch.setattr(update_coord, "try_claim_update_prompt", _boom)

        assert _StubChecker(gd)._claim_prompt("0.26.0.0beta") is True


class TestTheClaimIsHeldAcrossTheInstall:
    def test_saying_yes_does_not_release_before_installing(self):
        """Releasing on ID_YES would let another account open its own dialog
        while this one is already downloading and about to relaunch the shared
        install directory."""
        src = inspect.getsource(UpdateChecker._show_update_dialog)
        yes_branch = src[src.index("if result == wx.ID_YES:"):src.index("else:")]
        assert "_release_prompt" not in yes_branch
        assert "_do_install" in yes_branch

    def test_every_install_exit_that_leaves_us_running_releases_it(self):
        """The only path that may keep the claim is the one ending in
        real_exit(): that takes the owner process with it, and a dead owner is
        recovered for free on the next check."""
        src = inspect.getsource(UpdateChecker._do_install)
        for marker in ("if result == wx.ID_CANCEL:",
                       "if retry != wx.YES:",
                       "staying open instead of exiting"):
            after = src[src.index(marker):]
            assert "_release_prompt" in after[:after.index("return") + 200], (
                f"the branch at {marker!r} leaves the app running while still "
                "holding the prompt claim — no account can be offered the "
                "update again"
            )
