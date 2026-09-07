"""Tests for the lock around MainWindow._act_on_unlink_decision().

check_wa_connection_http() and _handle_local_auth_rejected() call this from
several independent threads (the health-check loop, _run_sync's tight poll,
wx.CallAfter callbacks). Before this lock, a bare check-then-set on
_logout_handled was a real race: two callers could both read it False before
either set it True, and both would fire _on_disconnect() -- a real incident
this codebase hit once already (two wipes logged inside the same second,
from two callers observing the same confirmed-unlinked reading).

This drives many real threads into _act_on_unlink_decision() at once (a
Barrier makes them actually overlap, not just run one after another) and
asserts the destructive action only ever fires once -- deterministically,
not "usually", since the whole point of the lock is to make this no longer
depend on timing.

Two levels, and both are needed. _call_locked() below takes the lock the way
the callers are supposed to, so it pins _act_on_unlink_decision() as safe
*given* the lock -- but it would stay green if a call site dropped its
`with`. TestTheStatusStringCallSiteTakesTheLock therefore drives the real
check_wa_connection_http() instead, so the notLogged/QRCODE path is held to
actually taking the lock and not merely to being safe once someone does.
(The 401/403 path is driven end to end the same way in the sibling file,
tests/test_local_auth_rejected_logout_gate.py.)
"""

import json
import threading
import time

import pytest

import connection_state as cs
from app_paths import resource_path
from main import MainWindow


class _Recorder:
    def __init__(self):
        self.played = 0

    def play(self):
        self.played += 1


def _real_translations():
    """The real pt-BR strings — see the same helper in
    tests/test_local_auth_rejected_logout_gate.py for why a `lambda key: key`
    stub is not good enough: it hides a wrong .format() call on any string
    with a named placeholder, which is what t("error") is."""
    with open(resource_path("languages", "pt-BR.json"), "r", encoding="utf-8") as f:
        return json.load(f)


_TRANSLATIONS = _real_translations()


class _I18n:
    @staticmethod
    def t(key):
        return _TRANSLATIONS.get(key, key)


class _Stub:
    _STILL_LINKED_VETO_LIMIT = MainWindow._STILL_LINKED_VETO_LIMIT
    _act_on_unlink_decision = MainWindow._act_on_unlink_decision

    def _note_status_for_profile_health(self, status):
        # The profile-health observer hangs off this very call site; without
        # it the guard there swallows an AttributeError and these tests pass
        # for the wrong reason.
        self.profile_health_readings.append(status)

    def __init__(self, *, probe=cs.LINK_PROBE_UNLINKED):
        self.profile_health_readings = []
        self._unlink_decision_lock = threading.Lock()
        self._probe = probe
        self.still_linked_probe_calls = 0
        self._probe_calls_lock = threading.Lock()
        self.error_sound = _Recorder()
        self.i18n = _I18n()
        self.app_name = "WinZapp"
        self.disconnect_calls = []
        self._disconnect_lock = threading.Lock()
        self._logout_strikes = 4
        self._resume_fail_strikes = 20

    def _still_linked_on_server(self):
        with self._probe_calls_lock:
            self.still_linked_probe_calls += 1
        # The real implementation makes a blocking HTTP call here, which
        # releases the GIL for the duration — exactly the window a second
        # thread can slip through a non-atomic check-then-set in. A trivial
        # in-memory stub would never actually exercise that race, so this
        # sleeps briefly to stand in for it.
        time.sleep(0.01)
        return self._probe

    def _on_disconnect(self, wipe=True):
        with self._disconnect_lock:
            self.disconnect_calls.append(wipe)


def _call_locked(stub, decision, log_label="test"):
    """What both real call sites do: hold the lock across the whole
    decision, not just the strike counting."""
    with stub._unlink_decision_lock:
        stub._act_on_unlink_decision(decision, log_label=log_label)


def _run_concurrently(target, n=25, timeout=10):
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()
        target()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
        assert not t.is_alive(), "a worker thread hung — possible deadlock"


def _patch_wx(monkeypatch):
    # wx.CallAfter runs its callback inline and synchronously here, so the
    # logout dialog/_on_disconnect() call happens to the queued threads
    # still logically "inside" the decision — the same shape a real 10s
    # _still_linked_on_server() HTTP call blocking under the lock has.
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    monkeypatch.setattr("main.wx.MessageBox", lambda *a, **kw: None)
    monkeypatch.setattr("main.wx.OK", 0, raising=False)
    monkeypatch.setattr("main.wx.ICON_ERROR", 0, raising=False)


