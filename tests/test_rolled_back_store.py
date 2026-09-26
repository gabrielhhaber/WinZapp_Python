"""A WhatsApp Web store that holds LESS than we do must not rewrite local state.

Both halves come from one live incident (2026-09-15):

1. An open group lost 199 messages, then 50 more. WhatsApp Web had unloaded
   the chat down to one or two messages; a new one arrived, get-messages
   returned it, and _reconcile_active_conversation_with_remote() mirrored every
   stored message older than it as a phone-side deletion. Minutes later the
   browser profile was restored from a snapshot a day old, so neither side had
   those messages any more.
2. After the restore, the unread counts went back near zero. get_remote_chats()
   copied every chat's rolled-back `t` over the local one; on the next round
   reconcile_snapshot_unread() saw the snapshot as current and took its counts —
   from a snapshot taken after an accidental mark-all-as-read.
"""

import time
import types

import pytest
import wx

import main as main_module
from core.incremental_sync import chat_activity_floor
from core.remote_reconcile import deletions_within_remote_window, remote_window_oldest
from core.remote_reconcile import (
    add_rollback_gap as _add_rollback_gap,
    normalize_rollback_gaps as _normalize_rollback_gaps,
)
from main import MainWindow
from tests.test_get_remote_chats_persistence import _chat, _make, post  # noqa: F401 (fixture)
from tests.god_modules import patch_main_global

GROUP = "120363409931936700@g.us"


def _msg(mid, ts, **extra):
    record = {
        "key": {"id": mid, "fromMe": False, "remoteJid": GROUP},
        "message": {"conversation": "oi"},
        "messageType": "conversation",
        "messageTimestamp": ts,
    }
    record.update(extra)
    return record


# ── the pure rules ──────────────────────────────────────────────────────────


class TestRemoteWindow:
    def test_oldest_is_the_earliest_timestamp_returned(self):
        assert remote_window_oldest([_msg("a", 300), _msg("b", 100), _msg("c", 200)]) == 100

    def test_milliseconds_are_read_as_seconds(self):
        assert remote_window_oldest([_msg("a", 1_700_000_000_123)]) == 1_700_000_000

    def test_no_timestamps_means_no_window(self):
        assert remote_window_oldest([{"key": {"id": "a"}}]) == 0
        assert remote_window_oldest([]) == 0


class TestDeletionsWithinRemoteWindow:
    def test_a_message_inside_the_window_and_absent_is_deleted(self):
        local = [_msg("old", 100), _msg("gone", 250), _msg("kept", 300)]
        assert deletions_within_remote_window(local, {"kept"}, 200) == {"gone"}

    def test_a_message_older_than_the_window_is_never_judged(self):
        """The incident: the store held only the newest message, and every
        older stored message looked deleted."""
        local = [_msg(str(i), 1000 + i) for i in range(199)] + [_msg("new", 5000)]
        assert deletions_within_remote_window(local, {"new"}, 5000) == set()

    def test_an_empty_answer_proves_nothing_here(self):
        assert deletions_within_remote_window([_msg("a", 100)], set(), 0) == set()

    def test_an_answer_without_timestamps_proves_nothing(self):
        assert deletions_within_remote_window([_msg("a", 100)], {"b"}, 0) == set()

    def test_siblings_cut_off_in_the_oldest_second_are_not_judged(self):
        """An album: five messages in one second, the count cut keeps two.
        The three left out share the oldest timestamp and are only not loaded."""
        local = [_msg(f"p{i}", 1000) for i in range(5)] + [_msg("n", 1001)]
        assert deletions_within_remote_window(local, {"p3", "p4", "n"}, 1000) == set()

    def test_a_message_in_the_oldest_second_is_not_judged_even_if_really_gone(self):
        """The price of the rule above, accepted: cosmetic, never destructive."""
        local = [_msg("same-second", 1000), _msg("kept", 1000), _msg("n", 1001)]
        assert deletions_within_remote_window(local, {"kept", "n"}, 1000) == set()


