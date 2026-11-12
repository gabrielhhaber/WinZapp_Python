"""WhatsApp events (client/core/events.py and the panel's event_messages.py).

The WhatsApp Web shapes are the ones read over CDP on 2026-11-11: an
``event_creation`` message, answers 1 going / 2 not going / 3 maybe."""
import datetime
from types import SimpleNamespace

import pytest

from core import events as ev
from core.websocket_client import WebSocketClient


class _I18n:
    texts = {
        "event_label": "Evento", "event_canceled": "Cancelado", "event_passed": "Já aconteceu",
        "event_time_range": "{start} até {end}", "event_location": "Local: {location}",
        "event_call_video": "Ligação de vídeo", "event_call_voice": "Ligação de voz",
        "event_responses_summary": "{going} vão, {maybe} talvez, {not_going} não vão",
        "event_mine_going": "Você vai", "event_unnamed": "sem nome",
        "datetime_fmt": "%d/%m/%Y %H:%M", "time_fmt": "%H:%M", "date_fmt": "%d/%m/%Y",
    }

    def t(self, key):
        return self.texts.get(key, key)


@pytest.fixture(autouse=True)
def app_formats(monkeypatch):
    # Windows' regional format would change the expected text
    monkeypatch.setattr(ev, "get_datetime_format", lambda fallback: fallback)
    monkeypatch.setattr(ev, "get_time_format", lambda fallback: fallback)


def _ts(*parts):
    return int(datetime.datetime(*parts).timestamp())


WPP = {"type": "event_creation", "eventName": " Churrasco ", "eventDescription": "No sítio",
       "eventStartTime": _ts(2030, 5, 1, 12, 0), "eventEndTime": _ts(2030, 5, 1, 15, 0),
       "eventLocation": {"name": "Sítio", "degreesLatitude": 0},
       "eventJoinLink": "https://call.whatsapp.com/video/abc", "isEventCanceled": False,
       "eventExtraGuestsAllowed": True}


def test_a_whatsapp_web_event_becomes_the_canonical_message():
    event = ev.event_from_wpp(WPP)
    assert event["name"] == "Churrasco" and event["location"] == "Sítio"
    assert event["startTime"] == WPP["eventStartTime"] and event["extraGuestsAllowed"] is True
    assert ev.event_from_wpp({"eventLocation": "Online"})["location"] == "Online"


def test_the_normalizer_hands_it_over():
    class _Normalizer:
        _normalize_wpp_message = WebSocketClient._normalize_wpp_message
        _clean_jid = WebSocketClient._clean_jid
    msg = dict(WPP, id="true_5511@c.us_3EB0AAAA", fromMe=True, t=1, **{"from": "5511@c.us", "to": "5511@c.us"})
    out = _Normalizer()._normalize_wpp_message(msg)
    assert out["messageType"] == "eventMessage"
    assert out["message"]["eventMessage"]["name"] == "Churrasco"


def test_the_row_says_what_when_where_how_and_who_goes():
    event = ev.event_from_wpp(WPP)
    event["responses"] = [{"response": 1, "isMe": True}, {"response": 1, "extraGuestCount": 2},
                          {"response": 3}, {"response": 2}]
    text = ev.event_text(event, _I18n(), now=_ts(2030, 4, 1))
    assert text == ("Evento: Churrasco. 01/05/2030 12:00 até 15:00. Local: Sítio. "
                    "Ligação de vídeo. No sítio. 4 vão, 1 talvez, 1 não vão. Você vai.")


def test_canceled_and_past_events_say_so_and_cannot_be_answered():
    event = ev.event_from_wpp(dict(WPP, isEventCanceled=True))
    assert "Cancelado" in ev.event_text(event, _I18n(), now=0)
    assert not ev.can_answer(event, True, 0)
    past = ev.event_from_wpp(WPP)
    assert "Já aconteceu" in ev.event_text(past, _I18n(), now=_ts(2031, 1, 1))
    assert not ev.can_answer(past, False, _ts(2031, 1, 1))
    assert ev.can_answer(past, True, _ts(2031, 1, 1))      # the creator still may


def test_answers_are_kept_and_say_when_they_changed():
    event = {}
    data = {"responses": [{"sender": "a@lid", "response": 1}, {"sender": "b", "response": 9}], "mine": 1}
    assert ev.apply_responses(event, data) is True
    assert event["responses"] == [{"sender": "a@lid", "response": 1}]
    assert ev.apply_responses(event, data) is False
    assert ev.my_response(event) == 1


def test_an_edit_replaces_the_fields_and_keeps_the_answers():
    event = ev.event_from_wpp(WPP)
    event["responses"] = [{"response": 1}]
    fresh = ev.event_from_wpp(dict(WPP, eventDescription="Na praia", isEventCanceled=True))
    assert ev.apply_edit(event, fresh) is True
    assert event["description"] == "Na praia" and event["isCanceled"] is True
    assert event["responses"] == [{"response": 1}]
    assert ev.apply_edit(event, {}) is False


def test_dates_are_read_in_the_apps_formats():
    assert ev.parse_moment("01/05/2030", "12:30", "%d/%m/%Y", "%H:%M") == _ts(2030, 5, 1, 12, 30)
    assert ev.parse_moment("2030-05-01", "12:30", "%d/%m/%Y", "%H:%M") is None
    assert ev.parse_moment("01/05/2030", "", "%d/%m/%Y", "%H:%M") is None
    assert ev.format_moment(_ts(2030, 5, 1, 12, 30), "%d/%m/%Y", "%H:%M") == ("01/05/2030", "12:30")
    assert ev.format_moment(0, "%d/%m/%Y", "%H:%M") == ("", "")
    assert ev.default_start(3601) == 7200


