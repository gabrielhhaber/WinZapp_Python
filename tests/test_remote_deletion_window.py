"""Tests for bounding the phone-side deletion mirror to what the server answered.

Reported live (2026-09-16) as "the message list shrinks back to the limit on the
60 s poll": with messages_page_size = 200 the open group lost its oldest row on
nearly every round, and the log showed why — every round ended with
"[_mirror_remote_deletions] 1 message(s) ... no longer on the phone — removing
locally". Nothing had been deleted on the phone. The comparison took the last
200 *local records* against the last 200 *remote entries*, and the server's
entries include edit events and placeholders WinZapp never stores as messages,
so they covered less history; the oldest local messages looked missing and
were removed from the list, the records and the database.

See core/remote_deletions.py for the rules.
"""

import time

import pytest
import wx

from core.remote_deletions import (
    comparable_local_ids,
    message_timestamp_seconds,
    oldest_timestamp,
)
from main import MainWindow, is_countable_message

JID = "120363427511142886@g.us"
OLD = int(time.time()) - 3600


def _msg(mid, ts, mtype="conversation"):
    return {
        "key": {"id": mid, "fromMe": False, "remoteJid": JID},
        "message": {"conversation": "x"},
        "messageType": mtype,
        "messageTimestamp": ts,
    }


def _reaction(target, ts):
    return {
        "key": {"id": f"_rxn_{target}", "fromMe": False, "remoteJid": JID},
        "message": {"reactionMessage": {"key": {"id": target}, "text": "👍"}},
        "messageType": "reactionMessage",
        "messageTimestamp": ts,
    }


class TestHelpers:
    def test_milliseconds_are_read_as_seconds(self):
        assert message_timestamp_seconds({"messageTimestamp": 1_789_569_047_000}) == 1_789_569_047

    def test_garbage_timestamps_are_zero(self):
        assert message_timestamp_seconds({"messageTimestamp": "x"}) == 0
        assert message_timestamp_seconds(None) == 0

    def test_oldest_ignores_undated_records(self):
        assert oldest_timestamp([{"messageTimestamp": 0}, _msg("a", 5), _msg("b", 3)]) == 3
        assert oldest_timestamp([{}]) is None

    def test_messages_at_or_before_the_remote_edge_are_not_compared(self):
        records = [_msg("a", OLD), _msg("b", OLD + 1), _msg("c", OLD + 2)]
        ids = comparable_local_ids(records, 200, time.time() - 120, OLD + 1)
        assert ids == {"c"}

    def test_without_a_remote_edge_the_page_slice_still_applies(self):
        records = [_msg("a", OLD), _msg("b", OLD + 1), _msg("c", OLD + 2)]
        assert comparable_local_ids(records, 2, time.time() - 120, None) == {"b", "c"}

    def test_synthetic_ids_pending_and_fresh_messages_are_never_compared(self):
        pending = _msg("p", OLD)
        pending["_local_pending"] = True
        records = [_reaction("a", OLD), pending, _msg("fresh", int(time.time())), _msg("a", OLD)]
        assert comparable_local_ids(records, 200, time.time() - 120, None) == {"a"}

    def test_only_real_content_passes_the_predicate(self):
        records = [_msg("a", OLD), _msg("n", OLD, mtype="groupNotification"), _reaction("a", OLD)]
        ids = comparable_local_ids(records, 200, time.time() - 120, None, is_countable_message)
        assert ids == {"a"}

    def test_a_predicate_that_raises_keeps_the_message_out(self):
        def boom(_r):
            raise RuntimeError
        assert comparable_local_ids([_msg("a", OLD)], 200, time.time() - 120, None, boom) == set()


class _Panel:
    def __init__(self):
        self.conversation = {"remoteJid": JID}
        self.removed = None
        self.selected_messages = set()

    def remove_messages_by_id(self, ids, focus_previous=False):
        self.removed = set(ids)

    def populate_messages(self):
        pass