class TestActivityFloor:
    def test_the_newest_countable_message_sets_the_floor(self):
        chat = {"messages": {"messages": {"records": [_msg("a", 100), _msg("b", 400)]}}}
        assert chat_activity_floor(chat, lambda m: True) == 400

    def test_a_non_countable_record_cannot_raise_it(self):
        chat = {"messages": {"messages": {"records": [
            _msg("a", 100), _msg("join", 900, messageType="groupNotification")]}}}
        assert chat_activity_floor(chat, lambda m: m.get("messageType") != "groupNotification") == 100

    def test_no_records_means_no_floor(self):
        assert chat_activity_floor({}, lambda m: True) == 0

    def test_a_filter_that_raises_counts_as_not_countable(self):
        def boom(_m):
            raise RuntimeError("boom")
        chat = {"messages": {"messages": {"records": [_msg("a", 100)]}}}
        assert chat_activity_floor(chat, boom) == 0

    def test_a_message_still_pending_locally_is_ignored(self):
        chat = {"messages": {"messages": {"records": [
            _msg("sent", 100), _msg("pending", 900, _local_pending=True)]}}}
        assert chat_activity_floor(chat, lambda m: True) == 100

    def test_a_message_stamped_in_the_future_is_ignored(self):
        """Skipped, not clamped to now: a clamped floor would climb with the
        clock every round and read as new activity each time."""
        chat = {"messages": {"messages": {"records": [
            _msg("past", 3000), _msg("future", 5000)]}}}
        assert chat_activity_floor(chat, lambda m: True, now=4000) == 3000

    def test_the_floor_is_stable_across_rounds_while_the_clock_is_ahead(self):
        chat = {"messages": {"messages": {"records": [
            _msg("past", 3000), _msg("future", 5000)]}}}
        floors = {chat_activity_floor(chat, lambda m: True, now=now) for now in (4000, 4060, 4120)}
        assert floors == {3000}


# ── the reconcile, end to end ───────────────────────────────────────────────


class _Panel:
    def __init__(self, jid):
        self.conversation = {"remoteJid": jid}
        self.removed = None
        self.populate_called = False
        self.selected_messages = {"marked"}

    def remove_messages_by_id(self, ids, focus_previous=False):
        self.removed = set(ids)

    def populate_messages(self):
        self.populate_called = True


class _ReconcileStub:
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _reconcile_active_conversation_with_remote = MainWindow._reconcile_active_conversation_with_remote
    _rollback_gaps = MainWindow._rollback_gaps
    _legacy_restore_gap = MainWindow._legacy_restore_gap
    _ROLLBACK_GAPS_METADATA_KEY = MainWindow._ROLLBACK_GAPS_METADATA_KEY
    _deletions_before_remote_window = MainWindow._deletions_before_remote_window
    _mirror_remote_clear = MainWindow._mirror_remote_clear
    _mirror_remote_deletions = MainWindow._mirror_remote_deletions
    _REMOTE_CLEAR_CONFIRM_STRIKES = MainWindow._REMOTE_CLEAR_CONFIRM_STRIKES
    _REMOTE_BEFORE_PAGES = MainWindow._REMOTE_BEFORE_PAGES

    def __init__(self, records, remote, before=None):
        """*remote* is (ids, oldest ts) for the newest window, anchored at
        "anchor". *before* maps an anchor to the page before it, as returned
        by _page(); an anchor it does not know is a failed fetch."""
        self.chats = {GROUP: {"remoteJid": GROUP, "messages": {"messages": {"records": records}}}}
        self.conversations_panel = _Panel(GROUP)
        self.messages_set_completed = True
        self.settings = {}
        self._remote = remote
        self._before = before or {}
        self.before_calls = []
        self.clear_calls = []

    def _fetch_remote_message_window(self, remote_jid):
        ids, oldest = self._remote
        return set(ids), oldest, ("anchor" if ids else "")

    def _fetch_remote_messages_before(self, remote_jid, anchor_id):
        self.before_calls.append(anchor_id)
        return self._before.get(anchor_id)

    def clear_chat_messages_local(self, jid, record_cutoff=True):
        self.clear_calls.append(jid)

    def _schedule_set_chats(self):
        pass


