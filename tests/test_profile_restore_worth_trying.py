"""The question issue #203's early repair asks before spending anything.

_handle_unattended_qr() (websocket_client.py) now tries a profile repair on the
first unattended code, ahead of the startup grace and the confirmation count —
but only when a restore would really start. With nothing restorable,
_recover_suspect_profile() answers with the "profile corrupted" sound, speech
and modal, takes the once-per-launch latch and climbs the persisted generation
ladder: a conclusion the gates exist to keep off one unconfirmed reading.

So the decision of which snapshot to put back lives in one pure function,
pick_restore_generation(), which the recovery and the pre-check both call. Two
copies of it would drift, and the drift would show up as either an early modal
or an early repair the recovery then refuses.
"""

import types

import pytest

import main
from main import MainWindow, pick_restore_generation
from tests.god_modules import patch_main_global

GD, SESSION = "/g", "sess123"


def _fake_recovery(newest=True, previous=True, refused=()):
    """A stand-in for core.profile_recovery: which generations exist, and which
    of them hold a state WhatsApp has already refused."""

    def has_snapshot(global_dir, session_name, prefer_previous=False):
        return previous if prefer_previous else newest

    def refused_reading(global_dir, session_name, prefer_previous=False):
        return ("previous" if prefer_previous else "newest") in refused

    return types.SimpleNamespace(
        has_snapshot=has_snapshot,
        snapshot_matches_live_profile=refused_reading,
        snapshot_was_rejected=lambda *a, **kw: False,
    )


class TestPickRestoreGeneration:
    def test_a_first_attempt_restores_the_newest(self):
        assert pick_restore_generation(_fake_recovery(), GD, SESSION, 0) == (
            False, "ok", False)

    def test_a_restore_that_did_not_hold_climbs_the_ladder(self):
        assert pick_restore_generation(_fake_recovery(), GD, SESSION, 1) == (
            True, "ok", True)

    def test_the_ladder_falls_back_to_the_newest_when_there_is_no_previous(self):
        assert pick_restore_generation(
            _fake_recovery(previous=False), GD, SESSION, 1) == (False, "ok", False)

    def test_a_refused_newest_climbs_within_the_same_launch(self):
        assert pick_restore_generation(
            _fake_recovery(refused=("newest",)), GD, SESSION, 0) == (
                True, "climbed", False)

    def test_every_generation_refused_is_a_dead_end(self):
        assert pick_restore_generation(
            _fake_recovery(refused=("newest", "previous")), GD, SESSION, 0)[1] == "refused"

    def test_a_refused_newest_with_no_previous_is_a_dead_end(self):
        assert pick_restore_generation(
            _fake_recovery(previous=False, refused=("newest",)), GD, SESSION, 0)[1] == "refused"

    def test_nothing_on_disk_is_missing(self):
        assert pick_restore_generation(
            _fake_recovery(newest=False, previous=False), GD, SESSION, 0)[1] == "missing"

    def test_the_rejected_record_counts_as_refused_too(self):
        recovery = _fake_recovery()
        recovery.snapshot_was_rejected = (
            lambda g, s, prefer_previous=False: not prefer_previous)
        assert pick_restore_generation(recovery, GD, SESSION, 0)[1] == "climbed"


class _Stub:
    def __init__(self, generation=0):
        self.token = SESSION + ":tok"
        self.global_dir = GD
        self._generation = generation

    def _profile_recovery_generation(self):
        return self._generation

    _profile_restore_worth_trying = MainWindow._profile_restore_worth_trying


@pytest.fixture
def verdict(monkeypatch):
    """Sets the verdict pick_restore_generation() returns, and records the
    generation it was asked about."""
    asked = {}

    def use(value):
        def fake(profile_recovery, global_dir, session_name, generation):
            asked["generation"] = generation
            asked["args"] = (global_dir, session_name)
            return False, value, False

        patch_main_global(monkeypatch, "pick_restore_generation", fake)
        return asked

    return use


class TestProfileRestoreWorthTrying:
    @pytest.mark.parametrize("value, expected", [
        ("ok", True), ("climbed", True), ("refused", False), ("missing", False),
    ])
    def test_only_a_restore_that_would_start_is_worth_trying(self, verdict, value, expected):
        verdict(value)
        assert _Stub()._profile_restore_worth_trying() is expected

    def test_it_asks_about_the_persisted_generation(self, verdict):
        asked = verdict("ok")
        _Stub(generation=2)._profile_restore_worth_trying()
        assert asked["generation"] == 2
        assert asked["args"] == (GD, SESSION)

    def test_a_spent_recovery_is_not_worth_trying(self, verdict):
        verdict("ok")
        stub = _Stub()
        stub._profile_recovery_attempted = True
        assert stub._profile_restore_worth_trying() is False

    def test_a_restore_already_in_flight_is_not_worth_trying_again(self, verdict):
        verdict("ok")
        stub = _Stub()
        stub._profile_restore_in_flight = True
        assert stub._profile_restore_worth_trying() is False

    def test_no_session_or_data_dir_is_not_worth_trying(self, verdict):
        verdict("ok")
        stub = _Stub()
        stub.token = ""
        assert stub._profile_restore_worth_trying() is False
        stub = _Stub()
        stub.global_dir = None
        assert stub._profile_restore_worth_trying() is False

    def test_it_spends_nothing(self, verdict):
        """No latch and no rung of the ladder: the whole point of asking first."""
        verdict("ok")
        stub = _Stub()
        stub._set_profile_recovery_generation = lambda *a: pytest.fail(
            "the pre-check climbed the generation ladder")
        stub._profile_restore_worth_trying()
        assert not getattr(stub, "_profile_recovery_attempted", False)

    def test_a_failure_to_read_means_wait_for_the_gates(self, monkeypatch):
        def broken(*a, **kw):
            raise OSError("snapshot directory unreadable")

        patch_main_global(monkeypatch, "pick_restore_generation", broken)
        assert _Stub()._profile_restore_worth_trying() is False
