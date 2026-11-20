"""Tests for mirroring phone-side changes (clears, message deletions) back
into WinZapp, and the shared message-removal/focus-restore helper.

Context: WPPConnect's list-chats has no per-message data (msgs:null), so the
only way to notice a conversation was cleared or a message deleted on the
phone is to diff a get-messages fetch against what's stored locally — which
is only ever done for the conversation the user has open right now (see the
cost-vs-benefit comment on MainWindow._reconcile_active_conversation_with_remote).

MainWindow is a wx.Frame and ConversationsPanel a wx.Panel; neither can be
instantiated without a running wx.App, so both are exercised as plain
functions bound onto small stubs — same approach as
tests/test_sender_names.py and tests/test_message_bookmarks.py.
"""

import time

import pytest

import wx
from main import MainWindow
from ui.conversations import ConversationsPanel


# ── ConversationsPanel.remove_messages_by_id ────────────────────────────────


class _FakeMessagesList:
    def __init__(self):
        self.deleted_indices = []
        self.focus_calls = []
        self.selected_calls = []
        self.ensure_visible_calls = []
        self._count = 0
        self._focused_idx = -1

    def DeleteItem(self, idx):
        self.deleted_indices.append(idx)
        self._count -= 1

    def GetItemCount(self):
        return self._count

    def GetFocusedItem(self):
        return self._focused_idx

    def Focus(self, idx):
        self.focus_calls.append(idx)

    def Select(self, idx, on=True):
        self.selected_calls.append((idx, on))

    def EnsureVisible(self, idx):
        self.ensure_visible_calls.append(idx)


class _FakeDB:
    def __init__(self):
        self.deleted = []

    def delete_message(self, jid, msg_id):
        self.deleted.append((jid, msg_id))


class _FakeMainWindowForRemoval:
    def __init__(self, db):
        self.db = db
        self.recompute_calls = []

    def _recompute_chat_last_message(self, jid):
        self.recompute_calls.append(jid)

    def _schedule_set_chats(self):
        pass


def _msg(msg_id):
    return {"key": {"id": msg_id, "fromMe": False}, "message": {"conversation": "x"},
            "messageType": "conversation",
            # Old enough to clear _reconcile_active_conversation_with_remote's
            # "recently sent/received" grace window (see that method).
            "messageTimestamp": 1000}


class _RemovalStub:
    """Minimal stand-in for ConversationsPanel, just for remove_messages_by_id."""

    remove_messages_by_id = ConversationsPanel.remove_messages_by_id
    _stop_playback_for_removed_messages = ConversationsPanel._stop_playback_for_removed_messages
    _focused_msg_id = ConversationsPanel._focused_msg_id
    _is_separator = ConversationsPanel._is_separator

    def __init__(self, sorted_messages, conversation=None):
        self._sorted_messages = list(sorted_messages)
        self._all_sorted_messages = list(sorted_messages)
        self._messages_offset = 0
        self._unread_sep_idx = -1
        # A removed id also leaves the selection — see
        # tests/test_selection_mode.py for why that matters.
        self.selected_messages = set()
        # Not under test here (see tests/test_stop_playback_on_delete.py) —
        # just need _stop_playback_for_removed_messages() to find nothing
        # playing and no-op.
        self._current_audio_id = None
        self._audio_stream = None
        self._current_video_msg_id = None
        self.messages_list = _FakeMessagesList()
        self.messages_list._count = len(sorted_messages)
        self.db = _FakeDB()
        self.main_window = _FakeMainWindowForRemoval(self.db)
        self.conversation = conversation