@pytest.fixture(autouse=True)
def _sync_call_after(monkeypatch):
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))


def _old_history(n=200):
    base = int(time.time()) - 86_400
    return [_msg(f"m{i}", base + i) for i in range(n)]


STRIKES = MainWindow._REMOTE_CLEAR_CONFIRM_STRIKES


def _ids(records):
    return {r["key"]["id"] for r in records}


def _page(records):
    """What _fetch_remote_messages_before() answers for these records."""
    if not records:
        return set(), 0, ""
    oldest = min(records, key=lambda r: r["messageTimestamp"])
    return _ids(records), oldest["messageTimestamp"], "ser_" + oldest["key"]["id"]


def _poll(stub, times=1):
    for _ in range(times):
        stub._reconcile_active_conversation_with_remote()


class TestAShrunkenWindowIsNotADeletion:
    def test_older_messages_the_database_still_holds_are_kept(self):
        """The window held only a message newer than all local history (its
        own paging behind it stopped early); the older ones are still in
        WhatsApp Web's database, one anchored page away."""
        history = _old_history()
        stub = _ReconcileStub(history, ({"brand-new"}, int(time.time()) - 600),
                              before={"anchor": _page(history)})

        _poll(stub, STRIKES + 1)

        assert stub.conversations_panel.removed is None
        assert stub.clear_calls == []
        assert stub.before_calls[0] == "anchor"

    def test_it_is_not_counted_as_a_clear_either(self):
        """A non-empty answer sharing no id with local history used to count
        as a clear strike; three polls of it wiped the conversation."""
        history = _old_history()
        stub = _ReconcileStub(history, ({"brand-new"}, int(time.time()) - 600),
                              before={"anchor": _page(history)})

        _poll(stub, STRIKES + 1)

        assert stub.clear_calls == []

    def test_a_mass_apparent_deletion_waits_for_confirmation(self):
        """A stray old message in the answer pulls its oldest timestamp back
        and everything after it looks deleted. Past the cap it is not mirrored
        from one read — but it is mirrored once confirmed, since a bulk
        deletion on the phone has exactly this shape."""
        history = _old_history(40)
        kept = _ids(history[:2]) | _ids(history[-2:])
        stub = _ReconcileStub(history, (kept, history[0]["messageTimestamp"]))

        _poll(stub, STRIKES - 1)
        assert stub.conversations_panel.removed is None

        _poll(stub)
        assert stub.conversations_panel.removed == _ids(history[2:-2])

    def test_leaving_the_chat_does_not_lose_the_confirmation(self):
        history = _old_history(40)
        kept = _ids(history[:2]) | _ids(history[-2:])
        stub = _ReconcileStub(history, (kept, history[0]["messageTimestamp"]))

        _poll(stub, STRIKES - 1)
        stub.conversations_panel.conversation = {"remoteJid": "someone-else@g.us"}
        _poll(stub)
        stub.conversations_panel.conversation = {"remoteJid": GROUP}
        _poll(stub)

        assert stub.conversations_panel.removed == _ids(history[2:-2])

    def test_a_message_seen_again_is_never_mirrored(self):
        """A read that missed the messages, then one that found them: the run
        starts over, so a wobbling answer cannot confirm anything."""
        history = _old_history(40)
        kept = _ids(history[:2]) | _ids(history[-2:])
        stub = _ReconcileStub(history, (kept, history[0]["messageTimestamp"]))

        _poll(stub, STRIKES - 1)
        stub._remote = (_ids(history), history[0]["messageTimestamp"])
        _poll(stub)
        stub._remote = (kept, history[0]["messageTimestamp"])
        _poll(stub, STRIKES - 1)

        assert stub.conversations_panel.removed is None

    def test_a_deletion_at_the_cap_is_still_mirrored(self):
        from core.remote_reconcile import MAX_MIRRORED_DELETIONS
        history = _old_history(30)
        gone = {r["key"]["id"] for r in history[5:5 + MAX_MIRRORED_DELETIONS]}
        remote_ids = {r["key"]["id"] for r in history} - gone
        stub = _ReconcileStub(history, (remote_ids, history[0]["messageTimestamp"]))

        stub._reconcile_active_conversation_with_remote()

        assert stub.conversations_panel.removed == gone

    def test_a_real_deletion_inside_the_window_is_still_mirrored(self):
        history = _old_history(10)
        remote_ids = {r["key"]["id"] for r in history} - {"m7"}
        oldest = history[0]["messageTimestamp"]
        stub = _ReconcileStub(history, (remote_ids, oldest))

        stub._reconcile_active_conversation_with_remote()

        assert stub.conversations_panel.removed == {"m7"}

    def test_a_partially_loaded_window_only_judges_what_it_covers(self):
        history = _old_history(10)
        covered = history[6:]                      # window holds m6..m9 only
        remote_ids = _ids(covered) - {"m8"}
        stub = _ReconcileStub(history, (remote_ids, covered[0]["messageTimestamp"]),
                              before={"anchor": _page(history[:6])})

        _poll(stub, STRIKES + 1)

        assert stub.conversations_panel.removed == {"m8"}

    def test_an_empty_answer_still_mirrors_a_clear_after_the_strikes(self):
        stub = _ReconcileStub(_old_history(5), (set(), 0))

        _poll(stub, STRIKES)

        assert stub.clear_calls == [GROUP]
        # The marked messages went with the clear, so the selection mode must
        # not stay on over them.
        assert stub.conversations_panel.selected_messages == set()


