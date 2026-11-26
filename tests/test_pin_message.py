"""Tests for pinning/unpinning an individual message in a chat
(Ctrl+Shift+P / "Fixar mensagem" context menu item).

Unlike _on_menu_star (a local-only flag), pinning a message is WhatsApp's own
feature — visible to every other participant — so it goes through the
WPPConnect API (MainWindow.pin_message) via a background thread, applied
optimistically and rolled back if the server rejects it, mirroring how
conversation pin/unpin (_sync_pin_to_server) already behaves.

ConversationsPanel is a wx.Panel and cannot be instantiated without a running
wx.App, so the methods under test are exercised as plain functions against a
small stub — same approach as tests/test_message_bookmarks.py.
"""

import pytest

from ui.conversations import ConversationsPanel
from ui.conversation_panel.pinned_messages import PinnedMessagesMixin


class _FakeI18n:
    _STRINGS = {
        "pin_message": "Fixar mensagem",
        "pin_message_failed": "Não foi possível fixar esta mensagem.",
        "unpin_message_failed": "Não foi possível desafixar esta mensagem.",
        "system_event_action_unavailable": "Esta ação não está disponível para avisos do grupo.",
    }

    def t(self, key):
        return self._STRINGS[key]


class _FakeDB:
    def __init__(self):
        self.inserted = []

    def insert_message(self, jid, msg):
        self.inserted.append((jid, dict(msg)))


class _FakeMainWindow:
    def __init__(self, pin_result=True):
        self.i18n = _FakeI18n()
        self.db = _FakeDB()
        self.pin_result = pin_result
        self.pin_calls = []
        self.save_calls = 0
        self.announced = []

    def output(self, text):
        self.announced.append(text)

    def pin_message(self, jid, msg_key, pin):
        self.pin_calls.append((jid, dict(msg_key), pin))
        return self.pin_result

    def _schedule_save(self):
        self.save_calls += 1


class _Stub(PinnedMessagesMixin):
    _on_menu_pin_message = ConversationsPanel._on_menu_pin_message
    _on_pin_message_failed = ConversationsPanel._on_pin_message_failed
    _persist_message_local_flag = ConversationsPanel._persist_message_local_flag
    # _persist_message_local_flag delegates to the bulk form so both share
    # one code path (see its docstring) — the stub needs it bound too.
    _persist_message_local_flags = ConversationsPanel._persist_message_local_flags
    # _is_system_event is a staticmethod on the real class and attribute
    # access unwraps it, so it has to be re-wrapped here.
    _is_system_event = staticmethod(ConversationsPanel._is_system_event)
    _reject_system_event_action = ConversationsPanel._reject_system_event_action

    def __init__(self, main_window):
        self.main_window = main_window
        self.conversation = {"remoteJid": "5521999999999@s.whatsapp.net"}
        self.populate_calls = []
        self.repainted = []

    def populate_messages(self, preserve_focus=False):
        self.populate_calls.append(preserve_focus)

    def _repaint_or_repopulate(self, msg_ids):
        # The real one repaints the affected rows and only rebuilds the list
        # when it can't — see ConversationsPanel._repaint_message_rows().
        self.repainted.append(sorted(i for i in msg_ids if i))


def _msg(mid="a", pinned=False):
    m = {"key": {"id": mid, "fromMe": True}}
    if pinned:
        m["pinInChat"] = True
    return m