class _Stub:
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _reconcile_active_conversation_with_remote = MainWindow._reconcile_active_conversation_with_remote
    _rollback_gaps = MainWindow._rollback_gaps
    _legacy_restore_gap = MainWindow._legacy_restore_gap
    _ROLLBACK_GAPS_METADATA_KEY = MainWindow._ROLLBACK_GAPS_METADATA_KEY
    _mirror_remote_deletions = MainWindow._mirror_remote_deletions
    _mirror_remote_clear = MainWindow._mirror_remote_clear
    _deletions_before_remote_window = MainWindow._deletions_before_remote_window
    _REMOTE_CLEAR_CONFIRM_STRIKES = MainWindow._REMOTE_CLEAR_CONFIRM_STRIKES
    _REMOTE_BEFORE_PAGES = MainWindow._REMOTE_BEFORE_PAGES

    def __init__(self, records, remote, page_size=200):
        self.chats = {JID: {"remoteJid": JID, "messages": {"messages": {"records": records}}}}
        self.conversations_panel = _Panel()
        self.messages_set_completed = True
        self.settings = {"user_interface": {"messages_page_size": page_size}}
        self._remote = remote
        self.fetches = 0

    def _fetch_remote_message_window(self, remote_jid):
        self.fetches += 1
        ids, oldest = self._remote
        return ids, oldest, ("anchor" if ids else "")

    # The look further back into older history fails here: whatever it cannot
    # account for must be kept, which is what these tests pin.
    def _fetch_remote_messages_before(self, remote_jid, anchor_id):
        return None


@pytest.fixture(autouse=True)
def _synchronous_call_after(monkeypatch):
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))


class TestTheOpenWindowIsNotEatenByThePoll:
    def test_entries_the_server_counts_do_not_push_messages_out_of_its_window(self):
        """The reported shape, scaled down: page size 4. The server's newest 4
        entries are m5, m6 and two edit events WinZapp dropped, so the oldest
        message it answered for is m5; the last 4 local records reach back to
        m3 — so m3 and m4 used to look deleted."""
        records = [_msg(f"m{i}", OLD + i) for i in range(1, 7)]
        stub = _Stub(records, ({"m5", "m6"}, OLD + 5), page_size=4)
        stub._reconcile_active_conversation_with_remote()
        assert stub.conversations_panel.removed is None, "a message outside the server's window was deleted"

    def test_the_old_comparison_would_have_deleted_them(self):
        """Pins that the fixture above can reproduce the defect: with no edge —
        the old comparison's shape — m3 and m4 are compared and absent from the
        server's answer. It exercises the new helper unbounded, not the old
        code itself."""
        records = [_msg(f"m{i}", OLD + i) for i in range(1, 7)]
        ids = comparable_local_ids(records, 4, time.time() - 120, None, is_countable_message)
        assert {"m3", "m4"} <= ids - {"m5", "m6"}

    def test_a_message_on_the_remote_edge_timestamp_is_kept(self):
        records = [_msg("a", OLD), _msg("b", OLD), _msg("c", OLD + 1), _msg("d", OLD + 2)]
        stub = _Stub(records, ({"b", "c", "d"}, OLD))
        stub._reconcile_active_conversation_with_remote()
        assert stub.conversations_panel.removed is None

    def test_a_real_deletion_inside_the_window_is_still_mirrored(self):
        records = [_msg("a", OLD), _msg("b", OLD + 1), _msg("c", OLD + 2), _msg("d", OLD + 3)]
        stub = _Stub(records, ({"a", "b", "d"}, OLD))
        stub._reconcile_active_conversation_with_remote()
        assert stub.conversations_panel.removed == {"c"}

    def test_a_reaction_the_server_does_not_list_is_not_a_deletion(self):
        records = [_msg("a", OLD), _msg("b", OLD + 1), _reaction("b", OLD + 2), _msg("c", OLD + 3)]
        stub = _Stub(records, ({"a", "b", "c"}, OLD))
        stub._reconcile_active_conversation_with_remote()
        assert stub.conversations_panel.removed is None

    def test_an_empty_answer_still_counts_toward_a_clear(self):
        """No remote edge exists in an empty answer, and none may be invented:
        that is the shape of a phone-side clear."""
        records = [_msg("a", OLD), _msg("b", OLD + 1), _msg("c", OLD + 2)]
        stub = _Stub(records, (set(), None))
        cleared = []
        stub.clear_chat_messages_local = lambda jid, record_cutoff=True: cleared.append(jid)
        stub._schedule_set_chats = lambda: None
        for _ in range(MainWindow._REMOTE_CLEAR_CONFIRM_STRIKES):
            stub._reconcile_active_conversation_with_remote()
        assert cleared == [JID]

    def test_a_chat_with_no_comparable_content_costs_no_request(self):
        records = [_reaction("x", OLD), _msg("n", OLD, mtype="groupNotification")]
        stub = _Stub(records, (set(), None))
        stub._reconcile_active_conversation_with_remote()
        assert stub.fetches == 0

    def test_a_clear_followed_by_a_new_message_is_not_mirrored(self):
        """Accepted deliberately (see core/remote_deletions.py): once a new
        message lands, the answer is bounded by it and no older local message
        is compared. A missed clear is cosmetic; lifting the bound is not."""
        records = [_msg("a", OLD), _msg("b", OLD + 1), _msg("c", OLD + 2)]
        stub = _Stub(records, ({"new"}, OLD + 100))
        stub._reconcile_active_conversation_with_remote()
        assert stub.conversations_panel.removed is None

    def test_messages_without_a_bound_are_never_compared(self):
        records = [_msg("a", OLD), _msg("b", OLD + 1), _msg("c", OLD + 2)]
        stub = _Stub(records, ({"c"}, None))
        stub._reconcile_active_conversation_with_remote()
        assert stub.conversations_panel.removed is None


