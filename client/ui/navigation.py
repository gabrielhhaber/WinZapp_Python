import os
import sys
import wx
from traceback import format_exc
from core.sound_system import SoundSystem
from ui.conversations import ConversationsPanel


class NavigationPanel(wx.Panel):
    def __init__(self, main_window, parent):
        super().__init__(parent)

        self.main_window = main_window
        self.parent = parent

        self.init_UI()

    def init_UI(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.nav_label = wx.StaticText(self, label=self.main_window.i18n.t("main_nav"))
        sizer.Add(self.nav_label, 0, wx.LEFT | wx.TOP, 5)

        self.nav_list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.nav_list.InsertColumn(0, self.main_window.i18n.t("main_nav"), width=180)

        self._nav_keys = []
        self.rebuild_items()

        self.nav_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_nav_item_selected)
        self.nav_list.Bind(wx.EVT_KEY_DOWN, self._on_nav_key_down)
        self.nav_list.Focus(0)
        self.nav_list.Select(0)
        sizer.Add(self.nav_list, 1, wx.EXPAND | wx.ALL, 5)

        self.SetSizer(sizer)

    def refresh_labels(self):
        """Update all translatable labels after a language change."""
        i18n = self.main_window.i18n
        self.nav_label.SetLabel(i18n.t("main_nav"))
        col = wx.ListItem()
        col.SetText(i18n.t("main_nav"))
        self.nav_list.SetColumn(0, col)
        self.rebuild_items()

    def rebuild_items(self):
        """Rebuild the small nav list when the optional vault row changes."""
        previous_key = None
        focused = self.nav_list.GetFocusedItem()
        if 0 <= focused < len(getattr(self, "_nav_keys", [])):
            previous_key = self._nav_keys[focused]

        keys = ["conversations", "archived"]
        if getattr(
            self.main_window, "chat_lock_navigation_visible", lambda: False
        )():
            keys.append("locked")
        keys.extend(["status", "calls", "settings"])
        i18n = self.main_window.i18n
        labels = {
            "conversations": f"{i18n.t('conversations')} alt+1",
            "archived": i18n.t("archived_chats_nav"),
            "locked": i18n.t("locked_chats_nav"),
            "status": i18n.t("status_nav"),
            "calls": i18n.t("calls_nav"),
            "settings": f"{i18n.t('settings')} {i18n.t('settings_shortcut')}",
        }
        self.nav_list.Freeze()
        try:
            self.nav_list.DeleteAllItems()
            for key in keys:
                self.nav_list.Append((labels[key],))
        finally:
            self.nav_list.Thaw()
        self._nav_keys = keys
        self.refresh_archived_label()
        target = keys.index(previous_key) if previous_key in keys else 0
        if keys:
            self.nav_list.Focus(target)
            self.nav_list.Select(target)

    def refresh_archived_label(self):
        """Update the "Conversas arquivadas" nav item to reflect how many
        archived conversations currently have unread messages.

        Archived chats are deliberately excluded from the window-title
        unread count (see MainWindow._update_title()) — this is where that
        count surfaces instead, so it is not simply hidden information.
        """
        if "archived" not in getattr(self, "_nav_keys", []):
            return
        i18n = self.main_window.i18n
        count = self.main_window.get_archived_unread_count()
        if count <= 0:
            label = i18n.t("archived_chats_nav")
        elif count == 1:
            label = i18n.t("archived_chats_nav_unread_singular")
        else:
            label = i18n.t("archived_chats_nav_unread_plural").format(count=count)
        self.nav_list.SetItemText(self._nav_keys.index("archived"), label)

    def _on_nav_key_down(self, event):
        if event.GetKeyCode() == wx.WXK_SPACE:
            idx = self.nav_list.GetFocusedItem()
            if idx >= 0:
                self.nav_list.Select(idx)
                class _E:
                    def GetIndex(self): return idx
                self.on_nav_item_selected(_E())
        else:
            event.Skip()

    def on_nav_item_selected(self, event):
        index = event.GetIndex()
        mw = self.main_window
        if index < 0 or index >= len(self._nav_keys):
            return
        key = self._nav_keys[index]

        if key == "settings":
            mw.open_settings()
            return

        if key == "locked":
            mw.show_locked_chats_panel()
            return

        lock_vault = getattr(mw, "lock_chat_vault", None)
        if lock_vault is not None:
            lock_vault(silent=True, show_conversations=False)

        # Hide all content panels, show the right one
        mw.conversations_panel.Hide()
        if hasattr(mw, "status_panel"):
            mw.status_panel.Hide()
        if hasattr(mw, "archived_conversations_panel"):
            mw.archived_conversations_panel.Hide()
        if hasattr(mw, "locked_conversations_panel"):
            mw.locked_conversations_panel.Hide()
        if hasattr(mw, "calls_panel"):
            mw.calls_panel.Hide()

        if key == "conversations":
            mw.conversations_panel.Show()
            # Safety net: if an archived chat's detail pane is still open,
            # its conversations_list/label were hidden by
            # ArchivedConversationsPanel.on_conversation_selected() and never
            # restored (the user got here via this nav item instead of Esc).
            mw.conversations_panel.conversations_label.Show()
            mw.conversations_panel.conversations_list.Show()
            mw.content_panel.Layout()
            mw.conversations_panel.conversations_list.SetFocus()
            if (mw.conversations_panel.conversations_list.GetFocusedItem() != -1
                    and mw.conversations_panel.conversations_list.GetItemCount() > 0):
                mw.output(
                    mw.conversations_panel.conversations_list.GetItemText(
                        mw.conversations_panel.conversations_list.GetFocusedItem()
                    ),
                    interrupt=True,
                )
        elif key == "archived" and hasattr(mw, "archived_conversations_panel"):
            mw.archived_conversations_panel.Show()
            mw.content_panel.Layout()
            mw.archived_conversations_panel.restore_selection()
        elif key == "status" and hasattr(mw, "status_panel"):
            mw.status_panel.Show()
            mw.content_panel.Layout()
            mw.status_panel._add_status_btn.SetFocus()
            mw.status_panel.on_show()
        elif key == "calls" and hasattr(mw, "calls_panel"):
            mw.calls_panel.Show()
            mw.content_panel.Layout()
            mw.calls_panel.on_show()
