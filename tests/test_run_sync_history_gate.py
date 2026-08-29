"""The history-sync gate as _run_sync() actually uses it.

Between the chat-list phase and the message phase, _run_sync() asks
unblock_history_sync() whether the phone's RECENT transfer is still running
and, if so, waits for it (_recent_history_needs_wait /
wait_for_restarted_history_sync). The gate has exactly two exits and both used
to skip sync_remote_chats():

  * the wait fails  -> an explicit `return` (deferral), by design;
  * the wait succeeds -> the very next line called get_remote_chats() with no
    arguments, and that method requires the dict to merge into. Every run that
    got this far raised TypeError, which start_sync()'s `except Exception`
    swallowed into a single "[start_sync] Unhandled error during sync" line.
    So on any account whose RECENT pass was still incomplete at sync time —
    i.e. any account with a real backlog — the message phase could not run at
    all, in either direction. The health checker then restarted the whole sync
    every few minutes, re-announcing "Sincronizando" each round, and
    _resolve_missing_group_names() (downstream of the crash) never ran, so
    groups stayed unnamed.

tests/test_history_sync_on_demand.py covers the wait and the gate predicate as
units — and did, the whole time. What it cannot see is the wiring around them,
which is the half that broke. These bind the real _run_sync() and let it run.

The stub harness is tests/test_run_sync_broken_store.py's: it is ~130 lines of
carefully-tuned wiring for this one method, and a second copy would drift from
the real loop exactly the way that module's own docstring warns about.
"""

import pytest

from main import MainWindow
from tests.test_run_sync_broken_store import _fast, _make  # noqa: F401  (_fast is an autouse fixture)


# A chat list that settles on the first pass, so every test here reaches the
# gate with the same uninteresting history behind it.
def _settled_stub(unblock_result, wait_result, outcome=None):
    """*outcome* is what the real wait records in _history_wait_outcome; it is
    what tells the two False cases apart ("timeout" vs "session_gone")."""
    stub = _make([931, 931], wa_web=937, local_chats=931)
    # These tests exercise the heavy recovery path specifically. Ordinary
    # warm-cache startup now skips RECENT restart/wait entirely and performs
    # an incremental delta refresh instead.
    stub._force_full_sync = True
    # Bound for real: it is the predicate that opens the gate. The stub's
    # __getattr__ answers unknown names with a lambda returning None, which is
    # falsy — leave it unbound and every test here silently exercises the
    # skip-the-gate path instead of the gate.
    stub._recent_history_needs_wait = MainWindow._recent_history_needs_wait
    stub.waits = 0
    stub._history_wait_outcome = ""
    stub._history_still_landing = False

    def _unblock(timeout=60):
        return unblock_result

    def _wait(timeout=600):
        stub.waits += 1
        if outcome is not None:
            stub._history_wait_outcome = outcome
        elif wait_result:
            stub._history_wait_outcome = "completed"
        return wait_result

    stub.unblock_history_sync = _unblock
    stub.wait_for_restarted_history_sync = _wait
    return stub


class TestTheGateIsSkippedEntirely:
    def test_a_completed_recent_pass_goes_straight_to_the_message_phase(self):
        stub = _settled_stub({"restarted": False, "recentCompleted": True}, None)
        stub._run_sync()
        assert stub.waits == 0
        assert stub.message_sync_ran == 1
        assert stub._sync_completed is True

    def test_an_unreadable_unblock_result_does_not_gate_anything(self):
        """unblock_history_sync() answers None on an API build with no such
        route. That must not be read as "the transfer is still running"."""
        stub = _settled_stub(None, None)
        stub._run_sync()
        assert stub.waits == 0
        assert stub.message_sync_ran == 1


