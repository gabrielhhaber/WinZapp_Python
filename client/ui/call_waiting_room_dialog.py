"""The waiting room of a call-link call (main_window/call_links.py).

Whoever opens the call's link without a WhatsApp account (a guest), or any
link whose admin asked for approval, waits here until the call's admin lets
them in. One window at a time, opened from the call window; the list follows
the room while it is open, keeping the selected person by address, and names
each person by name, never by number.
"""

import wx

from core.call_links import person_label


class CallWaitingRoomDialog(wx.Dialog):
    def __init__(self, parent, main_window, call_id: str):
        self.main_window = main_window
        self.call_id = call_id
        t = main_window.i18n.t
        super().__init__(parent, title=t("call_link_waiting_room_title"), style=wx.DEFAULT_DIALOG_STYLE)
        self._people = []
        self._busy = False
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(panel, label=t("call_link_waiting_list_label")), 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self.people_list = wx.ListBox(panel, size=(360, 160), style=wx.LB_SINGLE)
        sizer.Add(self.people_list, 1, wx.EXPAND | wx.ALL, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.admit_button = wx.Button(panel, label=t("call_link_admit_button"))
        self.deny_button = wx.Button(panel, label=t("call_link_deny_button"))
        close_button = wx.Button(panel, wx.ID_CANCEL, label=t("close"))
        for button in (self.admit_button, self.deny_button, close_button):
            buttons.Add(button, 0, wx.ALL, 4)
        sizer.Add(buttons, 0, wx.ALL, 4)
        panel.SetSizer(sizer)
        sizer.Fit(self)
        self.admit_button.Bind(wx.EVT_BUTTON, lambda event: self._decide(True))
        self.deny_button.Bind(wx.EVT_BUTTON, lambda event: self._decide(False))
        close_button.Bind(wx.EVT_BUTTON, lambda event: self.Close())
        self.people_list.Bind(wx.EVT_LISTBOX_DCLICK, lambda event: self._decide(True))
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.SetEscapeId(wx.ID_CANCEL)
        self.set_people([])

    def focus_list(self):
        self.people_list.SetFocus()

    def set_people(self, people):
        """Show *people* ([{jid, name, guest}]), keeping who was selected."""
        if not self:
            return
        selected = self._selected()
        if [p["jid"] for p in people] == [p["jid"] for p in self._people] and people == self._people:
            return
        self._people = list(people)
        labels = ([person_label(self.main_window.i18n, p) for p in self._people] or
                  [self.main_window.i18n.t("call_link_nobody_waiting")])
        self.people_list.Freeze()
        try:
            self.people_list.Set(labels)
            index = next((i for i, p in enumerate(self._people)
                          if selected and p["jid"] == selected["jid"]), 0)
            self.people_list.SetSelection(index)
        finally:
            self.people_list.Thaw()
        self._sync_buttons()

    def _selected(self):
        index = self.people_list.GetSelection() if self.people_list else wx.NOT_FOUND
        return self._people[index] if 0 <= index < len(self._people) else None

    def _sync_buttons(self):
        enabled = bool(self._people) and not self._busy
        self.admit_button.Enable(enabled)
        self.deny_button.Enable(enabled)

    def _decide(self, admit: bool):
        person = self._selected()
        if person is None or self._busy:
            return
        self._busy = True
        self._sync_buttons()

        def done(_ok):
            if not self:
                return
            self._busy = False
            self._sync_buttons()

        self.main_window.decide_call_link_person(self.call_id, person, admit, done)

    def _on_close(self, event):
        if getattr(self.main_window, "_call_waiting_room_dialog", None) is self:
            self.main_window._call_waiting_room_dialog = None
        self.Destroy()
