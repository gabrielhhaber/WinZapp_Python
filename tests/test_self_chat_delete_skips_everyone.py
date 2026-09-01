"""Issue #73: "Delete for everyone" in the self-chat ("Me — messages to
yourself") only ever removed the message locally — WhatsApp's revoke is a
no-op there (there's no one else to delete it for), so the message stayed
on every other linked device and reappeared in WinZapp itself after the
next resync, while the app had told the user it was gone for everyone.

_on_menu_delete_message() detects the self-chat up front and skips the
whole "delete for me / for everyone" dialog entirely, going straight to a
plain local delete (delete_message_for_me) — matching the reporter's own
suggested fix.

Issue #95: that self-chat path used to fire with zero confirmation at all.
It now shows a plain Delete/Cancel prompt (_confirm_local_only_delete) —
no scope choice, since "for everyone" was never real here, just a yes/no on
the delete itself — before actually deleting.

ConversationsPanel is a wx.Panel and can't be instantiated without a
running wx.App, so wx.MessageDialog (the confirmation) is faked below
rather than exercised for real — unlike the non-self-chat path, which is
left untested here since it builds a real modal dialog.
"""

import threading

import wx

from ui.conversations import ConversationsPanel


class _FakeI18n:
    def t(self, key):
        return key


class _FakeMainWindow:
    def __init__(self, is_self_chat):
        self._is_self_chat = is_self_chat
        self.delete_for_me_calls = []
        self.delete_for_everyone_calls = []
        self.i18n = _FakeI18n()

    def _is_self_jid(self, jid):
        return self._is_self_chat

    def delete_message_for_me(self, jid, key):
        self.delete_for_me_calls.append((jid, key))
        return True

    def delete_message_for_everyone(self, jid, key):
        self.delete_for_everyone_calls.append((jid, key))
        return True


class _Stub:
    _on_menu_delete_message      = ConversationsPanel._on_menu_delete_message
    _delete_message_for_me_only  = ConversationsPanel._delete_message_for_me_only
    _confirm_local_only_delete   = ConversationsPanel._confirm_local_only_delete
    _is_separator                = ConversationsPanel._is_separator
    _is_system_event             = lambda self, msg: False

    def __init__(self, jid, is_self_chat):
        self.main_window = _FakeMainWindow(is_self_chat)
        msg = {"key": {"id": "m1", "fromMe": True, "remoteJid": jid}}
        self._sorted_messages = [msg]
        self.conversation = {"remoteJid": jid}
        self.removed_ids = []

    def remove_messages_by_id(self, ids, focus_previous=False):
        self.removed_ids.append(set(ids))


SELF_JID = "5511999999999@s.whatsapp.net"


def _run_and_join_threads(fn):
    """_delete_message_for_me_only()/the "everyone" path fire a background
    daemon thread — join whatever's alive afterward so assertions don't race
    it."""
    before = set(threading.enumerate())
    fn()
    for t in set(threading.enumerate()) - before:
        t.join(timeout=2)


class TestSelfChatSkipsTheScopeDialog:
    def test_self_chat_deletes_locally_only_after_a_plain_confirm(self, monkeypatch):
        monkeypatch.setattr(wx, "MessageDialog",
                             lambda *a, **k: _FakeMessageDialog(wx.ID_OK))
        stub = _Stub(SELF_JID, is_self_chat=True)

        _run_and_join_threads(lambda: stub._on_menu_delete_message(0))

        assert stub.removed_ids == [{"m1"}]
        assert len(stub.main_window.delete_for_me_calls) == 1
        assert stub.main_window.delete_for_everyone_calls == []

    def test_declining_the_confirmation_deletes_nothing(self, monkeypatch):
        monkeypatch.setattr(wx, "MessageDialog",
                             lambda *a, **k: _FakeMessageDialog(wx.ID_CANCEL))
        stub = _Stub(SELF_JID, is_self_chat=True)

        _run_and_join_threads(lambda: stub._on_menu_delete_message(0))

        assert stub.removed_ids == []
        assert stub.main_window.delete_for_me_calls == []
        assert stub.main_window.delete_for_everyone_calls == []

    def test_confirmation_offers_no_scope_choice(self, monkeypatch):
        """No radios, no "for everyone" option — just Delete/Cancel."""
        captured = {}

        class _CapturingDialog(_FakeMessageDialog):
            def __init__(self, parent, message, caption, style):
                super().__init__(wx.ID_OK)
                captured["message"] = message
                captured["caption"] = caption
                captured["style"] = style

            def SetOKCancelLabels(self, ok, cancel):
                captured["labels"] = (ok, cancel)

        monkeypatch.setattr(wx, "MessageDialog", _CapturingDialog)
        stub = _Stub(SELF_JID, is_self_chat=True)

        _run_and_join_threads(lambda: stub._on_menu_delete_message(0))

        assert captured["message"] == "delete_msg_confirm"
        assert captured["caption"] == "delete_message"
        assert captured["labels"] == ("delete_msg_confirm_yes", "cancel")
        # Escape has to dismiss it — wxMSW only allows the native dialog to be
        # cancelled when wx.CANCEL is in the style — and the destructive
        # button must not be the default, since this prompt is one keystroke
        # away from a focused message.
        assert captured["style"] & wx.CANCEL
        assert captured["style"] & wx.CANCEL_DEFAULT


class _FakeMessageDialog:
    def __init__(self, result):
        self._result = result

    def SetOKCancelLabels(self, ok, cancel):
        pass

    def ShowModal(self):
        return self._result

    def Destroy(self):
        pass