class TestRemoveMessagesById:
    def test_removes_single_message_and_focuses_previous(self):
        panel = _RemovalStub(
            [_msg("A"), _msg("B"), _msg("C")],
            conversation={"remoteJid": "j@g.us", "messages": {"messages": {
                "records": [_msg("A"), _msg("B"), _msg("C")]}}},
        )

        panel.remove_messages_by_id({"B"}, focus_previous=True)

        assert [m["key"]["id"] for m in panel._sorted_messages] == ["A", "C"]
        assert panel.messages_list.deleted_indices == [1]
        assert panel.messages_list.focus_calls == [0]  # index before the removed row
        assert panel.messages_list.selected_calls == [(0, True)]
        assert panel.messages_list.ensure_visible_calls == [0]
        assert panel.db.deleted == [("j@g.us", "B")]
        remaining_ids = [m["key"]["id"] for m in panel.conversation["messages"]["messages"]["records"]]
        assert remaining_ids == ["A", "C"]

    def test_removing_first_row_focuses_row_zero(self):
        panel = _RemovalStub([_msg("A"), _msg("B")])

        panel.remove_messages_by_id({"A"}, focus_previous=True)

        assert panel.messages_list.focus_calls == [0]

    def test_removing_last_remaining_row_does_not_call_focus(self):
        panel = _RemovalStub([_msg("A")])

        panel.remove_messages_by_id({"A"}, focus_previous=True)

        assert panel.messages_list.focus_calls == []  # nothing left to focus

    def test_batch_removal_of_several_messages(self):
        records = [_msg("A"), _msg("B"), _msg("C"), _msg("D")]
        panel = _RemovalStub(
            records,
            conversation={"remoteJid": "j@g.us", "messages": {"messages": {"records": list(records)}}},
        )

        panel.remove_messages_by_id({"B", "D"}, focus_previous=True)

        assert [m["key"]["id"] for m in panel._sorted_messages] == ["A", "C"]
        # Deleted in descending index order so earlier indices stay valid.
        assert panel.messages_list.deleted_indices == [3, 1]
        assert set(panel.db.deleted) == {("j@g.us", "B"), ("j@g.us", "D")}

    def test_focus_previous_false_does_not_touch_list_focus(self):
        panel = _RemovalStub([_msg("A"), _msg("B")])

        panel.remove_messages_by_id({"A"}, focus_previous=False)

        assert panel.messages_list.focus_calls == []
        assert panel.messages_list.selected_calls == []

    def test_unread_separator_index_shifts_with_removal_above_it(self):
        panel = _RemovalStub([_msg("A"), _msg("B"), _msg("C")])
        panel._unread_sep_idx = 2

        panel.remove_messages_by_id({"A"}, focus_previous=False)

        assert panel._unread_sep_idx == 1

    def test_a_removal_elsewhere_does_not_move_focus_off_the_surviving_row(self):
        """The periodic phone-side-deletion poll (_mirror_remote_deletions)
        must not yank the user's reading position to wherever the earliest
        removed row happens to sit when the row they're actually on survives
        the removal untouched."""
        panel = _RemovalStub([_msg("A"), _msg("B"), _msg("C"), _msg("D")])
        panel.messages_list._focused_idx = 2  # focused on "C"

        panel.remove_messages_by_id({"A"}, focus_previous=True)

        assert [m["key"]["id"] for m in panel._sorted_messages] == ["B", "C", "D"]
        # "C" shifted from index 2 to index 1 after "A" was removed above it —
        # focus must follow it there, not land on index 0 (earliest - 1).
        assert panel.messages_list.focus_calls == [1]
        assert panel.messages_list.selected_calls == [(1, True)]

    def test_removal_of_the_focused_row_itself_falls_back_to_just_before_it(self):
        """The user-initiated single-delete path: the deleted message IS the
        one that was focused, so landing just before it is correct."""
        panel = _RemovalStub([_msg("A"), _msg("B"), _msg("C")])
        panel.messages_list._focused_idx = 1  # focused on "B", the one removed

        panel.remove_messages_by_id({"B"}, focus_previous=True)

        assert panel.messages_list.focus_calls == [0]

    def test_focus_follows_the_unread_separator_when_it_survives(self):
        separator = {"_type": "unread_separator", "count": 1}
        panel = _RemovalStub([_msg("A"), _msg("B"), separator, _msg("C")])
        panel._unread_sep_idx = 2
        panel.messages_list._focused_idx = 2  # focused on the separator row

        panel.remove_messages_by_id({"A"}, focus_previous=True)

        # The separator shifted from index 2 to index 1 (one row removed above
        # it); focus must follow it there.
        assert panel._unread_sep_idx == 1
        assert panel.messages_list.focus_calls == [1]

    def test_empty_id_set_is_a_no_op(self):
        panel = _RemovalStub([_msg("A")])

        panel.remove_messages_by_id(set(), focus_previous=True)

        assert panel.messages_list.deleted_indices == []

    def test_unknown_id_is_a_no_op(self):
        panel = _RemovalStub([_msg("A")])

        panel.remove_messages_by_id({"does-not-exist"}, focus_previous=True)

        assert panel.messages_list.deleted_indices == []