@pytest.mark.parametrize("name, start, end, key", [
    ("", 10, None, "event_name_required"), ("A", None, None, "event_start_required"),
    ("A", 20, 10, "event_end_before_start"), ("A", 10, 20, ""), ("A", 10, None, "")])
def test_validation(name, start, end, key):
    assert ev.validate(name, start, end, 0) == key


def test_call_type_comes_from_the_link():
    assert ev.call_type_of("https://call.whatsapp.com/video/x") == "video"
    assert ev.call_type_of("https://call.whatsapp.com/voice/x") == "voice"
    assert ev.call_type_of("") == ""


def test_http_bodies(monkeypatch):
    sent = []

    def post(url, json=None, headers=None, timeout=None):
        sent.append((url.rsplit("/", 1)[-1], json))
        return SimpleNamespace(status_code=200, json=lambda: {"status": "success", "response": {"id": "X"}})
    monkeypatch.setattr(ev, "api_post", post)
    conn = ("http://h", 1, "tok")
    assert ev.create(conn, "1@g.us", {"name": "A", "startTime": 1}) == "X"
    ev.respond(conn, "true_1@g.us_ID", 3)
    ev.edit(conn, "true_1@g.us_ID", {"description": "d"})
    ev.cancel(conn, "true_1@g.us_ID")
    assert sent == [("send-event", {"phone": "1@g.us", "name": "A", "startTime": 1}),
                    ("event-respond", {"messageId": "true_1@g.us_ID", "response": 3}),
                    ("edit-event", {"messageId": "true_1@g.us_ID", "description": "d"}),
                    ("edit-event", {"messageId": "true_1@g.us_ID", "cancel": True})]


def test_a_refused_request_raises_its_code(monkeypatch):
    monkeypatch.setattr(ev, "api_post", lambda *a, **k: SimpleNamespace(
        status_code=400, json=lambda: {"status": "error", "code": "event_canceled"}))
    with pytest.raises(ValueError, match="event_canceled"):
        ev.respond(("http://h", 1, "tok"), "true_1@g.us_ID", 1)


# ------------------------------------------------------------ the panel
def test_the_panel_wiring():
    import inspect
    from ui.conversations import ConversationsPanel
    from ui.conversation_panel import message_list, attachments, list_refresh
    assert hasattr(ConversationsPanel, "_show_event_dialog")
    assert '"eventMessage"' in inspect.getsource(message_list) and "_show_event_dialog" in inspect.getsource(message_list)
    assert "_on_attach_event" in inspect.getsource(attachments)
    assert "refresh_event_responses()" in inspect.getsource(list_refresh)


def test_the_socket_event_reaches_the_open_conversation(monkeypatch):
    from core import websocket_client as wc
    calls = []
    monkeypatch.setattr(wc.wx, "CallAfter", lambda fn, *a: calls.append(a))
    panel = SimpleNamespace(on_event_changed=lambda *a: None)
    client = SimpleNamespace(main_window=SimpleNamespace(conversations_panel=panel),
                             _belongs_to_this_session=lambda d: True)
    WebSocketClient.on_event_changed(client, {"id": "3EB0AAAA", "kind": "responses"})
    WebSocketClient.on_event_changed(client, {"id": "3EB0AAAA", "kind": "bogus"})
    assert calls == [("3EB0AAAA", "responses")]


def test_the_panel_reads_the_answers_and_repaints(monkeypatch):
    from ui.conversation_panel import event_messages as em
    monkeypatch.setattr(em.threading, "Thread",
                        lambda target=None, name=None, daemon=None: SimpleNamespace(start=target))
    monkeypatch.setattr(em.wx, "CallAfter", lambda fn, *a: fn(*a))
    monkeypatch.setattr(em.ev, "read_responses",
                        lambda c, ids: {ids[0]: {"responses": [{"response": 1, "isMe": True}], "mine": 1}})
    record = {"key": {"id": "3EB0AAAA", "remoteJid": "1@g.us", "fromMe": False},
              "messageType": "eventMessage", "message": {"eventMessage": ev.event_from_wpp(WPP)}}
    repainted = []
    panel = SimpleNamespace(
        conversation={"remoteJid": "1@g.us", "messages": {"messages": {"records": [record]}}},
        main_window=SimpleNamespace(wpp_server="h", wpp_port=1, token="t",
                                    _serialize_msg_id=lambda chat, key, msg: "false_1@g.us_3EB0AAAA"),
        refresh_messages_if_changed=lambda: repainted.append(1),
        _EVENT_RESPONSES_MAX=10, _EVENT_RESPONSES_MIN_INTERVAL=10)
    for name in ("_open_event_records", "refresh_event_responses", "_apply_event_responses",
                 "_event_message_id", "on_event_changed"):
        setattr(panel, name, getattr(em.EventMessagesMixin, name).__get__(panel))
    panel.on_event_changed("3EB0AAAA", "responses")
    assert record["message"]["eventMessage"]["responses"] == [{"response": 1, "isMe": True}]
    assert repainted == [1]
    panel.on_event_changed("OTHER", "responses")          # not in this conversation
    assert repainted == [1]