class TestConfirmedLogoutFiresExactlyOnce:
    def test_many_concurrent_confirmed_logouts_wipe_only_once(self, monkeypatch):
        _patch_wx(monkeypatch)
        stub = _Stub(probe=cs.LINK_PROBE_UNLINKED)

        _run_concurrently(lambda: _call_locked(stub, cs.LOGOUT), n=25)

        assert stub.disconnect_calls == [True]

    def test_many_concurrent_resume_failed_readings_pair_only_once(self, monkeypatch):
        _patch_wx(monkeypatch)
        stub = _Stub()

        _run_concurrently(lambda: _call_locked(stub, cs.RESUME_FAILED), n=25)

        assert stub.disconnect_calls == [False]


class TestStillLinkedVetoUnderConcurrency:
    def test_a_still_linked_phone_never_wipes_even_under_contention(self, monkeypatch):
        _patch_wx(monkeypatch)
        stub = _Stub(probe=cs.LINK_PROBE_LINKED)

        _run_concurrently(lambda: _call_locked(stub, cs.LOGOUT), n=25)

        assert True not in stub.disconnect_calls, "a veto must never wipe"
        # 25 overlapping confirmed logouts, all vetoed: the veto is bounded
        # (see _STILL_LINKED_VETO_LIMIT), so the run ends on the pairing
        # dialog rather than looping forever — but exactly once, and never
        # destructively, which is what the lock has to guarantee.
        assert stub.disconnect_calls == [False]
        assert stub.still_linked_probe_calls == stub._STILL_LINKED_VETO_LIMIT


class TestTheStatusStringCallSiteTakesTheLock:
    """The other half of the guarantee: check_wa_connection_http() must hold
    the lock across its own count-then-decide section.

    Everything above proves _act_on_unlink_decision() is safe *when the
    caller locks*. Nothing proved the caller does — deleting the `with
    self._unlink_decision_lock:` from the notLogged/QRCODE branch left the
    whole file green, which is the exact shape of the original incident (two
    wipes logged inside the same second). So this drives the real method,
    over a faked status-session response, from many threads at once.
    """

    def _stub(self, monkeypatch, *, probe=cs.LINK_PROBE_UNLINKED):
        stub = _Stub(probe=probe)
        stub._is_pairing_dialog_active = lambda: False
        stub._auto_restart_grace_active = lambda: False
        stub.check_wa_connection_http = (
            MainWindow.check_wa_connection_http.__get__(stub, _Stub))
        stub._LOGOUT_CONFIRM_STRIKES = MainWindow._LOGOUT_CONFIRM_STRIKES
        stub._RESUME_FAIL_STRIKES = MainWindow._RESUME_FAIL_STRIKES
        stub._HTTP_PROBE_STRIKES = MainWindow._HTTP_PROBE_STRIKES
        stub.wpp_server = "http://127.0.0.1"
        stub.wpp_port = 6300
        stub.token = "t"
        stub.settings = {"privateinfo": {"paired": True}}
        stub._wa_connected = True
        stub._wa_connect_announced = True   # connected this run → LOGOUT is reachable
        stub._wa_http_fail_strikes = 0
        stub._last_strike_ts = 0.0
        stub._set_wa_connected = lambda *a, **kw: None

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"status": "notLogged"}

        monkeypatch.setattr("main.api_get", lambda *a, **kw: _Resp())
        # Every reading must count, or the 20s strike interval collapses all
        # 25 concurrent readings into one and nothing ever overlaps inside
        # the decision — the region under test would go unexercised.
        monkeypatch.setattr("connection_state.should_count_strike",
                            lambda *a, **kw: True)
        return stub

    def test_concurrent_notlogged_readings_wipe_only_once(self, monkeypatch):
        _patch_wx(monkeypatch)
        stub = self._stub(monkeypatch)
        stub._logout_strikes = MainWindow._LOGOUT_CONFIRM_STRIKES

        _run_concurrently(stub.check_wa_connection_http, n=25)

        assert stub.disconnect_calls == [True]
        assert stub.error_sound.played == 1

    def test_concurrent_notlogged_readings_never_wipe_behind_a_veto(self, monkeypatch):
        _patch_wx(monkeypatch)
        stub = self._stub(monkeypatch, probe=cs.LINK_PROBE_LINKED)
        stub._logout_strikes = MainWindow._LOGOUT_CONFIRM_STRIKES

        _run_concurrently(stub.check_wa_connection_http, n=25)

        assert True not in stub.disconnect_calls

    def test_a_healthy_reading_clears_the_whole_tally(self, monkeypatch):
        """A non-unlinked status has to clear every counter a strike streak
        left behind, the veto run included — an unbroken run is the only
        thing _STILL_LINKED_VETO_LIMIT is counting."""
        _patch_wx(monkeypatch)
        stub = self._stub(monkeypatch)
        stub._logout_strikes = 3
        stub._resume_fail_strikes = 7
        stub._still_linked_vetoes = 2
        stub._last_strike_ts = 123.0

        class _Connected:
            status_code = 200

            @staticmethod
            def json():
                return {"status": "INITIALIZING"}

        monkeypatch.setattr("main.api_get", lambda *a, **kw: _Connected())
        _run_concurrently(stub.check_wa_connection_http, n=25)

        assert stub._logout_strikes == 0
        assert stub._resume_fail_strikes == 0
        assert stub._still_linked_vetoes == 0
        assert stub._last_strike_ts == 0.0
        assert stub.disconnect_calls == []

    def test_a_settling_restart_clears_the_whole_tally_too(self, monkeypatch):
        """The other reset of the same tally, and the one that omitted the
        veto run: an automatic _restart_wpp_session() tears the session down
        and builds it back, so the readings on either side of it are not one
        run of anything. Carrying four vetoes across it put the first
        confirmed logout after the restart straight on the limit, parking a
        session the probe says is linked on the pairing dialog."""
        _patch_wx(monkeypatch)
        stub = self._stub(monkeypatch)          # answers notLogged
        stub._auto_restart_grace_active = lambda: True
        stub._logout_strikes = 3
        stub._resume_fail_strikes = 7
        stub._still_linked_vetoes = 4
        stub._last_strike_ts = 123.0

        _run_concurrently(stub.check_wa_connection_http, n=25)

        assert stub._logout_strikes == 0
        assert stub._resume_fail_strikes == 0
        assert stub._still_linked_vetoes == 0
        assert stub._last_strike_ts == 0.0
        assert stub.disconnect_calls == []


