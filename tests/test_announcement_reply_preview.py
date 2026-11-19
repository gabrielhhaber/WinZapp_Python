"""A reply to a community announcement is the group's chat-list preview when
it is the newest thing there: "Fulano respondeu a um aviso 14:32", as on the
phone and in WhatsApp Web. WhatsApp keeps it as the chat's chatlistPreview,
which list-chats already carries and the live bridge refreshes."""

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import wx

from core.community_comments import latest_reply_preview, parse_reply_preview_event
from core.locale_format import preview_time_text
from main import MainWindow
from main_window.community_comments import CommunityCommentsMixin
from tests.locales import load_strings

JID = "123-456@g.us"
STRINGS = load_strings("pt-BR")
NOON = int(datetime.combine(date.today(), datetime.min.time()).timestamp()) + 12 * 3600


def _preview(sender="456@lid", seconds=NOON, from_me=False):
    return {"type": "comment", "msgKey": f"{'true' if from_me else 'false'}_{JID}_C_{sender}",
            "parentMsgKey": f"false_{JID}_PARENT_789@lid", "sender": sender,
            "timestamp": seconds * 1000}


def _message(seconds):
    return {"key": {"id": "M", "fromMe": False, "participant": "789@lid"},
            "messageType": "conversation", "message": {"conversation": "oi"},
            "messageTimestamp": seconds}


class _MainWindowStub:
    _counts_as_last_message = classmethod(MainWindow._counts_as_last_message.__func__)
    _last_msg_preview = MainWindow._last_msg_preview
    _PREVIEW_MESSAGE_TYPES = MainWindow._PREVIEW_MESSAGE_TYPES
    _announcement_reply_preview_text = CommunityCommentsMixin._announcement_reply_preview_text

    def __init__(self, names=None):
        self.i18n = SimpleNamespace(t=lambda key: STRINGS.get(key, key))
        self.settings = {}
        self._names = names or {}

    def _normalize_jid(self, jid):
        return jid

    def _is_self_jid(self, jid):
        return jid == "999@lid"

    def _resolve_contact_name(self, chat):
        return self._names.get(chat["remoteJid"], "")

    def _preview_sender_from_jid(self, jid):
        return ""


def _chat(preview, *messages):
    return {"remoteJid": JID, "chatlistPreview": preview,
            "messages": {"messages": {"records": list(messages)}}}


def test_reply_newer_than_the_last_message_is_the_preview():
    stub = _MainWindowStub({"456@lid": "Fulano"})
    text = stub._last_msg_preview(_chat(_preview(), _message(NOON - 60)))
    assert text.startswith("Fulano respondeu a um aviso ")
    assert text.endswith(preview_time_text(NOON, stub.i18n))


def test_own_reply_and_an_unknown_sender_never_show_an_identifier():
    stub = _MainWindowStub()
    assert stub._last_msg_preview(_chat(_preview(from_me=True))).startswith(
        "você respondeu a um aviso")
    assert stub._last_msg_preview(_chat(_preview(sender="999@lid"))).startswith(
        "você respondeu a um aviso")
    text = stub._last_msg_preview(_chat(_preview()))
    assert text.startswith("Participante sem nome respondeu a um aviso")
    assert "@" not in text


def test_a_newer_message_or_reaction_wins_over_the_reply():
    stub = _MainWindowStub({"456@lid": "Fulano"})
    assert "respondeu a um aviso" not in stub._last_msg_preview(
        _chat(_preview(), _message(NOON + 60)))
    chat = _chat(_preview())
    chat["_last_reaction"] = {"emoji": "👍", "target_id": "", "from_me": True,
                              "participant": "", "push_name": "", "timestamp": NOON + 60}
    assert "respondeu a um aviso" not in stub._last_msg_preview(chat)


def test_a_reaction_preview_from_whatsapp_is_not_read_as_a_reply():
    assert latest_reply_preview({"chatlistPreview": {"type": "reaction", "timestamp": NOON}}) is None
    assert latest_reply_preview({"chatlistPreview": None}) is None
    assert latest_reply_preview(None) is None
    assert latest_reply_preview({"chatlistPreview": {**_preview(), "timestamp": True}}) is None
    assert latest_reply_preview({"chatlistPreview": _preview()}) == {
        "sender": "456@lid", "from_me": False, "timestamp": NOON}


