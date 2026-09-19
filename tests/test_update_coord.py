"""Tests for client/update_coord.py — updater coordination (runtime leases +
update_state), the TOCTOU-safe protocol that gates start vs update install.
"""

import os

import pytest

import update_coord as uc


def _gd(tmp_path):
    gd = str(tmp_path / "global")
    os.makedirs(gd, exist_ok=True)
    return gd


ALIVE = lambda pid, ct: True


def test_default_proc_create_time_never_uses_os_kill_on_windows(monkeypatch):
    """REGRESSION (real Windows run): on win32, os.kill(pid, 0) calls
    TerminateProcess and would kill the process itself. The fallback must use
    OpenProcess, never os.kill, when psutil is absent and platform is win32."""
    import sys as _sys
    # Force the no-psutil path.
    monkeypatch.setitem(__import__("sys").modules, "psutil", None)
    called = {"os_kill": False}
    real_os_kill = os.kill

    def _guard(*a, **k):
        called["os_kill"] = True
        return real_os_kill(*a, **k)

    monkeypatch.setattr(os, "kill", _guard)
    monkeypatch.setattr(_sys, "platform", "win32", raising=False)
    # ctypes.WinDLL doesn't exist off-Windows; only assert the branch avoids
    # os.kill. On a real win32 host this exercises OpenProcess for real.
    try:
        uc._default_proc_create_time(os.getpid())
    except (AttributeError, OSError, FileNotFoundError):
        # WinDLL/kernel32 unavailable on this non-Windows CI host — acceptable;
        # the point is that os.kill was NOT the path taken.
        pass
    assert called["os_kill"] is False, "os.kill must never run on win32 (kills self)"


def test_default_proc_matches_us_identifies_its_own_process():
    # Our own pid's executable is whatever this test runs under (python.exe /
    # WinZapp.exe) — _expected_process_basenames() always includes it via
    # sys.executable, so this must never be a definite False.
    assert uc._default_proc_matches_us(os.getpid()) is not False


def test_default_proc_matches_us_is_false_for_a_pid_that_does_not_exist():
    # A pid this large is vanishingly unlikely to be a live process on any
    # real machine or CI runner.
    assert uc._default_proc_matches_us(2 ** 30) is False


def test_runtime_lease_create_and_list(tmp_path):
    gd = _gd(tmp_path)
    alive = lambda pid, ct: pid == 1111 and ct == 5.0
    lease = uc.try_create_runtime_lease(gd, pid=1111, create_time=5.0, is_alive=alive)
    assert lease is not None
    live = uc.live_runtime_leases(gd, is_alive=alive)
    assert any(l["pid"] == 1111 for l in live)
    uc.release_runtime_lease(gd, lease)
    assert all(l.get("pid") != 1111 for l in uc.live_runtime_leases(gd, is_alive=alive))


def test_dead_lease_filtered(tmp_path):
    gd = _gd(tmp_path)
    uc.try_create_runtime_lease(gd, pid=999999, create_time=1.0, is_alive=ALIVE)
    live = uc.live_runtime_leases(gd, is_alive=lambda pid, ct: False)
    assert live == []


def test_lease_alive_pid_reuse_guard():
    def fake(pid):
        return 100.0 if pid == 42 else None
    assert uc.lease_alive(42, 100.0, proc_create_time=fake) is True
    assert uc.lease_alive(42, 999.0, proc_create_time=fake) is False  # PID reuse
    assert uc.lease_alive(43, 100.0, proc_create_time=fake) is False


def test_begin_end_update_with_token(tmp_path):
    gd = _gd(tmp_path)
    assert uc.is_update_in_progress(gd) is False
    tok = uc.try_begin_update(gd, pid=222, create_time=9.0, is_alive=ALIVE)
    assert tok is not None and tok["owner_token"]
    alive = lambda pid, ct: pid == 222 and ct == 9.0
    assert uc.is_update_in_progress(gd, is_alive=alive) is True
    assert uc.end_update(gd, tok) is True
    assert uc.is_update_in_progress(gd, is_alive=alive) is False


def test_dead_owner_recovered(tmp_path):
    gd = _gd(tmp_path)
    uc.try_begin_update(gd, pid=888888, create_time=1.0, is_alive=ALIVE)
    assert uc.is_update_in_progress(gd, is_alive=lambda pid, ct: False) is False


def test_should_block_start():
    assert uc.should_block_start({"update_in_progress": True}) is True
    assert uc.should_block_start({"update_in_progress": False}) is False
    assert uc.should_block_start({}) is False


def test_should_block_update_with_live_leases():
    assert uc.should_block_update([{"pid": 1}]) is True
    assert uc.should_block_update([]) is False


def test_try_create_lease_blocked_during_update(tmp_path):
    gd = _gd(tmp_path)
    tok = uc.try_begin_update(gd, pid=500, create_time=1.0, is_alive=ALIVE)
    assert tok is not None
    assert uc.try_create_runtime_lease(gd, pid=501, create_time=2.0, is_alive=ALIVE) is None