class TestOlderDeletionsStillArrive:
    """A deletion a page of WhatsApp Web's database PROVES is mirrored, after
    confirmation. What the walk cannot account for is kept: that rule came from
    main (PR #248, core/remote_deletions.py) and won the merge, because a
    message WhatsApp Web's store never held or has lost answers exactly like a
    deleted one, and removing it deletes the only complete copy."""

    def test_the_oldest_messages_of_a_short_chat_are_kept_at_the_end_of_history(self):
        """WhatsApp Web holds m2..m9 and nothing before them. That is a
        delete-for-me of the first two — or a store that never had them."""
        history = _old_history(10)
        stub = _ReconcileStub(history, (_ids(history[2:]), history[2]["messageTimestamp"]),
                              before={"anchor": _page([])})

        _poll(stub, STRIKES + 1)
        assert stub.conversations_panel.removed is None

    def test_a_clear_on_the_phone_with_new_messages_since_is_not_mirrored(self):
        """Accepted cost of keeping what cannot be proven: the window holds only
        the new message and nothing exists before it, which is also exactly
        what a store holding little history looks like."""
        history = _old_history(30)
        stub = _ReconcileStub(history, ({"new"}, int(time.time()) - 600),
                              before={"anchor": _page([])})

        _poll(stub, STRIKES)

        assert stub.conversations_panel.removed is None
        assert stub.clear_calls == []

    def test_a_deletion_found_further_back_across_pages(self):
        history = _old_history(30)
        stub = _ReconcileStub(
            history, (_ids(history[20:]), history[20]["messageTimestamp"]),
            before={
                "anchor": _page([r for r in history[10:20] if r["key"]["id"] != "m15"]),
                "ser_m10": _page(history[:10]),
            })

        _poll(stub, STRIKES)

        assert stub.conversations_panel.removed == {"m15"}
        assert stub.before_calls[:2] == ["anchor", "ser_m10"]

    def test_a_look_further_back_that_keeps_failing_deletes_nothing(self):
        """A persistently failing page (an endpoint broken by an upgrade) must
        not turn into deleting the older history of every chat opened."""
        history = _old_history(10)
        stub = _ReconcileStub(history, (_ids(history[2:]), history[2]["messageTimestamp"]))

        _poll(stub, STRIKES + 1)
        assert stub.conversations_panel.removed is None

    def test_a_batch_pushed_out_of_the_slice_while_confirming_is_still_mirrored(self):
        """A busy chat: 11 messages deleted on the phone, and new ones keep
        arriving during the confirmation polls, pushing the deleted ones out of
        the last messages_page_size records."""
        history = _old_history(20)
        base = history[0]["messageTimestamp"]
        remote = {"m0"} | _ids(history[12:])
        stub = _ReconcileStub(history, (remote, base))
        stub.settings = {"user_interface": {"messages_page_size": 20}}

        for poll in range(STRIKES):
            _poll(stub)
            new = [_msg(f"n{poll}_{k}", base + 100 + poll * 10 + k) for k in range(5)]
            history.extend(new)
            remote |= _ids(new)
            stub._remote = (remote, base)

        assert stub.conversations_panel.removed == {f"m{i}" for i in range(1, 12)}

    def test_running_out_of_pages_keeps_what_was_not_reached(self):
        history = _old_history(30)
        before = {"anchor": _page([history[28]])}
        before.update({f"ser_m{k}": _page([history[k - 1]]) for k in range(28, 1, -1)})
        stub = _ReconcileStub(history, ({"m29"}, history[29]["messageTimestamp"]), before=before)

        _poll(stub, STRIKES)

        assert len(stub.before_calls) == STRIKES * MainWindow._REMOTE_BEFORE_PAGES
        assert stub.conversations_panel.removed is None

    def test_a_page_that_does_not_move_back_keeps_the_older_messages(self):
        history = _old_history(10)
        stub = _ReconcileStub(
            history, (_ids(history[2:]), history[2]["messageTimestamp"]),
            before={"anchor": ({"m2"}, history[2]["messageTimestamp"], "anchor")})

        _poll(stub, STRIKES)

        assert stub.conversations_panel.removed is None

    def test_a_direct_deletion_is_not_delayed_by_an_older_one(self):
        history = _old_history(10)
        window = [r for r in history[2:] if r["key"]["id"] != "m7"]
        stub = _ReconcileStub(history, (_ids(window), history[2]["messageTimestamp"]),
                              before={"anchor": _page([])})

        _poll(stub)

        assert stub.conversations_panel.removed == {"m7"}