# The shared tally: every attribute the count-then-decide sequence reads, and
# that a reset therefore may not clear from under it.
_TALLY_ATTRS = {
    "_logout_strikes",
    "_resume_fail_strikes",
    "_last_strike_ts",
    "_still_linked_vetoes",
}


@pytest.mark.parametrize("function_name", [
    "check_wa_connection_http",
    # Both writers of the tally, not just the one that happened to be checked
    # first: the 401/403 path counts into the very same attributes, from the
    # very same threads, so an unlocked reset there is the identical bug.
    "_handle_local_auth_rejected",
])
def test_the_tally_is_written_only_under_the_lock(function_name):
    """Source-level, because this one is genuinely not observable from
    outside: the healthy-status reset only ever *clears*, so racing it
    against the counting path converges on the same values either way and no
    behavioural test can tell locked from unlocked. What it can still do is
    clear a tally in the middle of another thread's count-then-decide — and
    the reason to fix it is the same reason the rest of the section is
    locked, so it is worth holding the code to it rather than trusting the
    comment. The reset sat outside the lock for exactly as long as nothing
    checked.
    """
    import ast
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[1]
              / "client" / "main.py").read_text(encoding="utf-8")
    function = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef)
        and node.name == function_name
    )

    def _is_the_lock(item):
        ctx = item.context_expr
        return (isinstance(ctx, ast.Attribute)
                and ctx.attr == "_unlink_decision_lock")

    guarded = [
        (node.lineno, max(child.lineno for child in ast.walk(node)
                          if hasattr(child, "lineno")))
        for node in ast.walk(function)
        if isinstance(node, ast.With) and any(_is_the_lock(i) for i in node.items)
    ]

    unguarded = []
    for node in ast.walk(function):
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AugAssign) else [])
        for target in targets:
            if not (isinstance(target, ast.Attribute) and target.attr in _TALLY_ATTRS):
                continue
            if not any(start <= node.lineno <= end for start, end in guarded):
                unguarded.append(f"main.py:{node.lineno} writes self.{target.attr}")

    assert unguarded == [], (
        "these writes to the shared unlink tally are outside "
        "`with self._unlink_decision_lock:`, so they can land in the middle "
        "of another thread's count-then-decide:\n" + "\n".join(unguarded)
    )
