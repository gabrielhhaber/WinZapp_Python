"""Old messages still arriving after the sync: the list is held, the status
says so, and a burst of recent messages notifies only a few times.

Reported 2026-11-13 (Neonize, a notebook weeks behind on a slow link).
"""

import time
from types import SimpleNamespace

import pytest

from core.notification_burst import NotificationBurstLimiter, notification_allowed
from main_window import backlog_flood
from main_window.backlog_flood import (
    BacklogFlood, defer_list_update, held_status, is_late_message,
    note_incoming_message,
)
from tests.test_archived_chat_sound_tts import _StubMainWindow
from tests.test_historical_self_chat_guard import (
    MY_PHONE, _HistoricalStub, _historical_msg,
)
from tests.test_unread_reread_race import JID as UNREAD_JID
from tests.test_unread_reread_race import _Stub as _UnreadStub
from tests.test_unread_reread_race import _chat as _unread_chat

JID = "5511911112222@s.whatsapp.net"
NOW = 1_800_000_000


# ── BacklogFlood ────────────────────────────────────────────────────────────

def test_a_few_late_messages_are_not_a_flood():
    flood = BacklogFlood(start_count=5, start_window=3.0)
    assert not any(flood.note(t) for t in (0, 0.1, 0.2, 0.3))
    assert not flood.active


def test_late_messages_spread_out_are_not_a_flood():
    flood = BacklogFlood(start_count=5, start_window=3.0)
    assert not any(flood.note(t) for t in (0, 2, 4, 6, 8, 10))
    assert not flood.active


def test_many_late_messages_at_once_start_a_flood_once():
    flood = BacklogFlood(start_count=5, start_window=3.0)
    started = [flood.note(t / 10) for t in range(8)]
    assert started == [False] * 4 + [True] + [False] * 3
    assert flood.active and flood.count == 8


def test_a_flood_ends_only_after_the_quiet_period():
    flood = BacklogFlood(start_count=2, start_window=3.0, quiet_seconds=5.0)
    flood.note(0)
    flood.note(1)
    assert not flood.settle(5.9)
    flood.note(5.9)
    assert not flood.settle(10)
    assert flood.settle(11)
    assert not flood.active
    assert not flood.settle(20)
    # The next flood needs its own evidence.
    assert not flood.note(21)


@pytest.mark.parametrize("ts,late", [
    (NOW - backlog_flood.LATE_SECONDS - 1, True),
    (NOW - backlog_flood.LATE_SECONDS + 1, False),
    (NOW + 30, False),
    ((NOW - 3600) * 1000, True),            # milliseconds
    (NOW * 1000, False),
    (0, False), (None, False), ("x", False),
])
def test_is_late_message(ts, late):
    assert is_late_message({"messageTimestamp": ts}, NOW) is late


# ── window functions ────────────────────────────────────────────────────────

class _Window:
    def __init__(self):
        self.statuses = []
        self.rebuilds = 0
        self.i18n = SimpleNamespace(t=lambda key: f"[{key}]")

    def _set_status(self, status):
        self.statuses.append(held_status(self, status))

    def _schedule_set_chats(self):
        self.rebuilds += 1


def _old():
    return {"messageTimestamp": int(time.time()) - 86400}


@pytest.fixture
def polls(monkeypatch):
    started = []
    monkeypatch.setattr(backlog_flood, "_start_poll", started.append)
    return started


def test_flood_holds_the_list_and_the_status_then_rebuilds_once(polls, monkeypatch):
    window = _Window()
    assert not defer_list_update(window)
    for _ in range(backlog_flood.START_COUNT):
        assert note_incoming_message(window, _old())
    assert polls == [window]
    assert window.statuses == ["[updating_conversations]"]
    assert defer_list_update(window) and defer_list_update(window)
    assert window.rebuilds == 0

    backlog_flood._poll(window)             # still arriving
    assert len(polls) == 2 and window.rebuilds == 0

    monkeypatch.setattr(backlog_flood.time, "monotonic",
                        lambda base=time.monotonic(): base + backlog_flood.QUIET_SECONDS + 1)
    backlog_flood._poll(window)
    assert window.statuses[-1] == ""
    assert window.rebuilds == 1
    assert not defer_list_update(window)


