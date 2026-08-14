import os
import sys
import wx


class AccessibleSearchInConversation(wx.Accessible):
    """Reports Ctrl+Shift+F as the keyboard shortcut for the search-in-conversation button."""

    def GetKeyboardShortcut(self, childId):
        return (wx.ACC_OK, "Ctrl+Shift+F")


class AccessibleSearchNextResult(wx.Accessible):
    """Reports Enter as the keyboard shortcut for the next-result button."""

    def GetKeyboardShortcut(self, childId):
        return (wx.ACC_OK, "Enter")


class AccessibleSearchPrevResult(wx.Accessible):
    """Reports Shift+Enter as the keyboard shortcut for the previous-result button."""

    def GetKeyboardShortcut(self, childId):
        return (wx.ACC_OK, "Shift+Enter")


class AccessibleStatusPrev(wx.Accessible):
    """Reports the keyboard shortcut for the previous-status button.

    Takes the announced string as a constructor argument (i18n's
    "accessible_ctrl_left") instead of a hardcoded "Ctrl+Left" — NVDA reads
    whatever GetKeyboardShortcut() returns completely verbatim, with no
    localization of its own, so a literal English string here was announced
    to every user regardless of the app's configured language.
    """

    def __init__(self, shortcut="Ctrl+Left"):
        super().__init__()
        self.shortcut = shortcut

    def GetKeyboardShortcut(self, childId):
        return (wx.ACC_OK, self.shortcut)


class AccessibleStatusNext(wx.Accessible):
    """Reports the keyboard shortcut for the next-status button.

    See AccessibleStatusPrev's docstring — same reasoning.
    """

    def __init__(self, shortcut="Ctrl+Right"):
        super().__init__()
        self.shortcut = shortcut

    def GetKeyboardShortcut(self, childId):
        return (wx.ACC_OK, self.shortcut)


class AccessibleSearchConversations(wx.Accessible):
    def __init__(self, shortcut):
        super().__init__()
        self.shortcut = shortcut

    def GetKeyboardShortcut(self, childId):
        return (wx.ACC_OK, self.shortcut)


class AccessibleRecordVoiceMessage(wx.Accessible):
    def __init__(self, shortcut):
        super().__init__()
        self.shortcut = shortcut

    def GetKeyboardShortcut(self, childId):
        return (wx.ACC_OK, self.shortcut)


class AccessibleSaveAs(wx.Accessible):
    """Reports Ctrl+Shift+S as the keyboard shortcut for the Save-As button."""

    def GetKeyboardShortcut(self, childId):
        return (wx.ACC_OK, "Ctrl+Shift+S")


class AccessibleStatusCopyText(wx.Accessible):
    """Reports Ctrl+C as the keyboard shortcut for the status copy-text button."""

    def GetKeyboardShortcut(self, childId):
        return (wx.ACC_OK, "Ctrl+C")


class AccessibleReadMoreButton(wx.Accessible):
    """Reports Alt+L as the keyboard shortcut for the Read-more button."""

    def GetKeyboardShortcut(self, childId):
        return (wx.ACC_OK, "Alt+L")


class AccessibleConversationDataButton(wx.Accessible):
    """Reports Ctrl+Shift+D as the keyboard shortcut for the conversation-data button."""

    def GetKeyboardShortcut(self, childId):
        return (wx.ACC_OK, "Ctrl+Shift+D")


class AccessibleAddAttachmentButton(wx.Accessible):
    """Reports Ctrl+Shift+A as the keyboard shortcut for the Add Attachment button."""

    def GetKeyboardShortcut(self, childId):
        return (wx.ACC_OK, "Ctrl+Shift+A")


class AccessibleDiscardVoiceMessage(wx.Accessible):
    """Reports Ctrl+Shift+D as the keyboard shortcut for the Discard button."""

    def GetKeyboardShortcut(self, childId):
        return (wx.ACC_OK, "Ctrl+Shift+D")


class AccessiblePauseResumeRecording(wx.Accessible):
    """Reports Ctrl+Shift+P as the keyboard shortcut for the Pause/Resume button."""

    def GetKeyboardShortcut(self, childId):
        return (wx.ACC_OK, "Ctrl+Shift+P")


class AccessiblePlayVoiceMessage(wx.Accessible):
    """Accessibility for the Play Voice Message button."""

    pass