# ── MainWindow._reconcile_active_conversation_with_remote ──────────────────


class _FakeConversationsPanel:
    def __init__(self, remote_jid=None):
        self.conversation = {"remoteJid": remote_jid} if remote_jid else None
        self.selected_messages = {"A", "B", "C"}
        self.removed = None
        self.populate_called = False

    def remove_messages_by_id(self, ids, focus_previous=False):
        self.removed = (set(ids), focus_previous)

    def populate_messages(self):
        self.populate_called = True


def _chat_with_records(*ids):
    return {
        "remoteJid": "j@s.whatsapp.net",
        "messages": {"messages": {"records": [_msg(i) for i in ids]}},
    }


class _ReconcileStub:
    """Minimal stand-in for MainWindow, exercising the reconcile/mirror
    methods as plain functions (no HTTP, no wx event loop)."""

    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _deletions_before_remote_window = MainWindow._deletions_before_remote_window
    _REMOTE_BEFORE_PAGES = MainWindow._REMOTE_BEFORE_PAGES
    _reconcile_active_conversation_with_remote = MainWindow._reconcile_active_conversation_with_remote
    _rollback_gaps = MainWindow._rollback_gaps
    _legacy_restore_gap = MainWindow._legacy_restore_gap
    _ROLLBACK_GAPS_METADATA_KEY = MainWindow._ROLLBACK_GAPS_METADATA_KEY
    _mirror_remote_clear = MainWindow._mirror_remote_clear
    _mirror_remote_deletions = MainWindow._mirror_remote_deletions
    _REMOTE_CLEAR_CONFIRM_STRIKES = MainWindow._REMOTE_CLEAR_CONFIRM_STRIKES

    def __init__(self, chats, conversations_panel, remote_ids, messages_set_completed=True):
        self.chats = chats
        self.conversations_panel = conversations_panel
        self.messages_set_completed = messages_set_completed
        self._remote_ids = remote_ids
        self.clear_calls = []
        self._schedule_set_chats_calls = 0
        self.settings = {}

    # Stubbed instead of hitting the network. The oldest remote timestamp
    # defaults to "older than every local record" (the fixtures all sit at
    # 1000), so tests that do not care about the window bound see none.
    _remote_oldest_ts = 1

    def _fetch_remote_message_window(self, remote_jid):
        if self._remote_ids is None:
            return None
        if not self._remote_ids:
            return set(), None, ""
        return set(self._remote_ids), self._remote_oldest_ts, "anchor"

    # The look further back. Fails by default: a failed page must never turn
    # into a deletion (see _deletions_before_remote_window).
    def _fetch_remote_messages_before(self, remote_jid, anchor_id):
        return None

    def clear_chat_messages_local(self, jid, record_cutoff=True):
        self.clear_calls.append((jid, record_cutoff))
        self.chats[jid]["messages"]["messages"]["records"] = []

    def _schedule_set_chats(self):
        self._schedule_set_chats_calls += 1


