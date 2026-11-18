"""The controls after the message list follow the focused row after a rebuild.

Reported: open another conversation while the previous one was on a message
with reactions (or a long text), Tab from the list, and "Reações" (or "Ler
mais") was still there, about a message of the other conversation. The rebuild
moves the focused row with no focus event of its own, and only "Retornar
ligação" was re-decided after it.

Unbound methods on a plain stub; no window opens.
"""

import inspect
from types import SimpleNamespace

from ui.conversations import ConversationsPanel
from ui.conversation_panel import list_refresh


class _Button:
    def __init__(self):
        self.shown = True
        self.label = ""

    def Show(self, show=True):
        self.shown = show

    def Hide(self):
        self.shown = False

    def SetLabel(self, label):
        self.label = label


class _Stub:
    _update_focused_row_controls = ConversationsPanel._update_focused_row_controls
    _update_reactions_button = ConversationsPanel._update_reactions_button
    _update_read_more_button = ConversationsPanel._update_read_more_button
    _is_separator = ConversationsPanel._is_separator

    def __init__(self, messages, reactions=None):
        self._sorted_messages = messages
        self._reactions = reactions or {}
        self._reactions_btn = _Button()
        self._read_more_btn = _Button()
        self._read_more_remainder = "stale"
        self._message_list_mode = "classic"
        self._LIST_CTRL_TEXT_LIMIT = 10
        self.main_window = SimpleNamespace(i18n=SimpleNamespace(t=lambda key: key), settings={})
        self.conversation_panel = SimpleNamespace(Layout=lambda: None)
        self.calls = []

    def _reaction_counts(self, msg_id):
        return self._reactions.get(msg_id, {})

    def _render_message_line(self, msg):
        return msg.get("text", "")

    def _update_return_call_button(self, idx):
        self.calls.append(("return_call", idx))

    def _update_inline_poll(self, idx):
        self.calls.append(("poll", idx))

    def _update_business_buttons(self, idx):
        self.calls.append(("business", idx))


def _msg(msg_id, text="hi"):
    return {"key": {"id": msg_id}, "messageType": "conversation", "text": text}


def test_the_new_conversations_row_takes_the_buttons_off():
    # The previous conversation's row had reactions and a long text...
    panel = _Stub([_msg("A", "x" * 40)], reactions={"A": {"👍": 1}})
    panel._update_focused_row_controls(0)
    assert panel._reactions_btn.shown and panel._read_more_btn.shown
    # ...and the conversation opened now has a short one with none.
    panel._sorted_messages = [_msg("B")]
    panel._update_focused_row_controls(0)
    assert not panel._reactions_btn.shown
    assert not panel._read_more_btn.shown and panel._read_more_remainder == ""
    assert ("return_call", 0) in panel.calls and ("poll", 0) in panel.calls


def test_an_empty_conversation_takes_them_off_too():
    panel = _Stub([])
    panel._update_focused_row_controls(-1)
    assert not panel._reactions_btn.shown and not panel._read_more_btn.shown


def test_the_rebuild_re_decides_all_of_them():
    source = inspect.getsource(list_refresh)
    body = source[source.index("    def populate_messages(self"):]
    assert "self._update_focused_row_controls(self.messages_list.GetFocusedItem())" in body