def test_flood_end_without_a_held_update_rebuilds_nothing(polls, monkeypatch):
    window = _Window()
    for _ in range(backlog_flood.START_COUNT):
        note_incoming_message(window, _old())
    monkeypatch.setattr(backlog_flood.time, "monotonic",
                        lambda base=time.monotonic(): base + 60)
    backlog_flood._poll(window)
    assert window.rebuilds == 0


def test_a_specific_status_is_not_replaced_by_the_flood(polls, monkeypatch):
    window = _Window()
    window._set_status("desconectado")
    for _ in range(backlog_flood.START_COUNT):
        note_incoming_message(window, _old())
    assert window.statuses == ["desconectado"]
    # The sync round ending in the middle of the flood must not read as idle.
    window._set_status("")
    assert window.statuses[-1] == "[updating_conversations]"
    monkeypatch.setattr(backlog_flood.time, "monotonic",
                        lambda base=time.monotonic(): base + 60)
    backlog_flood._poll(window)
    assert window.statuses[-1] == ""


def test_recent_messages_never_start_a_flood(polls):
    window = _Window()
    for _ in range(50):
        assert not note_incoming_message(
            window, {"messageTimestamp": int(time.time())})
    assert polls == [] and not defer_list_update(window)


def test_no_timer_means_no_flood(monkeypatch):
    def boom(_window):
        raise RuntimeError("no wx app")
    monkeypatch.setattr(backlog_flood, "_start_poll", boom)
    window = _Window()
    for _ in range(20):
        note_incoming_message(window, _old())
    assert not defer_list_update(window)
    assert window.statuses == []


# ── on_new_message ──────────────────────────────────────────────────────────

class _CountingWindow(_StubMainWindow):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.moved = []
        self.rebuilds = 0
        self.statuses = []

    def move_chat_row_to_top(self, remote_jid):
        self.moved.append(remote_jid)
        return True

    def _schedule_set_chats(self):
        self.rebuilds += 1

    def _set_status(self, status):
        self.statuses.append(held_status(self, status))


def _message(i, ts, jid=JID):
    return {
        "key": {"remoteJid": jid, "id": f"MSG_{i}", "fromMe": False},
        "messageType": "conversation",
        "message": {"conversation": f"texto {i}"},
        "pushName": "Contato",
        "messageTimestamp": ts,
    }


def test_on_new_message_stops_moving_rows_during_a_flood(polls):
    window = _CountingWindow(open_jid="", window_active=True)
    old = int(time.time()) - 7 * 86400
    for i in range(30):
        window.on_new_message(_message(i, old + i))
    # The messages before the flood was recognised moved their row; none after.
    assert len(window.moved) == backlog_flood.START_COUNT - 1
    assert len(window.chats[JID]["messages"]["messages"]["records"]) == 30
    assert window.chats[JID]["unreadCount"] == 30
    assert window.message_foreground_sound.play_count == 0

    # A message that is really new still moves its chat during the flood.
    window.on_new_message(_message("new", int(time.time())))
    assert len(window.moved) == backlog_flood.START_COUNT


def test_a_late_reaction_does_not_rebuild_the_list_during_a_flood(polls):
    window = _CountingWindow(open_jid="", window_active=True)
    old = int(time.time()) - 7 * 86400
    for i in range(10):
        window.on_new_message(_message(i, old + i))
    assert window.rebuilds == 0
    window.on_new_message({
        "key": {"remoteJid": JID, "id": "R1", "fromMe": False},
        "messageType": "reactionMessage",
        "message": {"reactionMessage": {
            "text": "👍", "key": {"id": "MSG_1", "fromMe": False, "remoteJid": JID}}},
        "messageTimestamp": old + 20,
    })
    assert window.rebuilds == 0
    assert window._backlog_flood.list_dirty


def _start_flood(window):
    for _ in range(backlog_flood.START_COUNT):
        note_incoming_message(window, _old())
    assert backlog_flood.backlog_flood_active(window)