@pytest.fixture(autouse=True)
def _synchronous_call_after(monkeypatch):
    """Run wx.CallAfter(fn, *args) immediately instead of queuing it onto a
    (nonexistent, in these tests) wx event loop."""
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))


class TestReconcileActiveConversation:
    def test_no_open_conversation_does_nothing(self):
        stub = _ReconcileStub(
            chats={}, conversations_panel=_FakeConversationsPanel(), remote_ids=set()
        )
        stub._reconcile_active_conversation_with_remote()
        assert stub.clear_calls == []

    def test_short_conversation_is_never_diffed(self):
        """Fewer than 2 local messages: too little history to trust a diff."""
        jid = "j@s.whatsapp.net"
        stub = _ReconcileStub(
            chats={jid: _chat_with_records("A")},
            conversations_panel=_FakeConversationsPanel(jid),
            remote_ids=set(),  # would look like "everything deleted" if trusted
        )
        stub._reconcile_active_conversation_with_remote()
        assert stub.clear_calls == []
        assert stub.conversations_panel.removed is None

    def test_fetch_failure_never_triggers_a_mirror(self):
        jid = "j@s.whatsapp.net"
        stub = _ReconcileStub(
            chats={jid: _chat_with_records("A", "B", "C")},
            conversations_panel=_FakeConversationsPanel(jid),
            remote_ids=None,  # simulates a failed/ambiguous fetch
        )
        stub._reconcile_active_conversation_with_remote()
        assert stub.clear_calls == []
        assert stub.conversations_panel.removed is None

    def test_all_messages_gone_mirrors_a_clear_after_confirm_strikes(self):
        """A "clear" is only mirrored once it holds for
        _REMOTE_CLEAR_CONFIRM_STRIKES consecutive polls — see
        test_a_single_empty_read_does_not_immediately_clear below for why."""
        jid = "j@s.whatsapp.net"
        stub = _ReconcileStub(
            chats={jid: _chat_with_records("A", "B", "C")},
            conversations_panel=_FakeConversationsPanel(jid),
            remote_ids=set(),
        )
        for _ in range(MainWindow._REMOTE_CLEAR_CONFIRM_STRIKES):
            stub._reconcile_active_conversation_with_remote()
        assert stub.clear_calls == [(jid, False)]  # record_cutoff=False: mirroring, not a new cutoff
        assert stub.conversations_panel.populate_called is True
        assert stub.conversations_panel.selected_messages == set()
        assert stub._schedule_set_chats_calls == 1

    def test_a_single_empty_read_does_not_immediately_clear(self):
        """Regression: a valid-but-empty get-messages response (as opposed
        to None, which already bails out separately) is indistinguishable
        from a real phone-side clear, but can also come from a transient
        server-side hiccup — reported live as an actively open group
        conversation briefly clearing to "no messages available" mid-read,
        "recovering" only once a new live message forced a repaint. A single
        bad read must never be enough to wipe a whole conversation."""
        jid = "j@s.whatsapp.net"
        stub = _ReconcileStub(
            chats={jid: _chat_with_records("A", "B", "C")},
            conversations_panel=_FakeConversationsPanel(jid),
            remote_ids=set(),
        )
        stub._reconcile_active_conversation_with_remote()
        assert stub.clear_calls == []
        assert stub.conversations_panel.removed is None
        assert stub.conversations_panel.populate_called is False

    def test_a_good_read_in_between_resets_the_clear_strike_count(self):
        jid = "j@s.whatsapp.net"
        stub = _ReconcileStub(
            chats={jid: _chat_with_records("A", "B", "C")},
            conversations_panel=_FakeConversationsPanel(jid),
            remote_ids=set(),
        )
        stub._reconcile_active_conversation_with_remote()  # strike 1
        stub._remote_ids = {"A", "B", "C"}
        stub._reconcile_active_conversation_with_remote()  # nothing missing — resets
        stub._remote_ids = set()
        for _ in range(MainWindow._REMOTE_CLEAR_CONFIRM_STRIKES - 1):
            stub._reconcile_active_conversation_with_remote()
        assert stub.clear_calls == [], "the reset strike should not have reached the threshold yet"

    def test_some_messages_gone_mirrors_individual_deletions(self):
        jid = "j@s.whatsapp.net"
        stub = _ReconcileStub(
            chats={jid: _chat_with_records("A", "B", "C")},
            conversations_panel=_FakeConversationsPanel(jid),
            remote_ids={"A", "C"},  # "B" was deleted on the phone
        )
        stub._reconcile_active_conversation_with_remote()
        assert stub.clear_calls == []
        assert stub.conversations_panel.removed == ({"B"}, True)

    def test_nothing_missing_is_a_no_op(self):
        jid = "j@s.whatsapp.net"
        stub = _ReconcileStub(
            chats={jid: _chat_with_records("A", "B", "C")},
            conversations_panel=_FakeConversationsPanel(jid),
            remote_ids={"A", "B", "C", "D"},  # server even has a newer message
        )
        stub._reconcile_active_conversation_with_remote()
        assert stub.clear_calls == []
        assert stub.conversations_panel.removed is None

    def test_messages_older_than_the_remote_fetch_window_are_never_diffed(self):
        """Regression: _fetch_remote_message_window() only asks WhatsApp Web for
        its last `messages_page_size` messages. Comparing the FULL local
        history against that limited window used to flag (and delete) older
        local messages just because a busy chat had pushed them past the
        server's returned window — not because they were actually deleted."""
        jid = "j@s.whatsapp.net"
        stub = _ReconcileStub(
            chats={jid: _chat_with_records("A", "B", "C")},
            conversations_panel=_FakeConversationsPanel(jid),
            remote_ids={"B", "C"},  # server's last-`limit` window — "A" is just outside it
        )
        stub.settings = {"user_interface": {"messages_page_size": 2}}
        stub._reconcile_active_conversation_with_remote()
        assert stub.clear_calls == []
        assert stub.conversations_panel.removed is None

    def test_recently_sent_message_is_never_diffed(self):
        """Regression: WhatsApp Web's own /get-messages can lag a few seconds
        behind a message actually reaching the server. A message younger than
        the grace window must never be treated as "missing" purely because
        that fetch hasn't caught up yet — reported live as a message that was
        demonstrably delivered (visible to other group members) vanishing
        from WinZapp's own local history right after being sent."""
        jid = "j@s.whatsapp.net"
        chat = _chat_with_records("A", "B")
        chat["messages"]["messages"]["records"].append({
            "key": {"id": "C", "fromMe": True},
            "message": {"conversation": "just sent"},
            "messageType": "conversation",
            "messageTimestamp": int(time.time()),  # just now
        })
        stub = _ReconcileStub(
            chats={jid: chat},
            conversations_panel=_FakeConversationsPanel(jid),
            remote_ids={"A", "B"},  # "C" hasn't shown up in the fetch yet
        )
        stub._reconcile_active_conversation_with_remote()
        assert stub.clear_calls == []
        assert stub.conversations_panel.removed is None

    def test_conversation_switched_away_before_callback_runs_is_ignored(self):
        """The CallAfter for the mirror can land after the user has already
        navigated to a different conversation — must not mutate the wrong one."""
        jid = "j@s.whatsapp.net"
        panel = _FakeConversationsPanel(jid)
        stub = _ReconcileStub(
            chats={jid: _chat_with_records("A", "B", "C")},
            conversations_panel=panel,
            remote_ids=set(),
        )
        # Simulate the user switching conversations between the background
        # fetch and the CallAfter actually running.
        panel.conversation = {"remoteJid": "someone-else@s.whatsapp.net"}

        stub._mirror_remote_clear(jid)

        assert stub.clear_calls == []
