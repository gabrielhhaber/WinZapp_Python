"""The "Groups in common" tab of a personal chat's info dialog."""
import logging

import wx

from core.common_groups import common_group_rows, fetch_common_groups


class CommonGroupsTabMixin:
    """Needs ``_mw``, ``_jid``, ``_i18n`` and ``_notebook`` from the dialog.

    The list is filled in two steps: the server answer arrives on the dialog's
    background thread, and the group names are looked up afterwards on the UI
    thread, where the chat list and its database are normally read.
    """

    def _build_common_groups_tab(self):
        page = wx.Panel(self._notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self._common_groups_list = wx.ListCtrl(
            page, style=wx.LC_REPORT | wx.LC_SINGLE_SEL
        )
        self._common_groups_list.InsertColumn(
            0, self._i18n.t("common_groups_tab"), width=400
        )
        self._common_groups_list.Bind(
            wx.EVT_LIST_ITEM_ACTIVATED, self._on_common_group_activated
        )
        self._common_groups_list.Bind(wx.EVT_KEY_DOWN, self._on_common_groups_key_down)
        sizer.Add(self._common_groups_list, 1, wx.EXPAND | wx.ALL, 8)
        page.SetSizer(sizer)
        self._notebook.AddPage(page, self._i18n.t("common_groups_tab"))
        self._show_common_groups_message(self._i18n.t("loading"))

    def _show_common_groups_message(self, text: str):
        """One non-actionable row: loading, nothing in common, or unavailable."""
        self._common_group_jids = []
        self._common_group_names = []
        lst = self._common_groups_list
        lst.Freeze()
        try:
            lst.DeleteAllItems()
            lst.InsertItem(0, text)
        finally:
            lst.Thaw()

    def _load_common_groups(self):
        """Ask the server (background thread) and hand the answer to the UI."""
        try:
            jids = fetch_common_groups(self._mw, self._jid)
        except Exception:
            logging.exception("[CommonGroupsTab] fetch failed")
            jids = None
        wx.CallAfter(self._populate_common_groups, jids)

    def _populate_common_groups(self, jids):
        """Main thread. *jids* is the group list, or None if the server could
        not say."""
        try:
            self._populate_common_groups_unsafe(jids)
        except RuntimeError:
            pass  # the dialog was closed before the answer arrived
        except Exception:
            logging.exception("[CommonGroupsTab] populate failed")
            try:
                self._show_common_groups_message(
                    self._i18n.t("common_groups_unavailable")
                )
            except RuntimeError:
                pass

    def _populate_common_groups_unsafe(self, jids):
        i18n = self._i18n
        if jids is None:
            self._show_common_groups_message(i18n.t("common_groups_unavailable"))
            return
        if not jids:
            self._show_common_groups_message(i18n.t("common_groups_none"))
            return

        rows = common_group_rows(
            jids, self._mw.chat_display_name, i18n.t("unknown_group")
        )
        self._common_group_jids = [jid for _, jid in rows]
        self._common_group_names = [name for name, _ in rows]
        lst = self._common_groups_list
        # One rebuild, one announcement for the screen reader.
        lst.Freeze()
        try:
            lst.DeleteAllItems()
            for index, (name, _jid) in enumerate(rows):
                lst.InsertItem(index, name)
        finally:
            lst.Thaw()

    def _open_common_group(self, index: int):
        if index < 0 or index >= len(self._common_group_jids):
            return
        jid = self._common_group_jids[index]
        name = self._common_group_names[index]
        # Navigate after the dialog has closed so the main window is the
        # active one when the conversation opens.
        wx.CallAfter(self._mw.navigate_to_conversation_jid, jid, name)
        self.EndModal(wx.ID_CANCEL)

    def _on_common_group_activated(self, event):
        """Enter / double-click on a group: open that conversation."""
        self._open_common_group(event.GetIndex())

    def _on_common_groups_key_down(self, event):
        """Space activates the focused group too, like Enter."""
        if event.GetKeyCode() == wx.WXK_SPACE:
            self._open_common_group(self._common_groups_list.GetFocusedItem())
        else:
            event.Skip()
