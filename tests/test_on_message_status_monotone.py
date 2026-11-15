"""Delivery-status acks for an own message are monotonic.

WhatsApp events (and the bridge's synthetic onack ack=1, emitted 0.6 s after
every send to flip the row out of the local "pending" state) can arrive in any
order. The row used to take the newest entry as truth, so a stale "sent"
landing after "delivered" regressed the message to "enviada" and the SQLite
indexed status column with it (revision of 2026-11-15: an audio message whose
DELIVERED had already landed was flipped back 280 ms later by the synthetic
onack 1). A positive ack that moves the message backwards is now ignored;
a failure verdict is always allowed through.
"""

import logging

from main import MainWindow


class _FakePanel:
    def __init__(self):
        self.refreshed = []

    def refresh_message_status(self, msg_id, status):
        self.refreshed.append((msg_id, status))


class _FakeDB:
    def __init__(self):
        self.written = []

    def insert_message(self, jid, msg):
        self.written.append((jid, dict(msg)))


class _Win:
    on_message_status_update = MainWindow.on_message_status_update

    def __init__(self):
        self.chats = {}
        self.db = _FakeDB()
        self.conversations_panel = _FakePanel()
        self.settings = {"user_interface": {"show_delivery_status_in_chat_list": True}}
        self.rows_refreshed = []
        self.scheduled = 0
        # bound as an instance attribute: no `self` in its signature
        self._is_status_stale = MainWindow._is_status_stale

    def _normalize_jid(self, jid):
        return jid

    def move_chat_row_to_top(self, chat_jid):
        self.rows_refreshed.append(chat_jid)
        return True

    def _schedule_set_chats(self):
        self.scheduled += 1


JID = "chat@g.us"


def _win_with_message(updates=None, status=None):
    win = _Win()
    msg = {"key": {"id": "m1", "remoteJid": JID, "fromMe": True},
           "messageType": "conversation",
           "message": {"conversation": "oi"},
           "MessageUpdate": [{"status": s, "ts": 1} for s in (updates or [])]}
    if status is not None:
        msg["status"] = status
    win.chats[JID] = {"messages": {"messages": {"records": [msg]}}}
    return win


def _ack(status):
    return {"key": {"id": "m1", "remoteJid": JID, "fromMe": True},
            "update": {"status": status}}


def _statuses(win):
    return [u["status"] for u in win.chats[JID]["messages"]["messages"]["records"][0]["MessageUpdate"]]


def test_stale_sent_after_delivered_is_ignored():
    win = _win_with_message(updates=[2, 3])
    win.on_message_status_update(_ack(2))
    assert _statuses(win) == [2, 3]          # the stale ack was not appended
    assert win.db.written == []              # nothing persisted for the stale ack
    assert win.conversations_panel.refreshed == []


def test_stale_sent_after_read_is_ignored():
    win = _win_with_message(updates=[2, 3, 4])
    win.on_message_status_update(_ack(2))
    assert _statuses(win) == [2, 3, 4]
    assert win.db.written == []


def test_stale_ack_uses_the_top_level_status_as_a_reached_stage():
    win = _win_with_message(updates=[], status=4)
    win.on_message_status_update(_ack(2))
    assert _statuses(win) == []
    assert win.db.written == []


def test_failure_after_delivered_is_allowed_through():
    win = _win_with_message(updates=[2, 3])
    win.on_message_status_update(_ack(-1))
    assert _statuses(win) == [2, 3, -1]
    assert win.db.written                   # the failure was persisted
    assert win.conversations_panel.refreshed


def test_sent_then_delivered_still_advances():
    win = _win_with_message(updates=[2])
    win.on_message_status_update(_ack(3))
    assert _statuses(win) == [2, 3]


def test_delivered_then_read_then_played_still_advances():
    win = _win_with_message(updates=[3])
    win.on_message_status_update(_ack(4))
    win.on_message_status_update(_ack(5))
    assert _statuses(win) == [3, 4, 5]


def test_equal_stage_is_recorded_like_before():
    win = _win_with_message(updates=[3])
    win.on_message_status_update(_ack(3))
    assert _statuses(win) == [3, 3]


def test_delivered_string_ack_is_recognised():
    win = _win_with_message(updates=[2])
    win.on_message_status_update(_ack("DELIVERED"))
    assert _statuses(win) == [2, "DELIVERED"]


def test_read_string_after_delivered_is_ignored_when_behind():
    win = _win_with_message(updates=[4])
    win.on_message_status_update(_ack("DELIVERED"))
    assert _statuses(win) == [4]
