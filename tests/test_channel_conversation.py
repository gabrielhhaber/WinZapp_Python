"""A channel's conversation (ui/conversation_panel/channel_conversation.py):
who pins there, and which loaded updates are pinned."""

from types import SimpleNamespace

from core import channels as ch
from core.websocket_client import WebSocketClient
from ui.conversation_panel.channel_conversation import (
    ChannelConversationMixin,
    remember_channel_roles,
)
from ui.conversation_panel.message_actions import MessageActionsMixin

CHANNEL = "120363000000000001@newsletter"


class _Normalizer:
    _normalize_wpp_message = WebSocketClient._normalize_wpp_message
    _clean_jid = WebSocketClient._clean_jid


def raw_update(jid=CHANNEL, server_id=4977):
    return {"id": f"false_{jid}_3EB0BBBBBBBBBBBBBBBB", "type": "chat", "t": 1790000200, "from": jid,
            "to": jid, "fromMe": False, "body": "Novidade", "serverId": server_id}


def test_a_channel_update_keeps_its_server_id_and_nothing_else_gets_one():
    assert _Normalizer()._normalize_wpp_message(raw_update())["serverId"] == 4977
    assert "serverId" not in _Normalizer()._normalize_wpp_message(raw_update(server_id=True))
    assert "serverId" not in _Normalizer()._normalize_wpp_message(raw_update("777@lid"))


def channel(role="admin", pinned=(8,)):
    return ch.Channel(jid=CHANNEL, name="", description="", followers=None, verified=False,
                      role=role, muted=None, invite="", created=0, pinned=pinned)


class Panel(ChannelConversationMixin):
    def __init__(self, role=""):
        self.main_window = SimpleNamespace(_channel_roles={CHANNEL: role} if role else {})
        self.refreshed = []
        self.conversation = {"remoteJid": CHANNEL, "messages": {"messages": {"records": [
            {"key": {"id": "A"}, "serverId": 7}, {"key": {"id": "B"}, "serverId": 8}]}}}

    def refresh_composer_permissions(self, jid, transition=True):
        self.refreshed.append(jid)


def test_only_an_admin_pins_and_only_an_update_with_a_server_id():
    assert Panel("admin")._can_pin_channel_update({"serverId": 7})
    assert not Panel("admin")._can_pin_channel_update({})
    assert not Panel("subscriber")._can_pin_channel_update({"serverId": 7})
    assert not Panel()._can_pin_channel_update({"serverId": 7})


def test_the_read_remembers_the_role_and_gives_the_pinned_updates():
    panel = Panel()
    pins = panel._channel_pins_from(CHANNEL, channel("owner"))
    assert [m["key"]["id"] for m in pins] == ["B"] and pins[0]["pinInChat"]
    assert panel.main_window._channel_roles == {CHANNEL: "owner"}
    assert panel.refreshed == [CHANNEL]                   # the composer follows the role
    assert panel._channel_pins_from(CHANNEL, None) is None


def test_the_channels_window_fills_the_roles_it_read():
    window = SimpleNamespace()
    remember_channel_roles(window, [channel("admin"), channel("")])
    assert window._channel_roles == {CHANNEL: "admin"}


class PinPanel(Panel):
    _on_menu_pin_message = MessageActionsMixin._on_menu_pin_message

    def __init__(self, role):
        super().__init__(role)
        self.said = []
        self.main_window.output = self.said.append
        self.main_window.i18n = SimpleNamespace(t=lambda key: key)

    def _reject_system_event_action(self, msg):
        return False


def test_a_follower_pressing_pin_hears_why_and_nothing_changes():
    panel = PinPanel("subscriber")
    msg = {"key": {"id": "A"}, "serverId": 7}
    panel._on_menu_pin_message(msg)
    assert panel.said == ["channel_pin_admins_only"]
    assert "pinInChat" not in msg


def test_a_channel_update_goes_out_as_plain_text_to_the_channel(monkeypatch):
    from tests.test_reply_with_mention_keeps_quote import _OK, QUOTED, _Response, _Stub, _answer
    calls = _answer(monkeypatch, _Response(201, _OK))
    _Stub().send_text_message(CHANNEL, "Novidade", quoted=QUOTED,
                              mentioned_jids=["5511888888888@s.whatsapp.net"],
                              link_preview={"title": "x"})
    (route, payload), = calls
    assert route == "send-message"
    assert payload["phone"] == [CHANNEL] and payload["isNewsletter"] is True
    assert "messageId" not in payload and "mentioned" not in payload


class SendPanel(Panel):
    from ui.conversation_panel.text_sending import TextSendingMixin
    on_send_message = TextSendingMixin.on_send_message

    def __init__(self, role):
        super().__init__(role)
        self.message_field = SimpleNamespace(GetValue=lambda: "Novidade")
        self._editing_message_id = None
        self.sent = []
        self.main_window.ensure_meta_ai_terms = lambda jid: True

    def _text_with_trailing_emoticon(self, text):
        return text

    def _send_new_text_message(self, text, jid):
        self.sent.append((text, jid))


def test_only_an_admin_posts_from_the_field():
    follower, admin = SendPanel("subscriber"), SendPanel("admin")
    follower.on_send_message(None)
    admin.on_send_message(None)
    assert follower.sent == [] and admin.sent == [("Novidade", CHANNEL)]