def test_unread_update_defers_the_list_during_a_flood(polls):
    stub = _UnreadStub(_unread_chat(t=2000))
    stub.on_chat_unread_update(UNREAD_JID, 4)
    assert stub.set_chats_calls == 1

    stub._set_status = lambda status: None
    _start_flood(stub)
    stub.on_chat_unread_update(UNREAD_JID, 6)
    assert stub.chats[UNREAD_JID]["unreadCount"] == 6      # data still updated
    assert stub.set_chats_calls == 1
    assert stub._backlog_flood.list_dirty


def test_history_events_feed_the_detector_and_hold_the_list(polls):
    rebuilds = []
    stub = _HistoricalStub(my_jid=MY_PHONE)
    stub._schedule_set_chats = lambda: rebuilds.append(1)
    stub._set_status = lambda status: None
    stub.i18n = SimpleNamespace(t=lambda key: f"[{key}]")
    group = "120363409931936700@g.us"
    for i in range(12):
        stub.on_historical_message(_historical_msg(
            group, participant="5521888888888@s.whatsapp.net", msg_id=f"H{i}"))
    # Rebuilt for the messages before the flood was recognised, never after.
    assert len(rebuilds) == backlog_flood.START_COUNT - 1
    assert len(stub.chats[group]["messages"]["messages"]["records"]) == 12


def test_a_long_flood_applies_the_held_list_once_a_minute():
    flood = BacklogFlood(start_count=2, start_window=3.0, quiet_seconds=5.0)
    flood.note(0)
    flood.note(1)
    assert not flood.flush_due(100, max_hold=60)        # nothing held
    flood.list_dirty = True
    assert not flood.flush_due(30, max_hold=60)
    assert flood.flush_due(61, max_hold=60)
    assert not flood.list_dirty
    flood.list_dirty = True
    assert not flood.flush_due(100, max_hold=60)
    assert flood.flush_due(121, max_hold=60)


def test_poll_applies_the_held_list_during_a_long_flood(polls, monkeypatch):
    window = _Window()
    _start_flood(window)
    defer_list_update(window)
    now = time.monotonic()
    flood = window._backlog_flood
    monkeypatch.setattr(backlog_flood.time, "monotonic",
                        lambda: now + backlog_flood.MAX_HOLD_SECONDS + 1)
    flood.note(now + backlog_flood.MAX_HOLD_SECONDS)    # still arriving
    backlog_flood._poll(window)
    assert window.rebuilds == 1
    assert backlog_flood.backlog_flood_active(window)
    assert defer_list_update(window)


# ── notification burst ──────────────────────────────────────────────────────

def test_limiter_lets_three_through_and_stays_quiet_for_the_burst():
    now = [0.0]
    limiter = NotificationBurstLimiter(limit=3, window=1.0, clock=lambda: now[0])
    answers = []
    for i in range(100):                    # 20 per second for 5 seconds
        now[0] = i * 0.05
        answers.append(limiter.allow())
    assert answers == [True] * 3 + [False] * 97
    now[0] += 1.0                           # the burst is over
    assert limiter.allow()


def test_limiter_never_touches_an_ordinary_conversation():
    now = [0.0]
    limiter = NotificationBurstLimiter(limit=3, window=1.0, clock=lambda: now[0])
    for i in range(50):                     # two messages a second
        now[0] = i * 0.5
        assert limiter.allow()


def test_notification_allowed_keeps_one_limiter_per_window():
    window = SimpleNamespace()
    assert [notification_allowed(window) for _ in range(5)] == [True] * 3 + [False] * 2


@pytest.mark.parametrize("active", [True, False])
def test_a_burst_of_recent_messages_notifies_three_times(active):
    window = _CountingWindow(open_jid="5521900000000@s.whatsapp.net",
                             window_active=active)
    now = int(time.time())
    for i in range(40):
        window.on_new_message(_message(i, now, jid=f"55119{i:08d}@s.whatsapp.net"))
    if active:
        assert window.message_foreground_sound.play_count == 3
        assert len(window.spoken) == 3
    else:
        assert len(window.notification_manager.sent) == 3
    assert len(window.chats) == 40          # nothing dropped, only quiet