class _FetchStub:
    _fetch_remote_message_window = MainWindow._fetch_remote_message_window
    _fetch_remote_messages_before = MainWindow._fetch_remote_messages_before

    def __init__(self, answer):
        self.answer = answer
        self.queries = []

    def _get_remote_messages(self, remote_jid, extra_query=""):
        self.queries.append(extra_query)
        return self.answer


def _raw_pair(mid, ts):
    return _msg(mid, ts), {"id": {"_serialized": f"false_{GROUP}_{mid}"}}


class TestTheFetches:
    def test_the_window_names_its_oldest_message_as_the_anchor(self):
        stub = _FetchStub(([_raw_pair("b", 200), _raw_pair("a", 100)], 2, 100))
        assert stub._fetch_remote_message_window(GROUP) == ({"a", "b"}, 100, f"false_{GROUP}_a")

    def test_the_page_before_is_asked_with_the_encoded_anchor(self):
        stub = _FetchStub(([_raw_pair("a", 100)], 1, 100))
        assert stub._fetch_remote_messages_before(GROUP, "false_x@g.us_A B") == (
            {"a"}, 100, f"false_{GROUP}_a")
        assert stub.queries == ["&direction=before&id=false_x%40g.us_A%20B"]

    def test_an_empty_page_is_the_end_of_history(self):
        assert _FetchStub(([], 0, None))._fetch_remote_messages_before(GROUP, "x") == (set(), None, "")

    def test_unreadable_items_are_a_failure_not_an_empty_page(self):
        assert _FetchStub(([], 3, 100))._fetch_remote_messages_before(GROUP, "x") is None

    def test_a_failed_request_is_a_failure(self):
        assert _FetchStub(None)._fetch_remote_messages_before(GROUP, "x") is None
        assert _FetchStub(None)._fetch_remote_message_window(GROUP) is None


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _GetStub:
    _get_remote_messages = MainWindow._get_remote_messages

    def __init__(self):
        self.ws = types.SimpleNamespace(_normalize_wpp_message=self._normalize)
        self.settings = {"user_interface": {"messages_page_size": 50}}
        self._phone_to_lid = {}
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "tok"

    @staticmethod
    def _normalize(wm):
        if wm.get("bad"):
            raise ValueError("unreadable")
        return {"key": {"id": wm["id"]["_serialized"].split("_")[-1]},
                "messageTimestamp": wm["t"]}


