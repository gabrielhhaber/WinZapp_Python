"""The buttons of a business's message (core/business_buttons.py,
ui/conversation_panel/business_buttons.py, api_patches buttonReplyController.ts).

What must hold: each shape of button is read off the message the server
serialized, a link or a code or a number is never answered through the server,
a reply is one request that is never repeated, and a second tap while the first
is on its way sends nothing.
"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import business_buttons as bb
from core.business_buttons import (ButtonReplyError, business_button_label,
                                   business_buttons, reply_to_button)
from core.websocket_client import WebSocketClient
from ui.conversation_panel.business_buttons import BusinessButtonsMixin

CONNECTION = ("http://localhost", 6300, "synthetic-token")
HEADERS = {"Authorization": "Bearer synthetic-token"}
MESSAGE = "false_777@lid_3EB0AAAAAAAAAAAAAAAA"
URL = "http://localhost:6300/api/synthetic-token/reply-button/false_777%40lid_3EB0AAAAAAAAAAAAAAAA"


def answer(body, status=200):
    return SimpleNamespace(status_code=status, json=lambda: body)


def native(name, **params):
    return {"name": name, "buttonParamsJson": json.dumps(params)}


class TestReadingTheButtons:
    def test_reply_buttons_of_a_plain_message_carry_their_id(self):
        msg = {"dynamicReplyButtons": [
            {"buttonId": "1", "buttonText": {"displayText": "Car"}, "type": 1},
            {"buttonId": "2", "buttonText": {"displayText": "Home"}, "type": 1}]}
        assert business_buttons(msg) == [
            {"kind": "reply", "source": "dynamic", "index": 0, "label": "Car", "value": "1"},
            {"kind": "reply", "source": "dynamic", "index": 1, "label": "Home", "value": "2"}]

    def test_an_interactive_message_gives_each_kind_its_own_value(self):
        msg = {"interactivePayload": {"buttons": [
            native("quick_reply", display_text="Debt", id="d1"),
            native("cta_url", display_text="Site", url="https://x.com/a"),
            native("cta_copy", display_text="Code", copy_code="ABC"),
            native("cta_call", display_text="Call", phone_number="+5511900000001")]}}
        assert [(b["kind"], b["index"], b["label"], b["value"]) for b in business_buttons(msg)] == [
            ("reply", 0, "Debt", "d1"), ("url", 1, "Site", "https://x.com/a"),
            ("copy", 2, "Code", "ABC"), ("call", 3, "Call", "+5511900000001")]

    def test_the_index_is_the_place_in_the_original_list(self):
        msg = {"interactivePayload": {"buttons": [
            native("cta_catalog", display_text="Catalog"), native("quick_reply", display_text="Yes", id="y")]}}
        assert [(b["index"], b["source"]) for b in business_buttons(msg)] == [(1, "native")]

    def test_carousel_buttons_name_their_card_and_keep_original_positions(self):
        actions = []
        trip = "17:30 → 12:30\nR$ 529,97\nCama\n" + ("Descrição da viagem " * 8).strip()
        for card_index in range(10):
            for action in range(2):
                actions.append({**native("quick_reply", display_text="Select", id=f"{card_index}-{action}"),
                                "cardIndex": card_index,
                                "cardTitle": f"Trip {card_index + 1}",
                                "cardText": f"Trip {card_index + 1}\n{trip}"})
        found = business_buttons({"interactivePayload": {"buttons": actions}})
        assert len(found) == 20
        assert found[0] == {"kind": "reply", "source": "native", "index": 0,
                            "label": f"Trip 1\n{trip}", "value": "0-0"}
        assert found[-1] == {"kind": "reply", "source": "native", "index": 19,
                             "label": f"Trip 10\n{trip}", "value": "9-1"}
        assert len(found[0]["label"]) > bb.LABEL_LIMIT

    def test_old_carousel_metadata_uses_full_card_title_without_number_or_action(self):
        action = {**native("quick_reply", display_text="Selecionar", id="trip-1"),
                  "cardIndex": 7, "cardTitle": "17:30 → 12:30\nR$ 529,97\nCama\nEXP"}
        assert business_buttons({"interactivePayload": {"buttons": [action]}}) == [
            {"kind": "reply", "source": "native", "index": 0,
             "label": "17:30 → 12:30\nR$ 529,97\nCama\nEXP", "value": "trip-1"}]

    def test_cached_carousel_button_loses_old_number_and_select_text(self):
        msg = {"messageType": "interactiveMessage"}
        button = {"kind": "reply", "source": "native", "index": 2,
                  "label": "3. 15:45 → 14:30\nR$ 330,97\nSemi-lei: Selecionar", "value": "trip-3"}
        assert business_button_label(msg, button) == "15:45 → 14:30\nR$ 330,97\nSemi-lei"
        assert button["index"] == 2 and button["value"] == "trip-3"

    def test_other_buttons_and_new_carousel_labels_are_unchanged(self):
        label = "3. Trip: Select"
        button = {"kind": "reply", "source": "native", "label": label}
        assert business_button_label({"messageType": "conversation"}, button) == label
        assert business_button_label({"messageType": "interactiveMessage"},
                                     {"kind": "reply", "source": "native",
                                      "label": "15:45 → 14:30\nR$ 330,97\nSemi-lei"}) == \
            "15:45 → 14:30\nR$ 330,97\nSemi-lei"

    def test_the_rows_of_a_list_are_numbered_across_its_sections(self):
        msg = {"list": {"buttonText": "Menu", "sections": [
            {"rows": [{"title": "Outage", "rowId": "r1"}, {"title": " ", "rowId": "skipped"}]},
            {"rows": [{"title": "Bills", "rowId": "r3", "description": "x"}]}]}}
        assert [(b["source"], b["index"], b["label"], b["value"]) for b in business_buttons(msg)] == [
            ("list", 0, "Outage", "r1"), ("list", 2, "Bills", "r3")]

    def test_template_buttons_are_answered_by_index(self):
        msg = {"hydratedButtons": [{"quickReplyButton": {"id": "q", "displayText": "Yes"}},
                                   {"urlButton": {"displayText": "Site", "url": "https://x.com"}},
                                   {"callButton": {"displayText": "Call", "phoneNumber": "123"}}]}
        assert [(b["kind"], b["source"], b["index"]) for b in business_buttons(msg)] == [
            ("reply", "template", 0), ("url", "template", 1), ("call", "template", 2)]

    @pytest.mark.parametrize("msg", [
        {}, None, "text", {"dynamicReplyButtons": "x"},
        {"dynamicReplyButtons": [{"buttonId": "1", "buttonText": {"displayText": " "}}]},
        {"dynamicReplyButtons": [{"buttonText": {"displayText": "No id"}}]},
        {"interactivePayload": {"buttons": [native("quick_reply", display_text="Used", id="u", disabled=True)]}},
        {"interactivePayload": {"buttons": [native("cta_url", display_text="Bad", url="javascript:alert(1)")]}},
        {"interactivePayload": {"buttons": [{"name": "quick_reply", "buttonParamsJson": "not json"}]}},
        {"interactivePayload": {"buttons": [native("cta_copy", display_text="Empty", copy_code="")]}},
        {"hydratedButtons": [{"urlButton": {"displayText": "Bad", "url": "ftp://x"}}]},
    ])
    def test_what_cannot_be_acted_on_is_not_offered(self, msg):
        assert business_buttons(msg) == []

    def test_a_long_label_is_cut_and_the_number_of_buttons_is_limited(self):
        msg = {"dynamicReplyButtons": [
            {"buttonId": str(i), "buttonText": {"displayText": "x" * 200}} for i in range(40)]}
        found = business_buttons(msg)
        assert len(found) == bb.MAX_BUTTONS and len(found[0]["label"]) == bb.LABEL_LIMIT


class _Normalizer:
    _normalize_wpp_message = WebSocketClient._normalize_wpp_message
    _clean_jid = WebSocketClient._clean_jid


def test_the_buttons_are_kept_with_the_message_and_only_when_there_are_some():
    raw = {"id": "false_777@lid_3EB0BBBBBBBBBBBBBBBB", "type": "chat", "t": 1790000200, "from": "777@lid",
           "to": "999@lid", "fromMe": False, "body": "Home or car?",
           "dynamicReplyButtons": [{"buttonId": "2", "buttonText": {"displayText": "Home"}}],
           "chatId": {"server": "lid", "user": "777", "_serialized": "777@lid"}}
    kept = _Normalizer()._normalize_wpp_message(raw)
    assert kept["businessButtons"] == [
        {"kind": "reply", "source": "dynamic", "index": 0, "label": "Home", "value": "2"}]
    del raw["dynamicReplyButtons"]
    assert "businessButtons" not in _Normalizer()._normalize_wpp_message(raw)


def test_legacy_unknown_button_message_recovers_its_text_and_options():
    raw = {"id": "false_777@lid_3EB0CCCCCCCCCCCCCCCC", "type": "unknown",
           "t": 1790000200, "from": "777@lid", "to": "999@lid",
           "fromMe": False, "body": "Choose a trip",
           "dynamicReplyButtons": [{"buttonId": "1", "buttonText": {"displayText": "Tomorrow"}}]}
    message = _Normalizer()._normalize_wpp_message(raw)
    assert message["messageType"] == "buttonsMessage"
    assert message["message"]["buttonsMessage"] == {
        "contentText": "Choose a trip", "buttons": raw["dynamicReplyButtons"]}
    assert message["businessButtons"][0]["label"] == "Tomorrow"


def test_interactive_card_and_button_reply_have_displayable_content():
    base = {"id": "false_777@lid_3EB0DDDDDDDDDDDDDDDD", "t": 1790000200,
            "from": "777@lid", "to": "999@lid", "fromMe": False}
    card = _Normalizer()._normalize_wpp_message({
        **base, "type": "interactive", "body": "Ticket\nDeparture at noon"})
    assert card["messageType"] == "interactiveMessage"
    assert card["message"]["interactiveMessage"]["body"]["text"] == "Ticket\nDeparture at noon"

    reply = _Normalizer()._normalize_wpp_message({
        **base, "type": "buttons_response", "body": "Tomorrow",
        "selectedButtonID": "tomorrow"})
    assert reply["messageType"] == "buttonsResponseMessage"
    assert reply["message"]["buttonsResponseMessage"]["selectedDisplayText"] == "Tomorrow"


class TestTheRequest:
    BUTTON = {"kind": "reply", "source": "native", "index": 2, "label": "Yes", "value": "y"}

    def test_a_reply_is_one_post_with_the_source_and_the_index(self, monkeypatch):
        post = Mock(return_value=answer({"status": "success", "response": {"sent": True}}))
        monkeypatch.setattr(bb, "api_post", post)
        reply_to_button(CONNECTION, MESSAGE, self.BUTTON)
        post.assert_called_once_with(URL, json={"source": "native", "index": 2}, headers=HEADERS, timeout=45)

    @pytest.mark.parametrize("body, status, code", [
        ({"status": "error", "code": "button_used"}, 409, "button_used"),
        ({"status": "error", "code": "reply_button_unavailable"}, 503, "unavailable"),
        ({"status": "error", "code": 5}, 500, "unavailable"),
        ("nothing", 200, "unavailable"),
    ])
    def test_a_refusal_keeps_the_servers_code_and_is_not_retried(self, monkeypatch, body, status, code):
        post = Mock(return_value=answer(body, status))
        monkeypatch.setattr(bb, "api_post", post)
        with pytest.raises(ButtonReplyError) as error:
            reply_to_button(CONNECTION, MESSAGE, self.BUTTON)
        assert error.value.code == code and post.call_count == 1

    def test_a_failed_connection_is_unavailable(self, monkeypatch):
        monkeypatch.setattr(bb, "api_post", Mock(side_effect=ConnectionError("down")))
        with pytest.raises(ButtonReplyError) as error:
            reply_to_button(CONNECTION, MESSAGE, self.BUTTON)
        assert error.value.code == "unavailable"

    @pytest.mark.parametrize("message, button", [
        ("../x", BUTTON), ("false_777@g.us_3EB0AAAAAAAAAAAA", BUTTON), (None, BUTTON),
        (MESSAGE, {**BUTTON, "kind": "url"}), (MESSAGE, {**BUTTON, "source": "other"}), (MESSAGE, None),
    ])
    def test_nothing_is_sent_for_what_is_not_a_reply_to_a_message(self, monkeypatch, message, button):
        post = Mock()
        monkeypatch.setattr(bb, "api_post", post)
        with pytest.raises(ButtonReplyError):
            reply_to_button(CONNECTION, message, button)
        post.assert_not_called()


class _Panel(BusinessButtonsMixin):
    """The mixin on a plain stub: no window."""

    def __init__(self):
        self.said = []
        self.main_window = SimpleNamespace(
            i18n=SimpleNamespace(t=lambda key: key),
            output=lambda text, interrupt=False: self.said.append(text),
            _serialize_msg_id=lambda jid, key, msg: MESSAGE,
            wpp_server="http://localhost", wpp_port=6300, token="synthetic-token")
        self.conversation = {"remoteJid": "777@lid"}
        self.opened = []
        self._activate_link = self.opened.append


MSG = {"key": {"id": "3EB0AAAAAAAAAAAAAAAA"}}


def test_a_link_opens_and_a_number_is_spoken_without_asking_the_server(monkeypatch):
    post = Mock()
    monkeypatch.setattr(bb, "api_post", post)
    panel = _Panel()
    panel._on_business_button({"kind": "url", "value": "https://x.com"}, MSG)
    panel._on_business_button({"kind": "call", "value": "123"}, MSG)
    assert panel.opened == ["https://x.com"] and panel.said == ["business_button_call"]
    post.assert_not_called()


def test_a_reply_is_sent_once_and_a_second_tap_meanwhile_sends_nothing(monkeypatch):
    sent = []
    monkeypatch.setattr("ui.conversation_panel.business_buttons.reply_to_button",
                        lambda connection, message_id, button: sent.append((connection, message_id, button)))
    done = []
    monkeypatch.setattr("ui.conversation_panel.business_buttons.wx.CallAfter", lambda fn, *a: done.append((fn, a)))
    monkeypatch.setattr("ui.conversation_panel.business_buttons.threading.Thread",
                        lambda target, daemon: SimpleNamespace(start=target))
    panel = _Panel()
    button = {"kind": "reply", "source": "dynamic", "index": 1, "label": "Home", "value": "2"}
    panel._on_business_button(button, MSG)
    panel._on_business_button(button, MSG)
    assert sent == [(("http://localhost", 6300, "synthetic-token"), MESSAGE, button)]
    fn, args = done[0]
    fn(*args)
    assert panel.said == ["business_button_sent"]
    panel._on_business_button(button, MSG)       # answered: a new tap is a new reply
    assert len(sent) == 2


def test_a_reply_that_failed_says_so(monkeypatch):
    def refuse(connection, message_id, button):
        raise ButtonReplyError("unavailable")
    monkeypatch.setattr("ui.conversation_panel.business_buttons.reply_to_button", refuse)
    monkeypatch.setattr("ui.conversation_panel.business_buttons.wx.CallAfter", lambda fn, *a: fn(*a))
    monkeypatch.setattr("ui.conversation_panel.business_buttons.threading.Thread",
                        lambda target, daemon: SimpleNamespace(start=target))
    panel = _Panel()
    panel._on_business_button({"kind": "reply", "source": "native", "index": 0, "label": "Yes", "value": "y"}, MSG)
    assert panel.said == ["business_button_failed"]