def test_try_begin_update_refuses_when_accounts_live(tmp_path):
    gd = _gd(tmp_path)
    uc.try_create_runtime_lease(gd, pid=600, create_time=1.0, is_alive=ALIVE)
    assert uc.try_begin_update(gd, pid=601, create_time=2.0, is_alive=ALIVE) is None


def test_try_begin_update_refuses_second_live_updater(tmp_path):
    gd = _gd(tmp_path)
    tok = uc.try_begin_update(gd, pid=700, create_time=1.0, is_alive=ALIVE)
    assert tok is not None
    assert uc.try_begin_update(gd, pid=701, create_time=2.0, is_alive=ALIVE) is None


def test_end_update_requires_token(tmp_path):
    gd = _gd(tmp_path)
    uc.try_begin_update(gd, pid=800, create_time=1.0, is_alive=ALIVE)
    with pytest.raises(ValueError):
        uc.end_update(gd, None)  # token mandatory (GPT r3 #1)


def test_end_update_wrong_token_refused(tmp_path):
    gd = _gd(tmp_path)
    tok = uc.try_begin_update(gd, pid=800, create_time=1.0, is_alive=ALIVE)
    bad = {"owner_pid": 800, "owner_create_time": 1.0, "owner_token": "deadbeef"}
    assert uc.end_update(gd, bad) is False
    assert uc.is_update_in_progress(gd, is_alive=ALIVE) is True
    assert uc.end_update(gd, tok) is True


def test_stale_same_pid_token_cannot_end_new_update(tmp_path):
    """A token from an earlier run of the SAME pid must not end a newer update
    (GPT r3 #2 — owner_token identifies the specific install run)."""
    gd = _gd(tmp_path)
    old = uc.try_begin_update(gd, pid=900, create_time=1.0, is_alive=ALIVE)
    uc.end_update(gd, old)
    new = uc.try_begin_update(gd, pid=900, create_time=1.0, is_alive=ALIVE)
    assert uc.end_update(gd, old) is False  # stale token rejected
    assert uc.is_update_in_progress(gd, is_alive=ALIVE) is True
    assert uc.end_update(gd, new) is True


def test_corrupt_update_state_fails_closed(tmp_path):
    gd = _gd(tmp_path)
    open(os.path.join(gd, "update_state.json"), "w").write("{ broken")
    assert uc.is_update_in_progress(gd) is True


def test_type_invalid_owner_pid_fails_closed(tmp_path):
    gd = _gd(tmp_path)
    import json
    open(os.path.join(gd, "update_state.json"), "w").write(
        json.dumps({"update_in_progress": True, "owner_pid": {}, "owner_create_time": 1.0})
    )
    # owner_pid={} must not raise; must fail closed (GPT r3 #3)
    assert uc.is_update_in_progress(gd) is True


def test_corrupt_lease_counts_as_live(tmp_path):
    gd = _gd(tmp_path)
    d = os.path.join(gd, "runtime")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "garbage"), "w").write("{ not json")
    live = uc.live_runtime_leases(gd, is_alive=lambda pid, ct: False)
    assert len(live) == 1 and live[0].get("_corrupt") is True


def test_type_invalid_lease_counts_as_live(tmp_path):
    gd = _gd(tmp_path)
    d = os.path.join(gd, "runtime")
    os.makedirs(d, exist_ok=True)
    import json
    open(os.path.join(d, "bad"), "w").write(json.dumps({"pid": True, "create_time": "NaN"}))
    live = uc.live_runtime_leases(gd, is_alive=lambda pid, ct: False)
    assert len(live) == 1 and live[0].get("_corrupt") is True


def test_leftover_tmp_ignored(tmp_path):
    gd = _gd(tmp_path)
    d = os.path.join(gd, "runtime")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "x.tmp"), "w").write("partial")
    live = uc.live_runtime_leases(gd, is_alive=ALIVE)
    assert live == []


# ── GPT r4 hardening ─────────────────────────────────────────────────────────
def test_try_ops_reject_invalid_identity(tmp_path):
    gd = _gd(tmp_path)
    for bad_pid, bad_ct in [(0, 1.0), (-1, 1.0), (True, 1.0), (10, float("nan")), (10, -1.0)]:
        with pytest.raises(ValueError):
            uc.try_create_runtime_lease(gd, pid=bad_pid, create_time=bad_ct, is_alive=ALIVE)
        with pytest.raises(ValueError):
            uc.try_begin_update(gd, pid=bad_pid, create_time=bad_ct, is_alive=ALIVE)


def test_state_missing_owner_token_is_corrupt(tmp_path):
    gd = _gd(tmp_path)
    import json
    open(os.path.join(gd, "update_state.json"), "w").write(
        json.dumps({"update_in_progress": True, "owner_pid": 5, "owner_create_time": 1.0})
    )
    # in-progress without a valid owner_token -> corrupt -> fail-closed
    assert uc.is_update_in_progress(gd) is True


def test_ct_unknown_sentinel_is_alive():
    # unknown create_time (sentinel 0.0), and no way to identify the pid's
    # own process either -> must still count as alive (fail-closed).
    assert uc.lease_alive(123, 5.0, proc_create_time=lambda pid: 0.0,
                          proc_matches_us=lambda pid: None) is True
    assert uc.lease_alive(123, 5.0, proc_create_time=lambda pid: None) is False