class _Response:
    status_code = 200

    def __init__(self, entries):
        self._entries = entries

    def json(self):
        return {"response": self._entries}


class _Ws:
    def _normalize_wpp_message(self, wm):
        if wm.get("boom"):
            raise ValueError("unmappable")
        return {"key": {"id": wm.get("id", "")}}


class _FetchStub:
    _fetch_remote_message_window = MainWindow._fetch_remote_message_window
    _get_remote_messages = MainWindow._get_remote_messages

    def __init__(self):
        self.ws = _Ws()
        self._phone_to_lid = {}
        self.settings = {}
        self.wpp_server, self.wpp_port, self.token = "http://127.0.0.1", 6300, "t"


class TestFetchRemoteMessageWindow:
    def _fetch(self, monkeypatch, entries):
        monkeypatch.setattr("main.api_get", lambda *a, **kw: _Response(entries))
        return _FetchStub()._fetch_remote_message_window(JID)

    def test_an_unmappable_entry_still_bounds_the_window(self, monkeypatch):
        entries = [{"id": "x", "boom": True, "t": OLD}, {"id": "m", "t": OLD + 5}]
        assert self._fetch(monkeypatch, entries) == ({"m"}, OLD, "")

    def test_an_empty_answer_is_data(self, monkeypatch):
        assert self._fetch(monkeypatch, []) == (set(), None, "")

    def test_entries_that_yield_no_ids_are_ambiguous_not_a_clear(self, monkeypatch):
        """A broken normaliser must not read as "the phone has nothing" — three
        polls of that would wipe the conversation."""
        entries = [{"boom": True, "t": OLD}, {"boom": True, "t": OLD + 1}]
        assert self._fetch(monkeypatch, entries) is None

    def test_ids_without_timestamps_are_ambiguous(self, monkeypatch):
        assert self._fetch(monkeypatch, [{"id": "m"}, {"id": "n"}]) is None
