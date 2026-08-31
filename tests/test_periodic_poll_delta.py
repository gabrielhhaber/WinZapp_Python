"""What the 60-second background poll is allowed to do to the chat list.

WPPConnect Server never relays a socket event for anything changed on the phone
or another linked device, so this poll is the only way those reach WinZapp. It
used to answer that by re-syncing every chat's messages, which on a large
account is a full sync every minute; it now captures the same activity baseline
_run_sync() uses and queries only the chats whose marker moved.

Two properties of that are load-bearing and neither is visible from reading the
loop: the chat-list metadata save must wait for every selected delta to succeed
(a committed marker with no message behind it makes the next launch skip the
chat for good), and the poll must not promote history repairs to full re-syncs
— include_repairs=False — or the most expensive conversation on the account is
re-fetched in full once a minute, forever.

start_periodic_contacts_sync() spawns a thread running an endless loop, so the
loop is captured instead of started and one iteration is driven by hand.
MainWindow is a wx.Frame and cannot be instantiated, so the method is bound to
a plain stub — the same pattern the other main.py tests use.
"""

import threading
import types

import pytest

import main
from main import MainWindow
from tests.conftest import warm_cached_chat as _chat


A, B = "5511900000000@s.whatsapp.net", "5511911111111@s.whatsapp.net"


class _StopLoop(Exception):
    """Raised from the loop's own sleep to end it after one iteration."""


class _PollStub:
    def __init__(self):
        self.chats = {A: _chat(A), B: _chat(B)}
        self.settings = {"storage": {"auto_download_media": True}}
        self._wa_connected = True
        self._initial_sync_running = False
        self._media_sync_running = False
        self._history_still_landing = False
        self._history_gap_jids = set()
        self._chats_awaiting_messages = set()
        self._message_retry_jids = set()
        self._backfill_state_lock = threading.RLock()
        self._lid_to_phone = {}
        self._phone_to_lid = {}

        # what the test inspects
        self.message_rounds = []
        self.media_scopes = []
        self.saves = 0
        self.contact_fetches = 0
        self.save_data_calls = 0
        self.reconciles = 0
        self.failing_jids = set()
        # {jid: new activity timestamp} the next list-chats answer reports
        self.server_activity = {}

    # ── bound for real: the decisions under test ─────────────────────────
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _server_claims_content = staticmethod(MainWindow._server_claims_content)
    _capture_chat_sync_baseline = MainWindow._capture_chat_sync_baseline
    _baseline_marker_for_jid = MainWindow._baseline_marker_for_jid
    _plan_message_sync = MainWindow._plan_message_sync
    _jid_address_forms = MainWindow._jid_address_forms

    # ── collaborators ────────────────────────────────────────────────────
    def get_remote_contacts(self):
        self.contact_fetches += 1

    def get_block_list(self):
        pass

    def get_remote_chats(self, chats, persist_full=True, notify_errors=True,
                         prune_stale=None, defer_chat_save=False):
        merged = dict(chats)
        for jid, activity in self.server_activity.items():
            chat = dict(merged[jid])
            chat["t"] = activity
            merged[jid] = chat
        # Mirrors the real method's own write decision (main.py, end of
        # get_remote_chats()). Without it a caller that drops
        # defer_chat_save=True is invisible here: the poll would commit the new
        # activity markers immediately, before knowing whether the delta they
        # selected succeeded, and TestThePollDoesNotCommitAFailedDelta would
        # still pass.
        if persist_full:
            self.save_data_calls += 1
        elif not defer_chat_save:
            self._schedule_save()
        return merged

    def sync_remote_chats(self, target_chats=None, incremental=False):
        self.message_rounds.append((
            incremental,
            sorted(c.get("remoteJid") for c in (target_chats or [])),
        ))
        return set(self.failing_jids)

    def sync_media_for_all_chats(self, jids=None):
        self.media_scopes.append(None if jids is None else set(jids))
        return 0

    def _schedule_save(self, **kwargs):
        self.saves += 1

    def _schedule_set_chats(self):
        pass

    def _reconcile_active_conversation_with_remote(self):
        # Last statement of the loop body, which the loop wraps in a bare
        # `except Exception`. The negative tests below assert on this so a
        # swallowed error cannot masquerade as "no message round happened".
        self.reconciles += 1