def test_ct_unknown_sentinel_with_positive_proof_of_a_different_process_is_dead():
    """A 0.0-recorded lease is written once at process start and never
    refreshed, so a process that crashes/is killed without releasing it
    leaves that lease on disk forever — and since create_time==0.0 can never
    be disproved by pid existence alone, Windows reusing that pid for ANY
    other process (routine) made it "alive" forever, permanently blocking
    every future update on that install. Reported live: a genuinely
    single-account install refused to update believing another account was
    open, traced to 11 leaked 0.0 leases up to two weeks old.

    A positive, non-None verdict that the pid's own image is NOT ours is the
    one thing that can prove it's actually dead."""
    assert uc.lease_alive(123, 5.0, proc_create_time=lambda pid: 0.0,
                          proc_matches_us=lambda pid: False) is False


def test_ct_unknown_sentinel_with_positive_match_is_alive():
    assert uc.lease_alive(123, 5.0, proc_create_time=lambda pid: 0.0,
                          proc_matches_us=lambda pid: True) is True


def test_leftover_zero_leases_self_heal_once_their_pid_belongs_to_someone_else(tmp_path):
    """The scenario end to end: other_live_leases() must stop counting a
    leaked 0.0 lease as another account once its pid is proven to belong to
    a different process, and _live_leases_locked() must remove it from disk
    (the same self-healing path used for any other confirmed-dead lease)."""
    import functools

    gd = _gd(tmp_path)
    uc.try_create_runtime_lease(gd, pid=900, create_time=5.0, is_alive=ALIVE)
    # A leaked lease from a crashed process, recorded with the unknown-ct
    # sentinel — its pid (777) now happens to be held by an unrelated process.
    uc._write_lease(uc._runtime_dir(gd), 777, 0.0)

    is_alive = functools.partial(
        uc.lease_alive,
        proc_create_time=lambda pid: 5.0 if pid == 900 else 12345.0,
        proc_matches_us=lambda pid: pid != 777,  # pid 777 is provably not us
    )
    others = uc.other_live_leases(gd, pid=900, create_time=5.0, is_alive=is_alive)
    assert others == []
    remaining = [f for f in os.listdir(uc._runtime_dir(gd)) if not f.endswith(".tmp")]
    assert len(remaining) == 1 and remaining[0].startswith("900_")


# ── GPT r5 hardening ─────────────────────────────────────────────────────────
def test_valid_ct_rejects_negative_and_huge():
    assert uc._valid_ct(-1.0) is False
    assert uc._valid_ct(float("inf")) is False
    assert uc._valid_ct(10 ** 400) is False  # would OverflowError in isfinite
    assert uc._valid_ct(0) is True
    assert uc._valid_ct(123.5) is True


def test_release_lease_rejects_path_traversal(tmp_path):
    gd = _gd(tmp_path)
    tok = uc.try_begin_update(gd, pid=10, create_time=1.0, is_alive=ALIVE)
    assert tok is not None
    uc.release_runtime_lease(gd, "../update_state.json")
    uc.release_runtime_lease(gd, "/etc/passwd")
    uc.release_runtime_lease(gd, "a/b")
    # state file survived the traversal attempts
    assert uc.is_update_in_progress(gd, is_alive=ALIVE) is True


def test_state_dir_instead_of_file_is_corrupt(tmp_path):
    gd = _gd(tmp_path)
    os.mkdir(os.path.join(gd, "update_state.json"))  # a directory, not a file
    assert uc.is_update_in_progress(gd) is True  # corrupt -> fail-closed


def test_try_begin_update_ignores_the_callers_own_lease(tmp_path):
    """The updater runs inside a live account, so its own runtime lease must
    not count as "another account is open" — it did, which is why the gate
    was never wired up and two accounts could xcopy over each other."""
    gd = _gd(tmp_path)
    uc.try_create_runtime_lease(gd, pid=900, create_time=5.0, is_alive=ALIVE)
    tok = uc.try_begin_update(gd, pid=900, create_time=5.0, is_alive=ALIVE)
    assert tok is not None
    assert uc.end_update(gd, tok) is True


def test_other_live_leases_excludes_self_and_dead(tmp_path):
    gd = _gd(tmp_path)
    uc.try_create_runtime_lease(gd, pid=900, create_time=5.0, is_alive=ALIVE)
    uc.try_create_runtime_lease(gd, pid=901, create_time=6.0, is_alive=ALIVE)
    uc.try_create_runtime_lease(gd, pid=902, create_time=7.0, is_alive=ALIVE)
    alive = lambda pid, ct: pid != 902
    others = uc.other_live_leases(gd, pid=900, create_time=5.0, is_alive=alive)
    assert [l["pid"] for l in others] == [901]
    # A lease of ours from a previous life (same pid, other create_time) is
    # a different process and still counts.
    stale = uc.other_live_leases(gd, pid=900, create_time=4.0, is_alive=alive)
    assert sorted(l["pid"] for l in stale) == [900, 901]
