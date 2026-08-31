"""Regression coverage for closing the open conversation when the user
navigates to a different top-level panel (e.g. Alt+5 / Status).

Reported live: leaving a conversation open while switching to the Status
tab kept sending typing/recording presence updates for it in the
background, with no way to see or stop them from Status. main.py's
on_alt_5() now calls ConversationsPanel.close_conversation_for_panel_switch()
first — the same cleanup close_conversation() (Esc) does, refactored into
_close_conversation_core() so the panel-switch path can skip
close_conversation()'s own focus-restoration side effects (which would
otherwise queue a CallAfter stealing focus back to the conversations list
a moment after Status sets its own focus).

ConversationsPanel is a wx.Panel and can't be instantiated without a
running wx.App, so the methods under test are bound onto a plain stub
carrying only the attributes they touch, matching the pattern used
throughout this test suite (see test_sender_names.py).
"""

from ui.conversations import ConversationsPanel


class _FakeWidget:
    def __init__(self, shown=False):
        self._shown = shown

    def Show(self, show=True):
        self._shown = bool(show)

    def Hide(self):
        self._shown = False

    def IsShown(self):
        return self._shown


class _FakeMainWindow:
    def __init__(self):
        self.typing_calls = []
        self.recording_calls = []

    def send_typing_status(self, jid, active, is_group):
        self.typing_calls.append((jid, active))

    def send_recording_status(self, jid, active, is_group):
        self.recording_calls.append((jid, active))

    def is_chat_archived(self, jid):
        return False


class _Stub:
    _close_conversation_core = ConversationsPanel._close_conversation_core
    close_conversation = ConversationsPanel.close_conversation
    close_conversation_for_panel_switch = ConversationsPanel.close_conversation_for_panel_switch
    _stop_typing_for_current_conversation = ConversationsPanel._stop_typing_for_current_conversation
    # Closing the conversation also drops the expanded history window, so the
    # next chat opens at the configured page size instead of inheriting this
    # one's thousands of rows.
    _reset_expanded_window = ConversationsPanel._reset_expanded_window

    def __init__(self, conversation):
        self.main_window = _FakeMainWindow()
        self.conversation = conversation
        self._last_open_jid = conversation.get("remoteJid", "") if conversation else ""
        self._is_typing = True
        self._is_recording = True
        self._editing_message_id = None
        self._quoted_message = None
        self._search_results = []
        self._search_result_idx = -1
        self._msg_bookmarks = {}
        self._msg_temp_bookmarks = {}
        self._expanded_visible_count = 0
        self._expanded_oldest_msg_id = ""
        self.conversation_panel = _FakeWidget(shown=True)
        self.message_field = _FakeWidget()
        self.restore_calls = []
        self.restore_archived_calls = []
        self._mention_panel_shown = False

    # Stand-ins for the various _hide_*() calls _close_conversation_core()
    # makes — not under test here, so no-ops.
    def _cancel_active_recording(self):
        pass

    def _hide_audio_controls(self):
        pass

    def _hide_all_media_controls(self):
        pass

    def _hide_attachment_panel(self):
        pass

    def _hide_media_transfer_gauge(self):
        pass

    def _restore_conversation_selection(self):
        self.restore_calls.append(True)

    def _restore_to_archived_list(self, jid):
        self.restore_archived_calls.append(jid)

    def Layout(self):
        pass


def _conv(jid="5511999999999@s.whatsapp.net"):
    return {"remoteJid": jid}


class TestCloseConversationForPanelSwitch:
    def test_stops_typing_and_recording(self):
        stub = _Stub(_conv())

        stub.close_conversation_for_panel_switch()

        assert stub.main_window.typing_calls == [("5511999999999@s.whatsapp.net", False)]
        assert stub.main_window.recording_calls == [("5511999999999@s.whatsapp.net", False)]

    def test_clears_and_hides_the_conversation(self):
        stub = _Stub(_conv())

        stub.close_conversation_for_panel_switch()

        assert stub.conversation is None
        assert stub.conversation_panel.IsShown() is False

    def test_does_not_queue_focus_restoration(self):
        # Unlike close_conversation() (Esc), the panel-switch variant must
        # not touch focus at all — the panel being switched TO sets its own
        # focus right after, and _restore_conversation_selection() would
        # otherwise steal it back moments later via wx.CallAfter.
        import wx

        calls = []
        original_call_after = wx.CallAfter
        wx.CallAfter = lambda *a, **kw: calls.append((a, kw))
        try:
            stub = _Stub(_conv())
            stub.close_conversation_for_panel_switch()
        finally:
            wx.CallAfter = original_call_after

        assert calls == []


class TestBookmarkLifetimeOnClose:
    """The two bookmark sets differ precisely here: the ten permanent ones
    (Ctrl+0..9) span conversations and must survive closing one, while the
    temporary ones (Alt+Shift+0..9) exist only for the open conversation."""

    def test_temporary_bookmarks_are_dropped(self):
        stub = _Stub(_conv())
        stub._msg_temp_bookmarks = {1: "MSG-A", 7: "MSG-B"}

        stub.close_conversation_for_panel_switch()

        assert stub._msg_temp_bookmarks == {}

    def test_permanent_bookmarks_survive(self):
        stub = _Stub(_conv())
        stub._msg_bookmarks = {1: ("5511999999999@s.whatsapp.net", "MSG-A")}

        stub.close_conversation_for_panel_switch()

        assert stub._msg_bookmarks == {1: ("5511999999999@s.whatsapp.net", "MSG-A")}


class TestExpandedHistoryWindowOnClose:
    """History the user loaded with Home widens the message list past
    messages_page_size for as long as that conversation is open. Closing it has
    to drop that, or the next conversation opens rendering the previous one's
    thousands of rows."""

    def test_closing_resets_the_expanded_window(self):
        stub = _Stub(_conv())
        stub._expanded_visible_count = 4200
        stub._expanded_oldest_msg_id = "MSG-OLD"

        stub.close_conversation_for_panel_switch()

        assert stub._expanded_visible_count == 0
        assert stub._expanded_oldest_msg_id == ""


class TestCloseConversationStillRestoresFocus:
    """close_conversation() (Esc) itself must keep doing what it always
    did — only the new panel-switch variant skips this."""

    def test_queues_restore_conversation_selection(self):
        import wx

        calls = []
        original_call_after = wx.CallAfter
        wx.CallAfter = lambda fn, *a, **kw: calls.append(fn)
        try:
            stub = _Stub(_conv())
            stub.close_conversation(None)
        finally:
            wx.CallAfter = original_call_after

        assert stub._restore_conversation_selection in calls