class TestGetRemoteMessages:
    def test_builds_the_url_and_counts_raw_items(self, monkeypatch):
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            return _Response(200, {"response": [
                {"id": {"_serialized": "false_5511@c.us_A"}, "t": 100}, {"bad": True}]})

        patch_main_global(monkeypatch, "api_get", fake_get)
        pairs, raw_count, oldest = _GetStub()._get_remote_messages(
            "5511@s.whatsapp.net", "&direction=before&id=x")

        assert calls == ["http://127.0.0.1:6300/api/tok/get-messages/5511@c.us"
                         "?count=50&direction=before&id=x"]
        assert [n["key"]["id"] for n, _raw in pairs] == ["A"]
        assert raw_count == 2
        # Read off the raw items, the unreadable one included.
        assert oldest == 100

    def test_an_error_status_is_a_failure(self, monkeypatch):
        patch_main_global(monkeypatch, "api_get", lambda url, **kw: _Response(500, {}))
        assert _GetStub()._get_remote_messages("5511@s.whatsapp.net") is None


# ── the chat-list merge ─────────────────────────────────────────────────────


JID = "5511900000001@s.whatsapp.net"


def _cached(t, unread, records):
    return {JID: {"remoteJid": JID, "t": t, "unreadCount": unread,
                  "messages": {"messages": {"records": records}}}}


def _record(ts):
    return {"key": {"id": f"id{ts}", "fromMe": False, "remoteJid": JID},
            "message": {"conversation": "oi"}, "messageType": "conversation",
            "messageTimestamp": ts}


class TestARolledBackSnapshotCannotRewindTheList:
    def test_t_is_not_lowered_below_a_message_we_hold(self, post):
        cached = _cached(1_700_000_500, 4, [_record(1_700_000_500)])
        post["payload"] = [_chat("5511900000001@c.us", t=1_700_000_000, unreadCount=0)]

        result = _make(cached).get_remote_chats(dict(cached), persist_full=False, notify_errors=False)

        assert result[JID]["t"] == 1_700_000_500

    def test_the_unread_count_survives_the_second_round(self, post):
        """The incident chain: round one used to rewind `t`, round two then
        accepted the snapshot's near-zero count."""
        cached = _cached(1_700_000_500, 4, [_record(1_700_000_500)])
        post["payload"] = [_chat("5511900000001@c.us", t=1_700_000_000, unreadCount=0)]
        stub = _make(cached)

        first = stub.get_remote_chats(dict(cached), persist_full=False, notify_errors=False)
        second = stub.get_remote_chats(dict(first), persist_full=False, notify_errors=False)

        assert second[JID]["unreadCount"] == 4
        assert second[JID]["t"] == 1_700_000_500

    def test_a_newer_snapshot_still_moves_t_forward(self, post):
        cached = _cached(1_700_000_500, 0, [_record(1_700_000_500)])
        post["payload"] = [_chat("5511900000001@c.us", t=1_700_000_900, unreadCount=1)]

        result = _make(cached).get_remote_chats(dict(cached), persist_full=False, notify_errors=False)

        assert result[JID]["t"] == 1_700_000_900
        assert result[JID]["unreadCount"] == 1

    def test_a_chat_with_no_stored_messages_behaves_as_before(self, post):
        cached = _cached(1_700_000_500, 0, [])
        post["payload"] = [_chat("5511900000001@c.us", t=1_700_000_000, unreadCount=0)]

        result = _make(cached).get_remote_chats(dict(cached), persist_full=False, notify_errors=False)

        assert result[JID]["t"] == 1_700_000_000