class AccessibleSendVoiceMessage(wx.Accessible):
    """Reports Ctrl+R as the keyboard shortcut for the Send Voice Message button."""

    def GetKeyboardShortcut(self, childId):
        return (wx.ACC_OK, "Ctrl+R")


class AccessibleNewConversationButton(wx.Accessible):
    """Reports Ctrl+N as the keyboard shortcut for the New Conversation button."""

    def GetKeyboardShortcut(self, childId):
        return (wx.ACC_OK, "Ctrl+N")


class AccessibleMessagesList(wx.Accessible):
    """
    Custom accessible for the conversation messages ListCtrl.

    The native Win32 ListView control truncates each item's text to ~512
    characters, both visually and in the MSAA name exposed to screen readers.
    Long messages (e.g. a paragraph ending in a URL) therefore got cut off and
    could only be read in full through the Alt+C popup.  This accessible returns
    the complete, untruncated rendered text for each row so the screen reader
    always announces the whole message.
    """

    def __init__(self, conversations_panel):
        super().__init__()
        self._panel = conversations_panel

    def GetName(self, childId):
        # childId 0 is the control itself; rows are 1-based.
        # We return an empty string for items (childId > 0) to prevent the native OS
        # MSAA list proxy from announcing the truncated text.
        # This completely avoids speech duplication and double entries in NVDA history,
        # allowing our debounced self.main_window.output() to cleanly announce the full message.
        if childId == 0:
            return (wx.ACC_NOT_IMPLEMENTED, "")
        return (wx.ACC_OK, "")


class AccessibleMessagesListControl(wx.Accessible):
    """
    Reports a simple, fixed name (e.g. "Mensagens") for the conversation
    messages list control itself (childId 0) when it receives focus via
    Tab/Shift+Tab.

    Without this, NVDA falls back to a generic, redundant description built
    from the native control's window class and item count — e.g. "List Box
    200 itens" — instead of announcing just the field label. Applies to both
    the classic wx.ListCtrl and CompatListBoxMessagesCtrl so the announcement
    is identical regardless of which one is configured.

    Per-row announcements (childId > 0) are left untouched (ACC_NOT_IMPLEMENTED)
    so the screen reader keeps reading each message's content normally.
    """

    def __init__(self, label):
        super().__init__()
        self._label = label

    def GetName(self, childId):
        if childId == 0:
            return (wx.ACC_OK, self._label)
        return (wx.ACC_NOT_IMPLEMENTED, "")

    def GetRole(self, childId):
        if childId == 0:
            return (wx.ACC_OK, wx.ROLE_SYSTEM_LIST)
        return (wx.ACC_NOT_IMPLEMENTED, 0)


class AccessibleAudioSlider(wx.Accessible):
    def __init__(self, conversations_panel):
        super().__init__()
        self._panel = conversations_panel

    def GetName(self, childId):
        panel = self._panel
        i18n = panel.main_window.i18n
        if panel._audio_stream is not None and panel._audio_stream_duration > 0:
            try:
                pos = panel._audio_stream.get_position()
                total = panel._audio_stream.get_length()
                current_secs = int(pos / total * panel._audio_stream_duration) if total > 0 else 0
            except Exception:
                current_secs = 0
            current_str = panel._format_duration(current_secs)
            total_str = panel._format_duration(panel._audio_stream_duration)
            return (wx.ACC_OK, f"{current_str} {i18n.t('of')} {total_str}")
        return (wx.ACC_OK, "")


class MockListEvent:
    def __init__(self, index):
        self._index = index
    def GetIndex(self):
        return self._index
    def Skip(self):
        pass