def _cycles(stub, monkeypatch, count=1):
    """Run exactly *count* passes of the poll loop.

    The loop is endless and its body is wrapped in a bare `except Exception`,
    so it is ended from the one statement outside that guard — its own sleep.
    """
    ticks = {"n": 0}

    def _sleep(_seconds):
        ticks["n"] += 1
        if ticks["n"] > count:
            raise _StopLoop

    captured = []
    monkeypatch.setattr(main.time, "sleep", _sleep)
    monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **kw: None)
    monkeypatch.setattr(main.threading, "Thread",
                        lambda target=None, **kw: types.SimpleNamespace(
                            start=lambda: captured.append(target)))

    MainWindow.start_periodic_contacts_sync(stub)
    assert captured, "the poll never started its thread"
    with pytest.raises(_StopLoop):
        captured[0]()


class TestThePollFetchesADeltaAndNotTheAccount:
    def test_an_unchanged_account_costs_no_get_messages_call(self, monkeypatch):
        stub = _PollStub()
        _cycles(stub, monkeypatch)
        assert stub.message_rounds == []
        assert stub.reconciles == 1

    def test_a_chat_changed_on_the_phone_gets_an_incremental_delta(self, monkeypatch):
        stub = _PollStub()
        stub.server_activity = {A: 500}
        _cycles(stub, monkeypatch)
        assert stub.message_rounds == [(True, [A])]

    def test_the_media_scan_follows_the_same_scope(self, monkeypatch):
        """A media message missed by the socket should behave like a live one —
        without the 60-second fallback turning into a global media rescan."""
        stub = _PollStub()
        stub.server_activity = {A: 500}
        _cycles(stub, monkeypatch)
        assert stub.media_scopes == [{A}]

    def test_a_failed_delta_is_left_out_of_the_media_scope(self, monkeypatch):
        stub = _PollStub()
        stub.server_activity = {A: 500, B: 500}
        stub.failing_jids = {A}
        _cycles(stub, monkeypatch)
        assert stub.media_scopes == [{B}]

    def test_a_chat_with_no_local_history_is_fetched_in_full(self, monkeypatch):
        """The poll has a full pass as well as an incremental one, and it is
        not decoration. A chat the server reports activity for while the local
        cache holds no records at all classifies "missing-local-history" — a
        conversation that would otherwise stay visibly empty until the next
        full _run_sync(), i.e. until the app is restarted. An incremental
        window cannot serve it: there is nothing local for the fetched window
        to overlap with.
        """
        stub = _PollStub()
        stub.chats[A] = _chat(A, records=0)
        _cycles(stub, monkeypatch)
        assert stub.message_rounds == [(False, [A])]

    def test_a_chat_owing_history_repair_is_not_promoted_to_a_full_resync(self, monkeypatch):
        """include_repairs=False. The repair queue belongs to the backfill and
        to _run_sync(); honouring it here would re-fetch the whole of the
        account's biggest conversation once a minute for the rest of the
        session, which is exactly what the incremental poll exists to stop."""
        stub = _PollStub()
        stub._chats_awaiting_messages = {A}
        _cycles(stub, monkeypatch)
        assert stub.message_rounds == []
        assert stub.reconciles == 1


class TestThePollDoesNotCommitAFailedDelta:
    def test_a_clean_pass_persists_the_chat_list_metadata(self, monkeypatch):
        stub = _PollStub()
        stub.server_activity = {A: 500}
        _cycles(stub, monkeypatch)
        assert stub.saves == 1

    def test_a_failed_delta_leaves_the_old_marker_on_disk(self, monkeypatch):
        """Keeping the stale marker guarantees the next process rediscovers the
        change even if this one dies before its in-memory retry latch is
        written."""
        stub = _PollStub()
        stub.server_activity = {A: 500}
        stub.failing_jids = {A}
        _cycles(stub, monkeypatch)
        assert stub.saves == 0


class TestTheContactListIsPolledFarLessOften:
    """Chat state is polled every minute because nothing else delivers a
    phone-side read/pin/archive change at all. The contact list is heavy and
    changes rarely, so it keeps the five-minute schedule it always had — the
    two cadences share one loop and one elapsed counter."""

    def test_a_single_minute_does_not_refetch_contacts(self, monkeypatch):
        stub = _PollStub()
        _cycles(stub, monkeypatch, count=1)
        assert stub.contact_fetches == 0

    def test_the_fifth_minute_does(self, monkeypatch):
        stub = _PollStub()
        _cycles(stub, monkeypatch, count=5)
        assert stub.contact_fetches == 1

    def test_and_the_counter_restarts_from_there(self, monkeypatch):
        """elapsed is reset, not compared against a running total — otherwise
        every cycle past the fifth would refetch."""
        stub = _PollStub()
        _cycles(stub, monkeypatch, count=6)
        assert stub.contact_fetches == 1