class TestARestoredStoreHasAHoleNotDeletions:
    """A profile restore rolls WhatsApp Web's database back to the snapshot,
    and what arrived in between is never put back there: a hole in the middle
    of the chat. A page before the anchor is contiguous from the database's
    point of view, so every local message in the hole "is inside the page's
    period and absent" on every poll — found in review, m10..m19 mirrored as
    phone-side deletions after three polls. Restores now record that period
    for good, and nothing stamped inside it is judged."""

    def _stub_with_hole(self):
        history = _old_history(30)
        stub = _ReconcileStub(
            history, (_ids(history[20:]), history[20]["messageTimestamp"]),
            before={"anchor": _page(history[:10])})  # m10..m19 lost by the store
        return history, stub

    def test_without_a_recorded_restore_the_hole_would_be_mirrored(self):
        """Pins that the fixture reproduces the defect."""
        _history, stub = self._stub_with_hole()
        _poll(stub, STRIKES)
        assert stub.conversations_panel.removed == {f"m{i}" for i in range(10, 20)}

    def test_a_recorded_restore_keeps_the_hole(self):
        history, stub = self._stub_with_hole()
        stub._rollback_gaps_cache = _add_rollback_gap(
            [], history[10]["messageTimestamp"], history[19]["messageTimestamp"])
        _poll(stub, STRIKES + 1)
        assert stub.conversations_panel.removed is None

    def test_a_real_deletion_outside_the_period_still_arrives(self):
        base = int(time.time()) - 400_000
        history = [_msg(f"m{i}", base + i * 10_000) for i in range(30)]
        window = [r for r in history[20:] if r["key"]["id"] != "m25"]
        stub = _ReconcileStub(history, (_ids(window), history[20]["messageTimestamp"]),
                              before={"anchor": _page(history[:20])})
        stub._rollback_gaps_cache = _add_rollback_gap([], base, base + 5)
        _poll(stub)
        assert stub.conversations_panel.removed == {"m25"}

    def test_the_period_survives_a_restart(self):
        class _Db:
            def __init__(self):
                self.data = {}

            def get_metadata_json(self, key, default=None):
                return self.data.get(key, default)

            def set_metadata_json(self, key, value):
                self.data[key] = value

        db = _Db()
        first = _ReconcileStub([], (set(), None))
        first.db = db
        first._record_rollback_gap = types.MethodType(MainWindow._record_rollback_gap, first)
        first._record_rollback_gap(1_000_000, 1_050_000)

        later = _ReconcileStub([], (set(), None))
        later.db = db
        assert later._rollback_gaps() == db.data[MainWindow._ROLLBACK_GAPS_METADATA_KEY]
        assert later._rollback_gaps()[0][0] <= 1_000_000

    def test_a_restore_before_the_database_exists_is_kept_until_it_does(self):
        stub = _ReconcileStub([], (set(), None))
        stub._record_rollback_gap = types.MethodType(MainWindow._record_rollback_gap, stub)
        stub._record_rollback_gap(1_000_000, 1_050_000)
        assert stub._rollback_gaps()

        stored = {}
        stub.db = types.SimpleNamespace(
            get_metadata_json=lambda key, default=None: stored.get(key, default),
            set_metadata_json=lambda key, value: stored.__setitem__(key, value))
        stub._rollback_gaps()
        assert stored[MainWindow._ROLLBACK_GAPS_METADATA_KEY]