class CompatListBoxMessagesCtrl(wx.ListBox):
    """
    Subclass of wx.ListBox that mimics the wx.ListCtrl API used elsewhere in
    the messages list code.

    wx.dataview.DataViewListCtrl (the previous alternative to the classic
    wx.ListCtrl) turned out to be a compound control whose generic backend
    is not natively screen-reader accessible on Windows — NVDA read only the
    raw "wxdataviewctrlmainwindow" window class, announced no label, and
    arrow-key navigation produced nothing at all. Plain wx.ListBox wraps a
    single native Win32 LISTBOX control (not the SysListView32 used by
    wx.ListCtrl), which is fully MSAA-accessible out of the box and — unlike
    SysListView32 — does not truncate item text at ~512 characters.
    """
    def __init__(self, parent, style=0):
        super().__init__(parent, style=wx.LB_SINGLE)
        self._activated_handler = None
        self._key_down_handler = None
        # wx.ListBox has no built-in "activate" notification for Enter (only
        # EVT_LISTBOX_DCLICK, which is mouse-double-click only). A plain
        # EVT_KEY_DOWN binding is not reliable for Enter specifically:
        # Windows' dialog/panel keyboard navigation can claim WXK_RETURN
        # before it ever becomes a normal key event for a control that,
        # unlike wx.ListCtrl, has no native "activate" concept of its own —
        # this is exactly why Enter did nothing here. EVT_CHAR_HOOK
        # intercepts before that navigation processing, so bind it here
        # unconditionally instead of trying to fold Enter into EVT_KEY_DOWN.
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)

    def set_key_down_handler(self, handler):
        self._key_down_handler = handler

    def _on_char_hook(self, event):
        if self.HasFocus():
            if event.GetKeyCode() == wx.WXK_RETURN:
                row = self.GetSelection()
                if row != wx.NOT_FOUND and self._activated_handler is not None:
                    self._activated_handler(MockListEvent(row))
                    return
            elif self._key_down_handler is not None:
                self._key_down_handler(event)
                if not event.GetSkipped():
                    return
        event.Skip()

    def InsertColumn(self, col, heading, format=0, width=-1):
        pass  # wx.ListBox has no columns — single text per row.

    def SetColumn(self, col, listItem):
        pass  # wx.ListBox has no columns.

    def GetItemCount(self):
        return self.GetCount()

    def DeleteAllItems(self):
        self.Clear()

    def Focus(self, row):
        if 0 <= row < self.GetCount() and self.GetSelection() != row:
            self.SetSelection(row)

    def Select(self, row, select=True):
        if select:
            if 0 <= row < self.GetCount() and self.GetSelection() != row:
                self.SetSelection(row)
        else:
            self.Deselect(row)

    def EnsureVisible(self, row):
        if 0 <= row < self.GetCount():
            super().EnsureVisible(row)

    def SetItemText(self, row, col_or_text, text=None):
        if text is None:
            self.SetString(row, col_or_text)
        else:
            self.SetString(row, text)

    def GetItemText(self, row, col=0):
        return self.GetString(row)

    def Append(self, entry_tuple):
        super().Append(entry_tuple[0])

    def InsertItem(self, pos, text):
        """Insert at pos, preserving the selected row and OS-level focus so
        a live message arriving mid-navigation doesn't jump the cursor."""
        selected  = self.GetSelection()
        had_focus = self.HasFocus()
        self.Insert(text, pos)
        if selected != wx.NOT_FOUND:
            restored = selected + 1 if pos <= selected else selected
            self.SetSelection(restored)
        if had_focus:
            self.SetFocus()
        return pos

    def DeleteItem(self, row):
        """Delete row, preserving the selected row and OS-level focus so
        dismissing the unread separator doesn't jump the cursor."""
        count = self.GetCount()
        if row < 0 or row >= count:
            return
        selected  = self.GetSelection()
        had_focus = self.HasFocus()
        self.Delete(row)
        if selected != wx.NOT_FOUND and self.GetCount() > 0:
            if selected > row:
                restored = selected - 1
            elif selected == row:
                restored = max(0, row - 1)
            else:
                restored = selected
            self.SetSelection(restored)
        if had_focus:
            self.SetFocus()

    def GetFocusedItem(self):
        return self.GetSelection()

    def GetFirstSelected(self):
        return self.GetSelection()

    def Bind(self, event_type, handler, *args, **kwargs):
        if event_type == wx.EVT_LIST_ITEM_ACTIVATED:
            # wx.ListBox has no Enter-to-activate notification of its own
            # (EVT_LISTBOX_DCLICK is mouse-double-click only) — _on_char_hook
            # invokes this same handler on Enter (see __init__).
            self._activated_handler = handler
            def _on_dclick(evt):
                row = self.GetSelection()
                if row != wx.NOT_FOUND:
                    handler(MockListEvent(row))
            super().Bind(wx.EVT_LISTBOX_DCLICK, _on_dclick, *args, **kwargs)

        elif event_type in (wx.EVT_LIST_ITEM_SELECTED, wx.EVT_LIST_ITEM_FOCUSED):
            def _on_selected(evt):
                row = self.GetSelection()
                if row != wx.NOT_FOUND:
                    handler(MockListEvent(row))
                evt.Skip()
            super().Bind(wx.EVT_LISTBOX, _on_selected, *args, **kwargs)

        else:
            super().Bind(event_type, handler, *args, **kwargs)