class _ImmediateThread:
    """threading.Thread stand-in that runs the target synchronously on
    .start(), so tests don't race a real background thread."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


@pytest.fixture(autouse=True)
def _synchronous_threads(monkeypatch):
    monkeypatch.setattr("ui.conversations.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("ui.conversations.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    monkeypatch.setattr("ui.conversations.wx.MessageBox", lambda *a, **kw: None)


class TestPinMessage:
    def test_pinning_an_unpinned_message_marks_it_optimistically(self):
        mw = _FakeMainWindow(pin_result=True)
        s = _Stub(mw)
        msg = _msg("a", pinned=False)

        s._on_menu_pin_message(msg)

        assert msg["pinInChat"] is True
        assert mw.save_calls >= 1
        assert s.repainted == [["a"]], "the message's own row should be re-rendered after the toggle"
        assert mw.pin_calls == [("5521999999999@s.whatsapp.net", {"id": "a", "fromMe": True}, True)]
        assert mw.db.inserted[0][1]["pinInChat"] is True, (
            "the optimistic pin must be written to the message's own DB row too — "
            "_schedule_save() alone only persists chat metadata, and "
            "navigate_to_conversation() reloads messages fresh from the DB on reopen"
        )

    def test_unpinning_a_pinned_message_toggles_the_other_way(self):
        mw = _FakeMainWindow(pin_result=True)
        s = _Stub(mw)
        msg = _msg("a", pinned=True)

        s._on_menu_pin_message(msg)

        assert msg["pinInChat"] is False
        assert mw.pin_calls == [("5521999999999@s.whatsapp.net", {"id": "a", "fromMe": True}, False)]

    def test_a_rejected_pin_is_rolled_back(self):
        """The server refusing the request must not leave the message stuck
        showing as pinned/unpinned locally when it never actually took effect."""
        mw = _FakeMainWindow(pin_result=False)
        s = _Stub(mw)
        msg = _msg("a", pinned=False)

        s._on_menu_pin_message(msg)

        assert msg["pinInChat"] is False, "rollback must undo the optimistic pin"
        assert mw.db.inserted[-1][1]["pinInChat"] is False, (
            "the rollback must overwrite the optimistically-persisted DB row too, "
            "or a reopened conversation would keep showing the rejected pin"
        )

    def test_a_group_notice_is_never_pinned(self):
        """Pinning is visible to every participant, so a group notice reaching
        it would broadcast a pin the server cannot honour. The accelerator
        (Ctrl+Shift+P) calls this handler directly, so the guard has to be
        here rather than on the context menu item."""
        mw = _FakeMainWindow(pin_result=True)
        s = _Stub(mw)
        msg = {"key": {"id": "a", "fromMe": False}, "messageType": "groupNotification"}

        s._on_menu_pin_message(msg)

        assert "pinInChat" not in msg
        assert mw.pin_calls == []
        assert mw.save_calls == 0
        assert mw.announced == [_FakeI18n._STRINGS["system_event_action_unavailable"]]

    def test_no_open_conversation_is_a_no_op(self):
        mw = _FakeMainWindow(pin_result=True)
        s = _Stub(mw)
        s.conversation = None
        msg = _msg("a")

        s._on_menu_pin_message(msg)

        assert "pinInChat" not in msg
        assert mw.pin_calls == []

    def test_a_raising_write_rolls_back_and_releases_the_pending_job(self, monkeypatch):
        mw = _FakeMainWindow()
        panel = _Stub(mw)
        def fail(*args):
            raise RuntimeError("write failed")
        monkeypatch.setattr(mw, "pin_message", fail)
        msg = _msg()
        panel._on_menu_pin_message(msg)
        assert msg["pinInChat"] is False
        assert panel._pinned_writes == set()

    def test_delayed_rollback_persists_in_the_original_chat_after_a_switch(self):
        mw = _FakeMainWindow()
        panel = _Stub(mw)
        old_jid = panel.conversation["remoteJid"]
        panel.conversation = {"remoteJid": "5511888888888@s.whatsapp.net"}
        msg = _msg(pinned=True)
        panel._on_pin_message_failed(msg, True, old_jid)
        assert msg["pinInChat"] is False
        assert mw.db.inserted[-1][0] == old_jid
        assert panel.repainted == []


@pytest.mark.parametrize("attempted_pin,title", [(True, "pin_message"), (False, "unpin_message")])
def test_the_failure_box_is_titled_after_what_was_tried(monkeypatch, attempted_pin, title):
    """A refused unpin used to be titled "Pin message", with its mnemonic."""
    shown = []
    monkeypatch.setattr("ui.conversations.wx.MessageBox", lambda *a, **kw: shown.append(a))
    mw = _FakeMainWindow()
    mw.i18n = type("I18n", (), {"t": staticmethod(lambda key: "&" + key)})()
    panel = _Stub(mw)
    panel._on_pin_message_failed(_msg(pinned=attempted_pin), attempted_pin,
                                 panel.conversation["remoteJid"])
    assert shown[0][1] == title