def test_live_preview_event_is_parsed_and_lifts_the_chat():
    event = parse_reply_preview_event({"kind": "preview", "chatId": JID, "preview": _preview()})
    assert event["chat"] == JID and event["preview"]["timestamp"] == NOON
    assert parse_reply_preview_event({"kind": "preview", "chatId": JID,
                                      "preview": {"type": "reaction"}}) == {"chat": JID, "preview": None}
    assert parse_reply_preview_event({"kind": "preview", "chatId": "1@lid", "preview": {}}) is None
    assert parse_reply_preview_event({"id": "x"}) is None

    chat = {"t": NOON - 600}
    mw = SimpleNamespace(chats={JID: chat}, _schedule_set_chats=Mock())
    CommunityCommentsMixin.on_announcement_reply_preview(mw, event)
    assert chat["t"] == NOON and latest_reply_preview(chat)["timestamp"] == NOON
    mw._schedule_set_chats.assert_called_once()
    CommunityCommentsMixin.on_announcement_reply_preview(mw, {"chat": JID, "preview": None})
    assert latest_reply_preview(chat) is None and chat["t"] == NOON


def test_a_preview_that_did_not_change_redraws_nothing():
    """Every reaction in a group is an event, and a reply's preview arrives
    again: the chat list is only rebuilt when something changed."""
    chat = {"t": NOON - 600}
    mw = SimpleNamespace(chats={JID: chat}, _schedule_set_chats=Mock())
    CommunityCommentsMixin.on_announcement_reply_preview(mw, {"chat": JID, "preview": None})
    mw._schedule_set_chats.assert_not_called()
    event = parse_reply_preview_event({"kind": "preview", "chatId": JID, "preview": _preview()})
    CommunityCommentsMixin.on_announcement_reply_preview(mw, event)
    CommunityCommentsMixin.on_announcement_reply_preview(mw, event)
    assert mw._schedule_set_chats.call_count == 1
    CommunityCommentsMixin.on_announcement_reply_preview(mw, {"chat": JID, "preview": None})
    CommunityCommentsMixin.on_announcement_reply_preview(mw, {"chat": JID, "preview": None})
    assert mw._schedule_set_chats.call_count == 2


def test_preview_event_reaches_the_main_window_on_the_ui_thread(monkeypatch):
    from core.websocket_client import WebSocketClient
    calls = []
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *args: calls.append((fn, args)))
    mw = SimpleNamespace(on_announcement_reply_preview=Mock(), on_announcement_reply_count=Mock())
    client = SimpleNamespace(main_window=mw, _belongs_to_this_session=lambda data: True)
    WebSocketClient.on_announcement_replies(
        client, {"session": "s", "kind": "preview", "chatId": JID, "preview": _preview()})
    assert [fn for fn, _ in calls] == [mw.on_announcement_reply_preview]


def test_preview_time_is_the_hour_today_and_the_date_before():
    i18n = SimpleNamespace(t=lambda key: {"time_fmt": "%H:%M", "datetime_fmt": "%d/%m/%Y %H:%M"}[key])
    assert preview_time_text(0, i18n) == ""
    today = datetime.fromtimestamp(NOON).date()
    assert ":" in preview_time_text(NOON, i18n, today=today)
    assert preview_time_text(NOON, i18n, today=date(1999, 1, 1)) != preview_time_text(
        NOON, i18n, today=today)


def test_a_channel_invitation_is_previewed_as_the_conversation_shows_it():
    """It became the preview (it counts as a message) but fell through to
    "unsupported message"."""
    from core import channel_invites as ci
    invite = {"key": {"id": "I", "fromMe": False}, "messageType": ci.FOLLOWER,
              "message": {ci.FOLLOWER: {"newsletterJid": "1@newsletter",
                                        "newsletterName": "Canal", "caption": "Siga!"}},
              "messageTimestamp": NOON}
    stub = _MainWindowStub()
    text = stub._last_msg_preview({"remoteJid": "5511@s.whatsapp.net",
                                   "messages": {"messages": {"records": [invite]}}})
    assert ci.row_text(stub.i18n, invite) in text
    assert STRINGS["notif_unsupported"] not in text
