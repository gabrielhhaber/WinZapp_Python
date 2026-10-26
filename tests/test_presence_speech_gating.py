"""Speech gating for live typing/recording presence events."""

import pytest
import wx

from main import MainWindow


GROUP_JID = "12345-6789@g.us"
PARTICIPANT_JID = "5511999999999@s.whatsapp.net"


class _Timer:
    def Stop(self):
        pass


class _Speech:
    def __init__(self):
        self.outputs = []

    def output(self, text):
        self.outputs.append(text)


class _I18n:
    def t(self, key):
        return {
            "typing_text": "{name} está digitando...",
            "recording_text": "{name} está gravando áudio...",
        }.get(key, key)


class _Panel:
    def __init__(self, conversation_jid=GROUP_JID):
        self.conversation = (
            {"remoteJid": conversation_jid} if conversation_jid else None
        )

    def _refresh_presence_note(self, _jid):
        pass


class _Stub:
    on_presence_update = MainWindow.on_presence_update
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _cancel_deferred_contact_presence = MainWindow._cancel_deferred_contact_presence

    def __init__(self, *, muted=False, archived=False, conversation_jid=GROUP_JID):
        self._muted = muted
        self._archived = archived
        self._window_hidden = False
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self._presence_cache = {}
        self._composing_chats = {}
        self._presence_timers = {}
        self._presence_pushname_map = {}
        self.contacts = {}
        self.chats = {}
        self.db = None
        self.settings = {
            "speech_content": {
                "announce_typing": True,
                "announce_recording": True,
            }
        }
        self.conversations_panel = _Panel(conversation_jid)
        self.speak_output = _Speech()
        self.i18n = _I18n()
        self.refreshed_rows = []

    def IsShown(self):
        return True

    def IsIconized(self):
        return False

    def IsActive(self):
        return True

    def is_chat_muted(self, _jid):
        return self._muted

    def is_chat_archived(self, _jid):
        return self._archived

    def _resolve_jid_name(self, _participant_jid, _chat_jid=""):
        return "Fulano"

    def _refresh_chat_row_in_list(self, jid, update_title=True):
        self.refreshed_rows.append(jid)


@pytest.fixture(autouse=True)
def _inline_call_later(monkeypatch):
    monkeypatch.setattr(wx, "CallLater", lambda *_args, **_kwargs: _Timer())


@pytest.mark.parametrize(
    ("muted", "archived"),
    [(True, False), (False, True)],
)
def test_open_active_chat_announces_even_when_muted_or_archived(muted, archived):
    stub = _Stub(muted=muted, archived=archived)

    stub.on_presence_update(GROUP_JID, {
        PARTICIPANT_JID: {"lastKnownPresence": "composing", "lastSeen": None}
    })

    assert stub.speak_output.outputs == ["Fulano está digitando..."]


def test_recording_uses_its_own_enabled_setting():
    stub = _Stub(muted=True)
    stub.settings["speech_content"]["announce_recording"] = False

    stub.on_presence_update(GROUP_JID, {
        PARTICIPANT_JID: {"lastKnownPresence": "recording", "lastSeen": None}
    })

    assert stub.speak_output.outputs == []


def test_closed_chat_does_not_announce_presence():
    stub = _Stub(conversation_jid="")

    stub.on_presence_update(GROUP_JID, {
        PARTICIPANT_JID: {"lastKnownPresence": "composing", "lastSeen": None}
    })

    assert stub.speak_output.outputs == []


class _Widget:
    def __init__(self, shown):
        self._shown = shown

    def IsShown(self):
        return self._shown


class _ShownPanel(_Panel):
    """A conversations panel that reports whether it, and its detail pane, are on screen."""

    def __init__(self, panel_shown, detail_shown, conversation_jid=GROUP_JID):
        super().__init__(conversation_jid)
        self._panel_shown = panel_shown
        self.conversation_panel = _Widget(detail_shown)

    def IsShown(self):
        return self._panel_shown


@pytest.mark.parametrize("presence", ["composing", "recording"])
@pytest.mark.parametrize(
    ("panel_shown", "detail_shown", "spoken"),
    [
        (True, True, True),
        # Switched to another panel: the panel itself is hidden (Status, Calls...).
        (False, True, False),
        # Alt+4 / Alt+1 with a conversation from the other panel: only its detail pane is hidden.
        (True, False, False),
    ],
)
def test_typing_and_recording_are_announced_only_while_the_conversation_is_on_screen(
    presence, panel_shown, detail_shown, spoken
):
    stub = _Stub()
    stub.conversations_panel = _ShownPanel(panel_shown, detail_shown)

    stub.on_presence_update(GROUP_JID, {
        PARTICIPANT_JID: {"lastKnownPresence": presence, "lastSeen": None}
    })

    assert bool(stub.speak_output.outputs) is spoken
    # The state itself is still tracked: the chat-list row keeps its label
    # for a conversation that is merely not on screen.
    assert stub.refreshed_rows == [GROUP_JID]
    assert stub._composing_chats[GROUP_JID]
