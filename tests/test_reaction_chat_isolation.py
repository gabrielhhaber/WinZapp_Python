"""Delayed own reactions must stay in the chat selected before the worker starts.

Only plain stubs are used: no wx.App, windows, API requests or real database.
Two chats deliberately reuse a bare message id to catch a wrong-chat repaint.
"""

from types import SimpleNamespace

import pytest

from ui.conversation_panel import reactions as reaction_module
from ui.conversation_panel.list_refresh import ListRefreshMixin
from ui.conversation_panel.reactions import ReactionsMixin


CHAT_A = "chat-A@g.us"
CHAT_B = "chat-B@g.us"
MESSAGE_ID = "shared-message-id"


def _message(jid, mid=MESSAGE_ID):
    return {"key": {"id": mid, "remoteJid": jid, "fromMe": False},
            "messageType": "conversation", "message": {"conversation": "Synthetic"}}


class _List:
    def __init__(self):
        self.writes = []
        self.freezes = 0

    def Freeze(self):
        self.freezes += 1

    def Thaw(self):
        self.freezes -= 1

    def SetItemText(self, index, text):
        self.writes.append((index, text))

    def GetFocusedItem(self):
        return 0


class _Panel(ReactionsMixin):
    _matches_open_conversation = ListRefreshMixin._matches_open_conversation

    def __init__(self, success=True):
        self.chats = {
            jid: {"remoteJid": jid, "messages": {"messages": {"records": []}}}
            for jid in (CHAT_A, CHAT_B)
        }
        self.sent = []
        self.persisted = []
        self.previews = []
        self.button_updates = []
        self.main_window = SimpleNamespace(
            get_chat=self.chats.get,
            send_reaction=lambda jid, key, emoji: self.sent.append((jid, key, emoji)) or success,
            db=SimpleNamespace(queue_message=lambda jid, record: self.persisted.append((jid, record))),
            _track_last_reaction=lambda jid, record: self.previews.append(jid),
            _schedule_set_chats=lambda: None,
            _schedule_save_settings=lambda: None,
            settings={},
        )
        self.messages_list = _List()
        self.open(CHAT_A)

    def open(self, jid, mid=MESSAGE_ID):
        self.conversation = self.chats[jid]
        self._sorted_messages = [_message(jid, mid)]
        self._reaction_map = {}

    def _is_separator(self, msg):
        return False

    def _render_message_line(self, msg):
        return str(self._reaction_counts(msg["key"]["id"]))

    def _update_reactions_button(self, index):
        self.button_updates.append(index)


@pytest.fixture
def deferred(monkeypatch):
    workers = []
    callbacks = []

    class _Thread:
        def __init__(self, *, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            workers.append((self.target, self.args))

    monkeypatch.setattr(reaction_module.threading, "Thread", _Thread)
    monkeypatch.setattr(reaction_module.wx, "CallAfter",
                        lambda fn, *args: callbacks.append((fn, args)))
    return workers, callbacks


@pytest.mark.parametrize("emoji", ["👍", ""])
@pytest.mark.parametrize("close_before_worker", [False, True])
def test_send_keeps_original_chat_and_key_before_worker_starts(deferred, emoji, close_before_worker):
    workers, callbacks = deferred
    panel = _Panel()
    msg = _message(CHAT_A)
    panel._send_reaction(msg, emoji)
    panel.open(CHAT_B)
    if close_before_worker:
        panel.conversation = None
    msg["key"]["id"] = "changed-after-selection"

    worker, args = workers.pop()
    worker(*args)
    assert panel.sent == [(CHAT_A, _message(CHAT_A)["key"], emoji)]

    callback, args = callbacks.pop()
    callback(*args)
    assert panel._reaction_map == {}
    assert panel.messages_list.writes == []
    assert panel.button_updates == []
    assert panel.persisted[0][0] == CHAT_A
    assert panel.persisted[0][1]["message"]["reactionMessage"]["text"] == emoji
    assert panel.previews == [CHAT_A]


@pytest.mark.parametrize("emoji", ["👍", ""])
@pytest.mark.parametrize("other_id", [MESSAGE_ID, "unrelated-message-id"])
def test_success_after_switch_preserves_other_chats_reaction(deferred, emoji, other_id):
    workers, callbacks = deferred
    panel = _Panel()
    panel._send_reaction(_message(CHAT_A), emoji)
    worker, args = workers.pop()
    worker(*args)
    panel.open(CHAT_B, other_id)
    panel._reaction_map = {other_id: {panel._SELF_REACTOR_KEY: "😂"}}

    callback, args = callbacks.pop()
    callback(*args)
    assert panel._reaction_map == {other_id: {panel._SELF_REACTOR_KEY: "😂"}}
    assert panel.messages_list.writes == []
    assert panel.button_updates == []
    assert panel.persisted[0][0] == CHAT_A
    assert panel.chats[CHAT_B]["messages"]["messages"]["records"] == []


def test_success_after_returning_to_original_chat_updates_it(deferred):
    workers, callbacks = deferred
    panel = _Panel()
    panel._send_reaction(_message(CHAT_A), "👍")
    worker, args = workers.pop()
    worker(*args)
    panel.open(CHAT_B)
    panel.open(CHAT_A)
    callback, args = callbacks.pop()
    callback(*args)
    assert panel._reaction_counts(MESSAGE_ID) == {"👍": 1}
    assert panel.messages_list.writes == [(0, "{'👍': 1}")]
    assert panel.button_updates == [0]
    assert panel.messages_list.freezes == 0


def test_phone_and_lid_alias_of_open_chat_can_update_the_view():
    panel = _Panel()
    phone, lid = "synthetic@s.whatsapp.net", "synthetic@lid"
    panel.main_window._phone_to_lid = {phone: lid}
    panel.conversation = {"remoteJid": lid}
    panel._on_own_reaction_sent(phone, _message(phone)["key"], "👍")
    assert panel._reaction_counts(MESSAGE_ID) == {"👍": 1}
    assert panel.button_updates == [0]


def test_failed_send_does_not_apply_or_persist_a_reaction(deferred):
    workers, callbacks = deferred
    panel = _Panel(success=False)
    panel._send_reaction(_message(CHAT_A), "👍")
    worker, args = workers.pop()
    worker(*args)
    assert callbacks == []
    assert panel._reaction_map == {}
    assert panel.persisted == []


def test_no_open_chat_does_not_start_a_worker(deferred):
    workers, callbacks = deferred
    panel = _Panel()
    panel.conversation = None
    panel._send_reaction(_message(CHAT_A), "👍")
    assert workers == []
    assert callbacks == []
