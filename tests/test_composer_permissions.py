"""Tests for ConversationsPanel._apply_composer_permissions().

Opening a channel, or a group with "only admins can send messages" on while
the user is not an admin, disables the composer's send/record/attach buttons.
The emoji button was left out of that switch: it stayed clickable in a group
the user cannot post in, opening the picker and inserting text into a field
that had just been made read-only.

The switch also only ever ran when a conversation was *opened*, and
navigate_to_conversation() early-returns for the one already on screen — so a
group switched to announcement-only while the user sat in it kept a writable
message field, with nothing spoken. refresh_composer_permissions() is the
re-entry point for that, and its tests live here too.

What it says when it does is a third defect pinned here: it announced "Agora
somente admins podem enviar mensagens neste grupo" for every writable →
read-only move, including the first time a verdict ever landed for a group
that had been announcement-only for months. Nothing had changed there except
what WinZapp knew, so "agora" was simply false.

ConversationsPanel is a wx.Panel and cannot be instantiated without a running
wx.App, so the method under test is bound to a small stub carrying only the
widgets it touches — same approach as tests/test_conversation_name_update.py.
"""

import pytest

from ui.conversations import ConversationsPanel


class _FakeButton:
    def __init__(self):
        self.enabled = None

    def Enable(self):
        self.enabled = True

    def Disable(self):
        self.enabled = False


class _FakeTextField(_FakeButton):
    def __init__(self):
        super().__init__()
        self.editable = None

    def SetEditable(self, value):
        self.editable = value

    def IsEditable(self):
        return bool(self.editable)


class _FakeLabel:
    def __init__(self):
        self.label = None

    def SetLabel(self, text):
        self.label = text


class _FakePanel:
    def __init__(self):
        self.layouts = 0

    def Layout(self):
        self.layouts += 1


class _FakeI18n:
    def t(self, key):
        return key


class _FakeMainWindow:
    def __init__(self, restricted=(), chats=None):
        self._restricted = set(restricted)
        self.i18n = _FakeI18n()
        self.chats = chats if chats is not None else {}
        self.spoken = []

    def _is_group_send_restricted(self, chat):
        return chat.get("remoteJid", "") in self._restricted

    def output(self, text, interrupt=False):
        self.spoken.append(text)


class _Panel:
    _apply_composer_permissions = ConversationsPanel._apply_composer_permissions
    _message_label_text = ConversationsPanel._message_label_text
    refresh_composer_permissions = ConversationsPanel.refresh_composer_permissions

    def __init__(self, restricted=(), open_jid=None, chats=None):
        self.main_window = _FakeMainWindow(restricted, chats)
        self.message_field = _FakeTextField()
        self.send_message_btn = _FakeButton()
        self.record_voice_message_btn = _FakeButton()
        self._add_attachment_btn = _FakeButton()
        self._emoji_btn = _FakeButton()
        self.message_label = _FakeLabel()
        self.conversation_panel = _FakePanel()
        self.conversation = {"remoteJid": open_jid} if open_jid else None
        self.conversation_name = "Grupo de teste"


_GROUP = "123456-group@g.us"


class TestRestrictedGroup:
    def test_emoji_button_is_disabled_with_the_other_send_controls(self):
        panel = _Panel(restricted=[_GROUP])
        panel._apply_composer_permissions(_GROUP, {"remoteJid": _GROUP})
        assert panel._emoji_btn.enabled is False
        assert panel.send_message_btn.enabled is False
        assert panel.record_voice_message_btn.enabled is False
        assert panel._add_attachment_btn.enabled is False

    def test_message_field_stays_focusable_but_read_only(self):
        panel = _Panel(restricted=[_GROUP])
        panel._apply_composer_permissions(_GROUP, {"remoteJid": _GROUP})
        assert panel.message_field.enabled is True
        assert panel.message_field.editable is False


class TestChannel:
    def test_emoji_button_is_disabled(self):
        panel = _Panel()
        jid = "123456@newsletter"
        panel._apply_composer_permissions(jid, {"remoteJid": jid})
        assert panel._emoji_btn.enabled is False
        assert panel.message_field.enabled is False


