"""Published wire shapes through the real main handler, coordinator and renderers."""
import itertools
import json
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from core.contact_presence import cached, merge
from core.websocket_client import WebSocketClient
import core.websocket_client as transport
from main_window.chat_events import ChatEventsMixin
from ui.conversation_panel.conversation_info import ConversationInfoMixin
from ui.dialogs.personal_presence import presence_line
from tests.test_contact_presence_deferred_refresh import Clockwork
from tests.test_contact_presence_freshness import PN, LID
from tests.test_contact_presence_views import views


FIXTURE = json.loads((Path(__file__).parent / "fixtures/contact_presence_transitions.json").read_text(encoding="utf-8-sig"))


def pipeline(monkeypatch):
    loop = Clockwork(monkeypatch)
    owner = loop.owner
    owner.on_presence_update = MethodType(ChatEventsMixin.on_presence_update, owner)
    owner._composing_chats = {}
    owner._presence_pushname_map = {}
    owner.contacts, owner.settings, owner.db = {}, {"speech_content": {}}, None
    owner._refresh_chat_row_in_list = lambda *args: None
    owner.speak_output = SimpleNamespace(output=lambda *args: pytest.fail("Unexpected speech"))
    panel, _, button, _, _ = views(owner)
    panel._contact_presence_visit = 1
    panel._refresh_presence_note = MethodType(ConversationInfoMixin._refresh_presence_note, panel)
    owner.conversations_panel = panel
    peer = SimpleNamespace(main_window=owner, _presence_delivery_generation=0,
                           _belongs_to_this_session=lambda payload: True)
    monkeypatch.setattr(transport, "wx", SimpleNamespace(CallAfter=lambda fn: loop.callbacks.append(fn)))
    def deliver(kind):
        WebSocketClient.on_wpp_presence_changed(peer, {**FIXTURE[kind], "id": PN})
        while loop.callbacks:
            loop.callbacks.pop(0)()
    def rendered():
        panel._refresh_presence_note(PN)
        return (ChatEventsMixin._presence_announcement_for_chat(owner, panel.conversation),
                button.text, presence_line(owner, PN, owner.i18n))
    return loop, deliver, rendered, button


def test_published_offline_event_gets_one_followup_and_updates_three_views(monkeypatch):
    loop, deliver, rendered, button = pipeline(monkeypatch)
    loop.begin()
    assert rendered() == ("Online", "Online", "Online")
    loop.now = 101
    loop.ready()
    deliver("offline_event")
    assert rendered() == ("Unknown", "Contact", "")
    loop.advance(110)
    assert len(loop.reads) == 2
    assert all("Seen" in value for value in rendered())
    assert button.focuses == 0
    loop.advance(1000)
    assert len(loop.reads) == 2  # HTTP application did not create another demand.


def test_published_denial_clears_three_views_and_cancels_waiting_read(monkeypatch):
    loop, deliver, rendered, button = pipeline(monkeypatch)
    loop.begin()
    loop.now = 101
    deliver("offline_event")
    loop.now = 102
    deliver("denied_event")
    assert cached(loop.owner, PN)["restricted"] is True
    assert rendered() == ("Unknown", "Contact", "")
    loop.advance(110)
    assert len(loop.reads) == 1
    assert button.focuses == 0


ACTIONS = ("offline", "online", "deny", "reopen", "mapping", "disconnect", "shutdown", "hidden")


@pytest.mark.parametrize("actions", tuple(itertools.product(ACTIONS, repeat=2)))
def test_pending_followup_after_every_pair_of_context_changes(monkeypatch, actions):
    loop = Clockwork(monkeypatch)
    loop.begin()
    loop.now = 101
    loop.offline()
    state = "offline"
    blocked, stale_scope = False, False
    for step, action in enumerate(actions, 2):
        loop.now = 100 + step
        if action == "offline":
            loop.offline()
            state = "offline"
            stale_scope = False  # A new explicit event owns the current visit/mapping.
        elif action == "online":
            merge(loop.owner, PN, {"lastKnownPresence": "available", "isOnline": True})
            state = "online"
        elif action == "deny":
            merge(loop.owner, PN, {"restricted": True})
            blocked = True
        elif action == "reopen":
            loop.owner.conversations_panel._contact_presence_visit += 1
            stale_scope = True
        elif action == "mapping":
            loop.owner._lid_to_phone[LID] = PN
            loop.owner._phone_to_lid[PN] = LID
            stale_scope = True
        elif action == "disconnect":
            loop.owner._wa_connected = False
            loop.owner._invalidate_contact_presence()
            blocked = True
        elif action == "shutdown":
            loop.owner._shutting_down = True
            blocked = True
        else:
            loop.owner._window_hidden = True
            blocked = True
    loop.ready()
    loop.advance(110)
    assert len(loop.reads) == (2 if not blocked and not stale_scope and state == "offline" else 1)
    loop.advance(1000)
    assert len(loop.reads) <= 2
