"""Tests for reconciling the unread count of a chat the user has OPEN.

The 60s list-chats resync (get_remote_chats) used to force the open
conversation's count to 0, under a comment claiming it was the "same guard as
on_chat_unread_update()'s live-event path". It was not. The live path consults
_new_since_read and keeps a real backlog; the resync ignored it.

That matters because "open" is not "read". on_new_message() increments both
unreadCount and _new_since_read whenever a message lands in the open
conversation while the window is hidden or minimized — and Alt+F4 only hides to
tray, so that state is ordinary, not exotic.

Concrete failure the (b) test below reproduces: leave chat X open, close with
Alt+F4, five messages arrive (unreadCount 5, _new_since_read 5). Within 60s the
resync sees the panel still on X and writes 0. Badge, window-title counter and
toast count are wiped, and the next arrival announces "1 mensagem não lida" for
a conversation holding six. Worse, it left unreadCount=0 with _new_since_read=5
— the two disagreeing, which then poisons the live path's own min() later.

Both surfaces now share reconcile_open_chat_unread(), so the parity the comment
asserts is checkable rather than claimed.
"""

import json
import types

import pytest

import main
from main import MainWindow, reconcile_open_chat_unread

from tests.test_get_remote_chats_persistence import _Stub, _chat, _make, post  # noqa: F401

JID = "5511900000001@s.whatsapp.net"


# ── (a) the decision itself, pure ────────────────────────────────────────────


class TestReconcileOpenChatUnread:
    def test_no_local_backlog_means_read(self):
        """The original intent, preserved: an open chat with nothing counted
        since the last mark-as-read really is read."""
        assert reconcile_open_chat_unread(7, 0) == (0, False)

    def test_a_server_zero_does_not_erase_a_local_backlog(self):
        """The bug. Without a confirmed remote read, a zero carries no
        information — trust what we counted ourselves."""
        assert reconcile_open_chat_unread(0, 5) == (5, False)

    def test_a_confirmed_remote_read_does_erase_it(self):
        """Somebody really read the chat elsewhere: the badge goes, and so
        does the tracking entry behind it."""
        assert reconcile_open_chat_unread(0, 5, remote_read_confirmed=True) == (0, True)

    def test_both_positive_takes_the_lower(self):
        """For an OPEN chat the local read state is authoritative. A higher
        server count is a /send-seen the server has not acknowledged yet, and
        taking it would resurrect already-read messages into the badge and the
        unread separator."""
        assert reconcile_open_chat_unread(21, 1) == (1, False)
        assert reconcile_open_chat_unread(2, 9) == (2, False)

    def test_it_never_returns_a_negative_or_trips_on_none(self):
        assert reconcile_open_chat_unread(None, None) == (0, False)
        assert reconcile_open_chat_unread(-3, 4) == (4, False)


# ── (b) the resync path really uses it ───────────────────────────────────────


class _OpenPanel:
    def __init__(self, jid):
        self.conversation = {"remoteJid": jid}


class TestTheResyncKeepsTheOpenChatsBacklog:
    """Without this, nothing proves the helper is actually wired into the
    merge — the unit tests above would pass against a dead function."""

    def _stub_with_open_chat(self, unread, new_since_read):
        """get_remote_chats() merges into the dict it is GIVEN, not into
        self.chats — passing {} makes every chat look new and skips the merge
        branch entirely, which is how an earlier version of these tests passed
        without the helper ever being called."""
        existing = {JID: {
            "remoteJid": JID, "t": 1700000000, "unreadCount": unread,
            "messages": {"messages": {"records": []}},
        }}
        stub = _make(existing)
        stub.conversations_panel = _OpenPanel(JID)
        stub._new_since_read = {JID: new_since_read}
        # Opening a conversation always runs mark_conversation_as_read
        # (navigate_to_conversation, ui/conversations.py), which is what makes
        # "open" mean "read" here. The tests below that need the opposite —
        # open but deliberately NOT read — drop it again.
        stub._anchor_unread_to_local_read(JID)
        return stub, existing

    def test_a_server_zero_does_not_wipe_the_backlog(self, post):
        post["payload"] = [_chat(JID, unreadCount=0)]
        stub, existing = self._stub_with_open_chat(unread=5, new_since_read=5)

        stub.get_remote_chats(existing, persist_full=False, notify_errors=False)

        assert existing[JID]["unreadCount"] == 5, (
            "the 60s resync wiped a backlog that arrived while the window "
            "was hidden — badge, title counter and toast count all reset"
        )

    def test_an_open_chat_with_nothing_new_is_still_zeroed(self, post):
        """The behaviour the old code got right must survive."""
        post["payload"] = [_chat(JID, unreadCount=3)]
        stub, existing = self._stub_with_open_chat(unread=3, new_since_read=0)

        stub.get_remote_chats(existing, persist_full=False, notify_errors=False)

        assert existing[JID]["unreadCount"] == 0

    def test_the_server_count_is_clamped_to_the_local_backlog(self, post):
        post["payload"] = [_chat(JID, unreadCount=21)]
        stub, existing = self._stub_with_open_chat(unread=1, new_since_read=1)

        stub.get_remote_chats(existing, persist_full=False, notify_errors=False)

        assert existing[JID]["unreadCount"] == 1

    def test_a_closed_chat_is_untouched_by_this_branch(self, post):
        """Guard against the fix leaking into the ordinary path."""
        post["payload"] = [_chat(JID, unreadCount=4)]
        stub, existing = self._stub_with_open_chat(unread=4, new_since_read=4)
        stub.conversations_panel = None

        stub.get_remote_chats(existing, persist_full=False, notify_errors=False)

        assert existing[JID]["unreadCount"] == 4


class TestAnOpenChatWhoseReadWasUndone:
    """"Open" stops meaning "read" the moment the read is undone with the
    conversation still on screen, and two paths do that:

      * Ctrl+Shift+M on the open chat (_on_accel_toggle_read ->
        mark_conversation_as_unread), which pops _new_since_read and drops
        the anchor.
      * _restore_unread_after_send_seen_failure(), putting a backlog back
        after WhatsApp refused all three /send-seen attempts.

    Both leave _new_since_read at 0 with the panel still showing the chat, and
    reconcile_open_chat_unread() answers 0 for exactly that shape — so this
    resync erased what the user had just asked for, up to a whole backlog,
    within 60 seconds and straight to disk.
    """

    def _open_but_unread(self, unread):
        existing = {JID: {
            "remoteJid": JID, "t": 1700000000, "unreadCount": unread,
            "messages": {"messages": {"records": []}},
        }}
        stub = _make(existing)
        stub.conversations_panel = _OpenPanel(JID)
        stub._new_since_read = {JID: 0}
        # No anchor: the read this chat had was undone.
        return stub, existing

    def test_a_chat_marked_unread_on_screen_keeps_its_badge(self, post):
        post["payload"] = [_chat(JID, unreadCount=1)]
        stub, existing = self._open_but_unread(unread=1)

        stub.get_remote_chats(existing, persist_full=False, notify_errors=False)

        assert existing[JID]["unreadCount"] == 1

    def test_a_restored_backlog_is_not_erased_by_the_resync(self, post):
        """The send-seen rollback case, at the scale it was measured at."""
        post["payload"] = [_chat(JID, unreadCount=34876)]
        stub, existing = self._open_but_unread(unread=34876)

        stub.get_remote_chats(existing, persist_full=False, notify_errors=False)

        assert existing[JID]["unreadCount"] == 34876