class TestWritableConversations:
    def test_unrestricted_group_keeps_every_control_enabled(self):
        panel = _Panel()
        panel._apply_composer_permissions(_GROUP, {"remoteJid": _GROUP})
        assert panel._emoji_btn.enabled is True
        assert panel.send_message_btn.enabled is True
        assert panel.message_field.editable is True

    def test_private_chat_keeps_every_control_enabled(self):
        panel = _Panel(restricted=[_GROUP])
        jid = "5511988888888@s.whatsapp.net"
        panel._apply_composer_permissions(jid, {"remoteJid": jid})
        assert panel._emoji_btn.enabled is True
        assert panel.send_message_btn.enabled is True
        assert panel.message_field.editable is True

    def test_reopening_a_writable_chat_reenables_after_a_restricted_one(self):
        # The switch has to re-enable, not just skip disabling: the panel is
        # reused across conversations, so a disabled emoji button would stick.
        panel = _Panel(restricted=[_GROUP])
        panel._apply_composer_permissions(_GROUP, {"remoteJid": _GROUP})
        jid = "5511988888888@s.whatsapp.net"
        panel._apply_composer_permissions(jid, {"remoteJid": jid})
        assert panel._emoji_btn.enabled is True


class TestLiveReapply:
    """refresh_composer_permissions(): the announce flag flipping while the
    group is on screen.

    _apply_composer_permissions() only ever ran from
    navigate_to_conversation(), which early-returns for the conversation
    already open — so a group switched to "only admins can send messages"
    while the user sat in it kept a writable message field, and re-selecting
    the chat did not help either.
    """

    def _open_panel(self, restricted=(), jid=_GROUP):
        panel = _Panel(restricted=restricted, open_jid=jid,
                       chats={jid: {"remoteJid": jid}})
        panel._apply_composer_permissions(jid, {"remoteJid": jid})
        return panel

    def test_becoming_restricted_makes_the_field_read_only(self):
        panel = self._open_panel()
        assert panel.message_field.editable is True
        panel.main_window._restricted.add(_GROUP)
        panel.refresh_composer_permissions(_GROUP)
        assert panel.message_field.editable is False
        assert panel._emoji_btn.enabled is False
        assert panel.message_label.label == "group_admins_only"

    def test_the_transition_to_read_only_is_spoken(self):
        # A message field that silently stops accepting text is precisely the
        # reported failure — a screen-reader user gets no other signal.
        panel = self._open_panel()
        panel.main_window._restricted.add(_GROUP)
        panel.refresh_composer_permissions(_GROUP)
        assert panel.main_window.spoken == ["group_send_restricted_now"]

    def test_a_first_ever_verdict_is_announced_without_saying_now(self):
        # get_remote_chats() passes transition=False when it had no previous
        # verdict for the group: the composer was writable only because
        # nothing had answered yet, so the group did not just become
        # announcement-only — it may have been for months.
        panel = self._open_panel()
        panel.main_window._restricted.add(_GROUP)
        panel.refresh_composer_permissions(_GROUP, False)
        assert panel.message_field.editable is False
        assert panel.main_window.spoken == ["group_send_restricted"]

    def test_a_refresh_that_changes_nothing_says_nothing(self):
        panel = self._open_panel(restricted=[_GROUP])
        panel.refresh_composer_permissions(_GROUP)
        assert panel.message_field.editable is False
        assert panel.main_window.spoken == []

    def test_becoming_writable_again_reenables_without_announcing(self):
        panel = self._open_panel(restricted=[_GROUP])
        panel.main_window._restricted.discard(_GROUP)
        panel.refresh_composer_permissions(_GROUP)
        assert panel.message_field.editable is True
        assert panel.send_message_btn.enabled is True
        assert panel.main_window.spoken == []

    def test_another_groups_change_leaves_the_open_composer_alone(self):
        panel = self._open_panel()
        other = "999999-group@g.us"
        panel.main_window._restricted.add(other)
        panel.refresh_composer_permissions(other)
        assert panel.message_field.editable is True
        assert panel.conversation_panel.layouts == 0

    def test_no_open_conversation_is_a_no_op(self):
        panel = _Panel(restricted=[_GROUP])
        panel.refresh_composer_permissions(_GROUP)
        assert panel.message_field.editable is None
