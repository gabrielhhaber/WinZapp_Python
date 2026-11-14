"""A chat cleared on another device is cleared in WinZapp too
(client/core/remote_chat_clear.py, the `chat-cleared` socket event).

Users reported that clearing a chat on the phone left it full in WinZapp."""
from types import SimpleNamespace

from core import remote_chat_clear as rcc

JID = "5511999990000@s.whatsapp.net"


def msg(mid, t, starred=False):
    return {"key": {"id": mid}, "messageTimestamp": t, "starred": starred}


def test_the_payload_is_checked():
    assert rcc.chat_cleared_payload({"chatId": "5511@c.us", "until": 10}) == ("5511@c.us", 10, True)
    assert rcc.chat_cleared_payload(
        {"chatId": "5511@c.us", "until": 10, "keepStarred": False})[2] is False
    for bad in ({"chatId": "5511@c.us"}, {"chatId": "x", "until": 10},
                {"chatId": "1@newsletter", "until": 10}, {"chatId": "status@broadcast", "until": 10},
                {"chatId": "5511@c.us", "until": True}, {"chatId": "5511@c.us", "until": 0}, None):
        assert rcc.chat_cleared_payload(bad) is None


def test_what_came_after_the_clear_and_the_starred_ones_stay():
    records = [msg("a", 100), msg("b", 100, starred=True), msg("c", 102), msg("d", 300), "x"]
    assert [m["key"]["id"] for m in rcc.kept_after_clear(records, 100, True)] == ["b", "d"]
    assert [m["key"]["id"] for m in rcc.kept_after_clear(records, 100, False)] == ["d"]


class _DB:
    def __init__(self):
        self.calls = []

    def upsert_chat(self, jid, chat):
        self.calls.append(("upsert", jid))

    def delete_chat_messages_except(self, jid, keep):
        self.calls.append(("delete", jid, list(keep)))


def _window(chat_jid=JID, **maps):
    chat = {"remoteJid": chat_jid, "t": 300, "unreadCount": 4,
            "messages": {"messages": {"records": [msg("a", 100), msg("b", 100, True)]}}}
    calls = []
    mw = SimpleNamespace(
        chats={chat_jid: chat}, settings={}, db=_DB(), calls=calls,
        _normalize_jid=lambda j: j.replace("@c.us", "@s.whatsapp.net"),
        save_settings=lambda: calls.append("save_settings"),
        _recompute_chat_last_message=lambda j: chat.__setitem__("t", 0),
        _schedule_save=lambda dirty_jid=None: calls.append(("save", dirty_jid)),
        _schedule_set_chats=lambda: calls.append("set_chats"),
        _refresh_open_conversation_after_sync=lambda j, c: calls.append(("repaint", j)),
        **maps)
    return mw, chat


def test_the_conversation_is_cleared_and_stays_where_it_was():
    mw, chat = _window()
    assert rcc.apply_remote_clear(mw, "5511999990000@c.us", 200, True)
    assert [m["key"]["id"] for m in chat["messages"]["messages"]["records"]] == ["b"]
    assert chat["unreadCount"] == 0 and chat["t"] == 300
    assert mw.settings["cleared_chats"] == {JID: 200}
    assert "cleared_starred_chats" not in mw.settings
    assert ("delete", JID, ["b"]) in mw.db.calls
    assert ("repaint", JID) in mw.calls and "set_chats" in mw.calls


def test_a_clear_of_the_starred_ones_too_records_their_cutoff():
    mw, chat = _window()
    rcc.apply_remote_clear(mw, JID, 200, False)
    assert chat["messages"]["messages"]["records"] == []
    assert mw.settings["cleared_starred_chats"] == {JID: 200}


def test_a_clear_named_by_lid_finds_the_chat_by_number():
    mw, chat = _window(_lid_to_phone={"777@lid": JID})
    assert rcc.apply_remote_clear(mw, "777@lid", 200, True)
    assert mw.settings["cleared_chats"] == {JID: 200}


def test_an_unknown_chat_or_an_older_cutoff_changes_nothing_it_should_not():
    mw, _chat = _window()
    assert not rcc.apply_remote_clear(mw, "1@g.us", 200, True)
    mw.settings["cleared_chats"] = {JID: 500}
    rcc.apply_remote_clear(mw, JID, 200, True)
    assert mw.settings["cleared_chats"] == {JID: 500}


def test_the_socket_event_reaches_the_window(monkeypatch):
    from core import websocket_client as wc
    scheduled = []
    monkeypatch.setattr(wc.wx, "CallAfter", lambda fn, *a: scheduled.append((fn, a)))
    client = SimpleNamespace(main_window="mw", _belongs_to_this_session=lambda d: True)
    wc.WebSocketClient.on_chat_cleared(client, {"chatId": "5511@c.us", "until": 10})
    assert scheduled == [(rcc.apply_remote_clear, ("mw", "5511@c.us", 10, True))]
    other = SimpleNamespace(main_window="mw", _belongs_to_this_session=lambda d: False)
    wc.WebSocketClient.on_chat_cleared(other, {"chatId": "5511@c.us", "until": 10})
    assert len(scheduled) == 1
