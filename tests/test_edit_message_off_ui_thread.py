"""Regression test: editing a message froze the UI for a second or two.

ConversationsPanel's edit path called MainWindow.edit_message() inline, on the
wx main thread. That method POSTs to WPPConnect's /edit-message, which drives
Puppeteer/WhatsApp Web and routinely takes a second or two to answer (its own
timeout is 15s) — so the whole window stopped responding for the length of the
request every single time a sent message was edited. It was the last
server-backed message action still doing that: sending goes through
MessageQueue's worker, pinning and deleting each start their own thread.

The fix runs the server call on a worker and applies the local update
immediately, the same optimistic shape _on_menu_pin_message() uses.

ConversationsPanel is a wx.Panel and cannot be instantiated without a running
wx.App, so _apply_message_edit() is bound to a small stub — same approach as
tests/test_message_bookmarks.py.
"""

import pytest

from ui.conversations import ConversationsPanel


class _FakeList:
    def __init__(self, focused=-1):
        self._focused = focused
        self.texts = {}

    def SetItemText(self, idx, text):
        self.texts[idx] = text

    def GetFocusedItem(self):
        return self._focused


class _FakeMainWindow:
    def __init__(self):
        self.edit_calls = []
        self.saves = []
        self.set_chats_calls = 0

    def edit_message(self, remote_jid, message_id, new_text, mentioned_jids=None):
        self.edit_calls.append((remote_jid, message_id, new_text, mentioned_jids))

    def _schedule_save(self, dirty_jid=None):
        self.saves.append(dirty_jid)

    def _schedule_set_chats(self):
        self.set_chats_calls += 1


class _Panel:
    _apply_message_edit = ConversationsPanel._apply_message_edit
    _send_message_edit = ConversationsPanel._send_message_edit
    _message_own_links = ConversationsPanel._message_own_links

    def __init__(self, messages=(), editing_id="m1", mentions=None, focused=-1):
        self.main_window = _FakeMainWindow()
        self._sorted_messages = list(messages)
        self.messages_list = _FakeList(focused=focused)
        self._editing_message_id = editing_id
        self._mentions = mentions
        self.cancel_calls = 0
        self.links_panels = []
        self.mentions_panels = []

    def _build_mention_payload(self, text):
        return (text, self._mentions)

    def _render_message_line(self, msg, index=None, total=None, include_quoted_preview=True, **_kw):
        body = msg.get("message", {})
        return body.get("conversation") or body.get("extendedTextMessage", {}).get("text", "")

    def _on_cancel_edit(self):
        self.cancel_calls += 1

    def _extract_links(self, line):
        return []

    def _extract_mentions(self, msg):
        return []

    def _update_links_panel(self, links):
        self.links_panels.append(links)

    def _update_mentions_panel(self, mentions):
        self.mentions_panels.append(mentions)


class _CapturedThread:
    """threading.Thread stand-in that records the call instead of running it,
    so a test can tell the work was handed to a worker at all."""

    created = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self.daemon_flag = daemon
        _CapturedThread.created.append(self)

    def start(self):
        pass

    def run_it(self):
        self._target(*self._args, **self._kwargs)


@pytest.fixture(autouse=True)
def _captured_threads(monkeypatch):
    _CapturedThread.created = []
    monkeypatch.setattr("ui.conversations.threading.Thread", _CapturedThread)
    yield
    _CapturedThread.created = []


def _msg(mid="m1", text="antes"):
    return {
        "key": {"id": mid, "fromMe": True},
        "messageType": "conversation",
        "message": {"conversation": text},
    }


class TestServerCallIsOffTheUiThread:
    def test_the_edit_request_is_handed_to_a_worker(self):
        panel = _Panel([_msg()])

        panel._apply_message_edit("depois", "grupo@g.us")

        assert panel.main_window.edit_calls == [], (
            "edit_message() must not run inline — it blocks the UI thread for "
            "as long as WhatsApp Web takes to answer"
        )
        assert len(_CapturedThread.created) == 1
        assert _CapturedThread.created[0].daemon_flag is True

    def test_the_worker_sends_exactly_what_the_edit_asked_for(self):
        panel = _Panel([_msg()], mentions=["5511999999999@s.whatsapp.net"])

        panel._apply_message_edit("depois", "grupo@g.us")
        _CapturedThread.created[0].run_it()

        assert panel.main_window.edit_calls == [
            ("grupo@g.us", "m1", "depois", ["5511999999999@s.whatsapp.net"])
        ]

    def test_the_row_is_updated_without_waiting_for_the_server(self):
        panel = _Panel([_msg()])

        panel._apply_message_edit("depois", "grupo@g.us")

        # Nothing ran the worker, yet the local state is already applied.
        assert panel._sorted_messages[0]["message"] == {"conversation": "depois"}
        assert panel._sorted_messages[0]["_edited"] is True
        assert panel.messages_list.texts == {0: "depois"}
        assert panel.main_window.saves == ["grupo@g.us"]
        assert panel.main_window.set_chats_calls == 1
        assert panel.cancel_calls == 1


class TestLocalUpdate:
    def test_the_message_is_located_by_id_not_by_row_index(self):
        """A background sync can rebuild _sorted_messages while the user is
        typing, so the index captured when edit mode was entered may by then
        point at an unrelated row."""
        panel = _Panel([_msg("other", "outra"), _msg("m1", "antes")])

        panel._apply_message_edit("depois", "grupo@g.us")

        assert panel._sorted_messages[0]["message"] == {"conversation": "outra"}
        assert panel._sorted_messages[1]["message"] == {"conversation": "depois"}

    def test_an_edit_that_adds_mentions_keeps_the_extended_shape(self):
        panel = _Panel([_msg()], mentions=["5511999999999@s.whatsapp.net"])

        panel._apply_message_edit("oi @5511999999999", "grupo@g.us")

        edited = panel._sorted_messages[0]
        assert edited["messageType"] == "extendedTextMessage"
        assert edited["contextInfo"]["mentionedJid"] == ["5511999999999@s.whatsapp.net"]

    def test_an_edit_that_removes_every_mention_clears_the_stale_list(self):
        msg = _msg()
        msg["contextInfo"] = {"mentionedJid": ["5511999999999@s.whatsapp.net"]}
        panel = _Panel([msg], mentions=None)

        panel._apply_message_edit("sem mencao", "grupo@g.us")

        assert "mentionedJid" not in panel._sorted_messages[0]["contextInfo"]

    def test_a_message_no_longer_in_the_list_still_leaves_edit_mode(self):
        """The server call already went out addressed by id; the local half
        just has nothing to update. Staying stuck in edit mode would be worse."""
        panel = _Panel([_msg("other")])

        panel._apply_message_edit("depois", "grupo@g.us")

        assert len(_CapturedThread.created) == 1
        assert panel.main_window.saves == []
        assert panel.cancel_calls == 1

    def test_the_side_panels_refresh_only_for_the_focused_row(self):
        panel = _Panel([_msg()], focused=0)
        panel._apply_message_edit("depois", "grupo@g.us")
        assert panel.links_panels and panel.mentions_panels

        other = _Panel([_msg()], focused=5)
        other._apply_message_edit("depois", "grupo@g.us")
        assert other.links_panels == [] and other.mentions_panels == []