class TestRollbackGapHelpers:
    def test_margins_widen_the_period(self):
        (start, end), = _add_rollback_gap([], 10_000, 20_000)
        assert start < 10_000 and end > 20_000

    def test_unknown_snapshot_age_covers_everything_before_the_restore(self):
        (start, end), = _add_rollback_gap([], None, 20_000)
        assert start == 0 and end > 20_000

    def test_overlapping_periods_merge_and_garbage_is_dropped(self):
        gaps = _normalize_rollback_gaps([[100, 200], "x", [150, 300], [5, 1], [400, 500]])
        assert gaps == [[100, 300], [400, 500]]

    def test_the_list_is_bounded_to_the_newest(self):
        from core.remote_reconcile import MAX_ROLLBACK_GAPS
        gaps = _normalize_rollback_gaps([[i * 10, i * 10 + 1] for i in range(50)])
        assert len(gaps) == MAX_ROLLBACK_GAPS and gaps[-1] == [490, 491]


class TestARestoreFromBeforeThisBuild:
    """A profile restored on an earlier build left its hole with nothing
    recording it, and the walk back into older history ships in the same
    release as the recording — so the first time that chat is opened, the hole
    would be confirmed away (reproduced in review). The broken profile the
    restore moved aside is the evidence left of it."""

    class _Db:
        def __init__(self, stored=None):
            self.data = {} if stored is None else {MainWindow._ROLLBACK_GAPS_METADATA_KEY: stored}

        def get_metadata_json(self, key, default=None):
            return self.data.get(key, default)

        def set_metadata_json(self, key, value):
            self.data[key] = value

    def _stub(self, tmp_path, db):
        from core import profile_recovery
        stub = _ReconcileStub([], (set(), None))
        stub.db = db
        stub.token = "sess:tok"
        stub.global_dir = str(tmp_path)
        broken = profile_recovery.profile_dir(str(tmp_path), "sess") + ".broken"
        return stub, broken

    def test_the_broken_profile_it_left_records_everything_before_it(self, tmp_path):
        import os
        db = self._Db()
        stub, broken = self._stub(tmp_path, db)
        os.makedirs(broken)
        os.utime(broken, (2_000_000, 2_000_000))

        gaps = stub._rollback_gaps()

        assert gaps and gaps[0][0] == 0 and gaps[0][1] >= 2_000_000
        assert db.data[MainWindow._ROLLBACK_GAPS_METADATA_KEY] == gaps

    def test_no_broken_profile_records_nothing_but_is_checked_once(self, tmp_path):
        db = self._Db()
        stub, _broken = self._stub(tmp_path, db)
        assert stub._rollback_gaps() == []
        assert db.data[MainWindow._ROLLBACK_GAPS_METADATA_KEY] == []

    def test_an_install_that_already_records_is_not_second_guessed(self, tmp_path):
        """Once written, the stored list is the record — a `.broken` from a
        restore this build already recorded must not widen it to all history."""
        import os
        db = self._Db(stored=[[1_000, 2_000]])
        stub, broken = self._stub(tmp_path, db)
        os.makedirs(broken)
        assert stub._rollback_gaps() == [[1_000, 2_000]]

    def test_the_legacy_period_keeps_the_reproduced_hole(self, tmp_path):
        import os
        history = _old_history(30)
        stub = _ReconcileStub(
            history, (_ids(history[20:]), history[20]["messageTimestamp"]),
            before={"anchor": _page(history[:10])})
        stub.db = self._Db()
        stub.token = "sess:tok"
        stub.global_dir = str(tmp_path)
        from core import profile_recovery
        broken = profile_recovery.profile_dir(str(tmp_path), "sess") + ".broken"
        os.makedirs(broken)
        restored = history[25]["messageTimestamp"]
        os.utime(broken, (restored, restored))

        _poll(stub, STRIKES + 1)

        assert stub.conversations_panel.removed is None
