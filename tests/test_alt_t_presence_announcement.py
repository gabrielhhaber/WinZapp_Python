"""Tests for Alt+T: announcing a conversation's presence (typing/recording,
online, last seen) via AO2, from either the open conversation or the
selected row in the chat list.
"""

from main import MainWindow


class _I18n:
    TRANSLATIONS = {
        "typing_indicator": "typing...",
        "recording_indicator": "recording audio...",
        "group_presence_indicator": "{name}: {action}",
        "group_typing_singular": "{names} is typing",
        "group_typing_plural": "{names} are typing",
        "group_recording_singular": "{names} is recording audio",
        "group_recording_plural": "{names} are recording audio",
        "group_activity_and": "{first} and {second}",
        "group_activity_comma": "{first}, {second}",
        "presence_online": "Online",
        "presence_last_seen": "Last seen {time}",
        "presence_unavailable": "No presence information available",
        "time_fmt": "%H:%M",
        "datetime_fmt": "%d/%m/%Y %H:%M",
    }

    def t(self, key):
        return self.TRANSLATIONS.get(key, key)


class _Stub:
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _presence_label_for_chat = MainWindow._presence_label_for_chat
    _presence_announcement_for_chat = MainWindow._presence_announcement_for_chat
    _on_global_alt_t = MainWindow._on_global_alt_t

    def __init__(self):
        self.i18n = _I18n()
        self._composing_chats = {}
        self._presence_cache = {}
        self._lid_to_phone = {}
        self.outputs = []
        self.conversations_panel = None
        self.settings = {}

    def output(self, text, interrupt=False):
        self.outputs.append(text)

    names = None

    def _resolve_jid_name(self, jid_norm, chat_jid_norm=""):
        if self.names is not None:
            return self.names.get(jid_norm, "")
        return "Fulano"


def _chat(jid, is_group=False):
    return {"remoteJid": jid}


class TestPresenceAnnouncementContent:
    def test_typing_takes_priority(self):
        stub = _Stub()
        jid = "5511999999999@s.whatsapp.net"
        stub._composing_chats[jid] = {jid: "composing"}
        assert stub._presence_announcement_for_chat(_chat(jid)) == "typing..."

    def test_recording_takes_priority(self):
        stub = _Stub()
        jid = "5511999999999@s.whatsapp.net"
        stub._composing_chats[jid] = {jid: "recording"}
        assert stub._presence_announcement_for_chat(_chat(jid)) == "recording audio..."

    def test_group_typing_includes_participant_name(self):
        stub = _Stub()
        jid = "12345-6789@g.us"
        stub._composing_chats[jid] = {"participant@s.whatsapp.net": "composing"}
        result = stub._presence_announcement_for_chat(_chat(jid, is_group=True))
        assert result == "Fulano is typing..."

    def test_group_names_everyone_active(self):
        stub = _Stub()
        stub.names = {"a@s.whatsapp.net": "Ana", "b@s.whatsapp.net": "Bia"}
        jid = "12345-6789@g.us"
        stub._composing_chats[jid] = {
            "a@s.whatsapp.net": "composing",
            "b@s.whatsapp.net": "recording",
        }
        result = stub._presence_announcement_for_chat(_chat(jid, is_group=True))
        assert result == "Ana is typing and Bia is recording audio..."

    def test_group_setting_off_keeps_the_old_wording(self):
        stub = _Stub()
        stub.settings = {"speech_content": {"announce_group_multiple_activity": False}}
        jid = "12345-6789@g.us"
        stub._composing_chats[jid] = {"participant@s.whatsapp.net": "composing"}
        result = stub._presence_announcement_for_chat(_chat(jid, is_group=True))
        assert result == "Fulano: typing..."

    def test_online_when_no_typing(self):
        stub = _Stub()
        jid = "5511999999999@s.whatsapp.net"
        stub._presence_cache[jid] = {"lastKnownPresence": "available", "lastSeen": None}
        assert stub._presence_announcement_for_chat(_chat(jid)) == "Online"

    def test_last_seen_when_unavailable(self):
        stub = _Stub()
        jid = "5511999999999@s.whatsapp.net"
        stub._presence_cache[jid] = {
            "lastKnownPresence": "unavailable",
            "lastSeen": 1_700_000_000,
        }
        result = stub._presence_announcement_for_chat(_chat(jid))
        assert result.startswith("Last seen ")

    def test_no_info_when_cache_empty(self):
        stub = _Stub()
        jid = "5511999999999@s.whatsapp.net"
        assert (
            stub._presence_announcement_for_chat(_chat(jid))
            == "No presence information available"
        )

    def test_group_without_typing_reports_unavailable(self):
        stub = _Stub()
        jid = "12345-6789@g.us"
        assert (
            stub._presence_announcement_for_chat(_chat(jid, is_group=True))
            == "No presence information available"
        )

    def test_no_network_call_made(self, monkeypatch):
        stub = _Stub()
        jid = "5511999999999@s.whatsapp.net"

        def _boom(*a, **kw):
            raise AssertionError("get_last_seen must not be called by Alt+T")

        stub.get_last_seen = _boom
        stub._presence_announcement_for_chat(_chat(jid))


class TestAltTHandler:
    def test_speaks_open_conversation_presence(self):
        stub = _Stub()
        jid = "5511999999999@s.whatsapp.net"
        stub._presence_cache[jid] = {"lastKnownPresence": "available", "lastSeen": None}
        stub.conversations_panel = type(
            "Panel", (), {"conversation": _chat(jid)}
        )()

        stub._on_global_alt_t(None)

        assert stub.outputs == ["Online"]

    def test_falls_back_to_selected_chat_in_list(self):
        stub = _Stub()
        jid = "5511999999999@s.whatsapp.net"
        stub._presence_cache[jid] = {"lastKnownPresence": "available", "lastSeen": None}
        panel = type(
            "Panel", (),
            {
                "conversation": None,
                "_selected_chat_from_list": lambda self: _chat(jid),
            },
        )()
        stub.conversations_panel = panel

        stub._on_global_alt_t(None)

        assert stub.outputs == ["Online"]

    def test_no_conversation_and_no_selection_does_nothing(self):
        stub = _Stub()
        panel = type(
            "Panel", (),
            {"conversation": None, "_selected_chat_from_list": lambda self: None},
        )()
        stub.conversations_panel = panel

        stub._on_global_alt_t(None)

        assert stub.outputs == []

    def test_no_conversations_panel_does_not_crash(self):
        stub = _Stub()
        stub.conversations_panel = None

        stub._on_global_alt_t(None)

        assert stub.outputs == []