def test_a_burst_in_the_open_conversation_is_still_marked_read(monkeypatch):
    started = []
    monkeypatch.setattr(
        "main_window.message_events.threading.Thread",
        lambda **kw: SimpleNamespace(start=lambda: started.append(kw["args"])))
    window = _CountingWindow(open_jid=JID, window_active=True)
    window._last_activation_time = 0
    now = int(time.time())
    for i in range(10):
        window.on_new_message(_message(i, now))
    assert window.message_current_sound.play_count == 3
    assert len(started) == 10


def test_a_muted_chat_does_not_use_up_the_limit():
    muted = "5511977776666@s.whatsapp.net"
    window = _CountingWindow(open_jid="", muted_jids=[muted], window_active=True)
    now = int(time.time())
    for i in range(20):
        window.on_new_message(_message(i, now, jid=muted))
    window.on_new_message(_message("x", now))
    assert window.message_foreground_sound.play_count == 1


# ── WhatsApp's own announcement (Neonize: offline-sync) ─────────────────────

def test_an_announced_delivery_starts_the_flood_before_any_message(polls):
    window = _Window()
    backlog_flood.note_offline_sync(window, "started", 1500)
    assert polls == [window]
    assert window.statuses == ["[updating_conversations]"]
    assert defer_list_update(window)


@pytest.mark.parametrize("state,messages", [
    ("started", 0), ("started", backlog_flood.START_COUNT - 1),
    ("completed", 0), ("other", 900),
])
def test_a_small_or_unknown_announcement_starts_nothing(polls, state, messages):
    window = _Window()
    backlog_flood.note_offline_sync(window, state, messages)
    assert polls == [] and window.statuses == []
    assert not defer_list_update(window)


def test_an_announced_delivery_survives_pauses_until_whatsapp_says_done():
    flood = BacklogFlood(quiet_seconds=5.0)
    assert flood.announce(0)
    assert not flood.announce(1)            # a second preview starts nothing new
    assert not flood.settle(30)             # a slow link pausing for half a minute
    flood.note(40)
    assert not flood.settle(40 + backlog_flood.ANNOUNCED_QUIET_SECONDS - 1)
    flood.note(90)
    flood.delivered()
    assert not flood.settle(94)             # the usual quiet period is back
    assert flood.settle(95.5)
    assert not flood.active and not flood.announced


def test_an_announcement_never_followed_by_done_still_ends():
    flood = BacklogFlood(quiet_seconds=5.0)
    flood.announce(0)
    assert not flood.settle(backlog_flood.ANNOUNCED_QUIET_SECONDS - 1)
    assert flood.settle(backlog_flood.ANNOUNCED_QUIET_SECONDS + 1)
    assert not flood.announced


def test_done_without_an_announcement_changes_nothing(polls):
    window = _Window()
    _start_flood(window)
    backlog_flood.note_offline_sync(window, "completed")
    assert backlog_flood.backlog_flood_active(window)


def test_the_socket_event_is_checked_before_it_reaches_the_window(monkeypatch):
    from core.websocket_client import WebSocketClient
    calls = []
    monkeypatch.setattr("core.websocket_client.wx.CallAfter",
                        lambda fn, *a: calls.append((fn.__name__, a[1:])))
    client = SimpleNamespace(main_window=object(), instance_name="acc",
                             _belongs_to_this_session=lambda d: d.get("session") == "acc")
    handle = WebSocketClient.on_offline_sync
    handle(client, {"session": "acc", "state": "started", "messages": 1500, "total": 1800})
    handle(client, {"session": "acc", "state": "completed", "count": 1800})
    handle(client, {"session": "other", "state": "started", "messages": 9})
    handle(client, {"session": "acc", "state": "weird", "messages": 9})
    handle(client, {"session": "acc", "state": "started", "messages": "many"})
    handle(client, "junk")
    assert calls == [
        ("note_offline_sync", ("started", 1500)),
        ("note_offline_sync", ("completed", 0)),
        ("note_offline_sync", ("started", 0)),
    ]