class TestTheWaitSucceeds:
    """The branch that used to raise TypeError on every single run."""

    def test_the_message_phase_runs_after_the_wait(self):
        stub = _settled_stub({"restarted": True, "recentCompleted": False}, True)
        stub._run_sync()
        assert stub.waits == 1
        assert stub.message_sync_ran == 1, (
            "the wait succeeded and the message phase still did not run"
        )
        assert stub._sync_completed is True

    def test_the_refreshed_chat_list_is_fetched_with_the_dict_to_merge_into(self):
        """The point of the refresh: chats delivered *during* the wait join
        this same sync. get_remote_chats() merges into what it is handed and
        returns the result — it does not mutate self.chats — so calling it
        bare is not a style slip, it cannot work."""
        stub = _settled_stub({"restarted": True, "recentCompleted": False}, True)
        seen = []
        inner = stub.get_remote_chats

        def _record(chats, **kwargs):
            seen.append((dict(chats), kwargs))
            return inner(chats, **kwargs)

        stub.get_remote_chats = _record
        stub._run_sync()

        assert seen, "the post-wait refresh never fetched the chat list"
        merged_into, kwargs = seen[0]
        assert merged_into, "handed an empty dict — the merge would lose every chat"
        assert kwargs.get("persist_full") is False
        assert kwargs.get("notify_errors") is False

    def test_a_failed_refresh_keeps_the_chats_we_already_had(self):
        """None means "the chat list is unknown", never "there are no chats"
        — get_remote_chats()'s own contract. Overwriting on None would empty
        the list right before the message phase reads it."""
        stub = _settled_stub({"restarted": True, "recentCompleted": False}, True)
        before = dict(stub.chats)
        stub.get_remote_chats = lambda *a, **kw: None
        stub._run_sync()

        assert stub.chats == before
        assert stub.message_sync_ran == 1


class TestTheWaitTimesOut:
    """Reported live on a first pairing of a large account: the RECENT pass
    does not finish inside the wait's budget, so every round waited the full
    ten minutes, threw the round away and let the health check start another
    — sync_remote_chats() never ran once. Nothing was fetched, and
    _resolve_missing_group_names() (past the message phase) never ran either,
    so groups came back unnamed. A partial sync that fills in afterwards beats
    an empty one that repeats.
    """

    def test_the_message_phase_runs_anyway(self):
        stub = _settled_stub(
            {"restarted": True, "recentCompleted": False}, False, outcome="timeout"
        )
        stub._run_sync()
        assert stub.waits == 1
        assert stub.message_sync_ran == 1, (
            "a budget that ran out is not a reason to fetch nothing at all"
        )

    def test_the_chats_are_marked_as_still_landing(self):
        """What makes the partial sync safe: _note_backfill_state() reads this
        to decide that a chat answering short is provisional and has to be
        re-queried, instead of taking it at face value."""
        stub = _settled_stub(
            {"restarted": True, "recentCompleted": False}, False, outcome="timeout"
        )
        # The real refresh_history_still_landing() would re-read the flag from
        # a live status call; the stub's returns False, so capture the value
        # the gate itself set, before that.
        seen = {}
        inner = stub.refresh_history_still_landing

        def _record(context=""):
            seen["landing_at_entry"] = stub._history_still_landing
            return inner(context=context)

        stub.refresh_history_still_landing = _record
        stub._run_sync()

        assert seen.get("landing_at_entry") is True

    def test_the_chat_list_is_still_refreshed_after_the_wait(self):
        """Chats can have landed during the wait either way, so the refresh is
        not exclusive to the completed path.

        Identified by elimination rather than by call count: the settle loop's
        own fetches are the ones carrying prune_stale=True, and the refresh
        after the message phase happens once message_sync_ran is already 1. A
        fetch with neither is the post-wait one.
        """
        stub = _settled_stub(
            {"restarted": True, "recentCompleted": False}, False, outcome="timeout"
        )
        calls = []
        inner = stub.get_remote_chats

        def _record(chats, **kwargs):
            calls.append((kwargs.get("prune_stale"), stub.message_sync_ran))
            return inner(chats, **kwargs)

        stub.get_remote_chats = _record
        stub._run_sync()

        post_wait = [
            c for c in calls if not c[0] and c[1] == 0
        ]
        assert post_wait, f"no post-wait chat-list refresh among {calls}"


class TestTheSessionGoesAway:
    def test_the_message_phase_is_deferred_and_the_sync_is_not_marked_complete(self):
        """The one case where deferring is still right: there is no session
        left to run get-messages against."""
        stub = _settled_stub(
            {"restarted": True, "recentCompleted": False}, False, outcome="session_gone"
        )
        stub._run_sync()
        assert stub.waits == 1
        assert stub.message_sync_ran == 0
        assert stub.media_sync_ran == 0
        assert stub._sync_completed is False


class TestBothExitsAreReachable:
    @pytest.mark.parametrize("wait_result", (True, False))
    def test_the_gate_never_raises(self, wait_result):
        """The regression was an exception, not a wrong decision — and
        start_sync() catches Exception, so nothing downstream ever noticed.
        Whatever the wait answers, _run_sync() must return normally."""
        stub = _settled_stub({"restarted": True, "recentCompleted": False}, wait_result)
        stub._run_sync()  # must not raise
