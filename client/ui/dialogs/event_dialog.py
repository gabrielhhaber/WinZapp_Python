"""WinZapp – WhatsApp event windows.

EventDialog creates or edits an event: name, description, start and end (a
date and a time each, typed in the app's own formats), place, call (none,
voice, video) and whether guests may be brought. EventDetailsDialog opens an
event row: what, when, where, the answers by kind with names, and the buttons
to answer, edit or cancel it.

Plain wx controls only, each with its label, so a screen reader reads them.
The rules are core/events.py.
"""

import time

import wx

from core import events as ev
from ui.accessible import split_mnemonic

CALLS = (None, "voice", "video")


def _formats(i18n):
    return i18n.t("date_fmt"), i18n.t("time_fmt")


class EventDialog(wx.Dialog):
    """After ``wx.ID_OK``: :attr:`fields`, the body of send-event/edit-event.
    With *event* (an eventMessage) it edits that event."""

    def __init__(self, main_window, event=None):
        self._mw = main_window
        self._event = event or {}
        i18n = main_window.i18n
        super().__init__(main_window, title=i18n.t("event_edit_title" if event else "event_new_title"),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.fields = {}
        self._build(i18n)
        self.SetMinSize((440, 520))
        self.Fit()
        self.CentreOnParent()

    def _text(self, panel, sizer, label, value="", multiline=False):
        sizer.Add(wx.StaticText(panel, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        ctrl = wx.TextCtrl(panel, value=value,
                           style=wx.TE_MULTILINE if multiline else 0,
                           size=(-1, 70) if multiline else wx.DefaultSize)
        ctrl.SetName(label.replace("&", ""))
        sizer.Add(ctrl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        return ctrl

    def _build(self, i18n):
        t = i18n.t
        date_fmt, time_fmt = _formats(i18n)
        e = self._event
        start = e.get("startTime") or ev.default_start(time.time())
        start_date, start_time = ev.format_moment(start, date_fmt, time_fmt)
        end_date, end_time = ev.format_moment(e.get("endTime") or 0, date_fmt, time_fmt)
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self._name = self._text(panel, sizer, t("event_name_label"), e.get("name", ""))
        self._description = self._text(panel, sizer, t("event_description_label"),
                                       e.get("description", ""), multiline=True)
        self._start_date = self._text(panel, sizer, t("event_start_date_label"), start_date)
        self._start_time = self._text(panel, sizer, t("event_start_time_label"), start_time)
        self._end_date = self._text(panel, sizer, t("event_end_date_label"), end_date)
        self._end_time = self._text(panel, sizer, t("event_end_time_label"), end_time)
        self._location = self._text(panel, sizer, t("event_location_label"), e.get("location", ""))
        sizer.Add(wx.StaticText(panel, label=t("event_call_label")), 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self._call = wx.Choice(panel, choices=[t("event_call_none"), t("event_call_voice"),
                                               t("event_call_video")])
        self._call.SetName(t("event_call_label").replace("&", ""))
        current = ev.call_type_of(e.get("joinLink"))
        self._call.SetSelection(CALLS.index(current) if current in CALLS else 0)
        sizer.Add(self._call, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self._guests = wx.CheckBox(panel, label=t("event_extra_guests_label"))
        self._guests.SetValue(bool(e.get("extraGuestsAllowed")))
        if self._event:
            self._guests.Disable()       # WhatsApp Web's edit does not change it
        sizer.Add(self._guests, 0, wx.ALL, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        ok = wx.Button(panel, wx.ID_OK, label=t("event_save" if self._event else "event_create"))
        ok.Bind(wx.EVT_BUTTON, self._on_ok)
        ok.SetDefault()
        buttons.Add(ok, 0, wx.ALL, 5)
        buttons.Add(wx.Button(panel, wx.ID_CANCEL, label=t("cancel")), 0, wx.ALL, 5)
        sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 8)
        panel.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)
        self._name.SetFocus()

    def _on_ok(self, event=None):
        i18n = self._mw.i18n
        date_fmt, time_fmt = _formats(i18n)
        start = ev.parse_moment(self._start_date.GetValue(), self._start_time.GetValue(),
                                date_fmt, time_fmt)
        end_typed = self._end_date.GetValue().strip() or self._end_time.GetValue().strip()
        end = (ev.parse_moment(self._end_date.GetValue() or self._start_date.GetValue(),
                               self._end_time.GetValue(), date_fmt, time_fmt)
               if end_typed else None)
        problem = ev.validate(self._name.GetValue(), start, end, time.time())
        if start is None:
            problem = "event_date_invalid"
        elif end_typed and end is None:
            problem = "event_date_invalid"
        if problem:
            example_date, example_time = ev.format_moment(time.time(), date_fmt, time_fmt)
            text = i18n.t(problem).format(date=example_date, time=example_time)
            wx.MessageBox(text, i18n.t("event_label"), wx.OK | wx.ICON_WARNING, self)
            return
        call = CALLS[self._call.GetSelection()] if self._call.GetSelection() >= 0 else None
        fields = {
            "name": self._name.GetValue().strip(),
            "description": self._description.GetValue().strip(),
            "location": self._location.GetValue().strip(),
            "startTime": start,
            "endTime": end,
            "callType": call,
        }
        if not self._event:
            fields["extraGuestsAllowed"] = self._guests.GetValue()
        self.fields = fields
        self.EndModal(wx.ID_OK)


class EventDetailsDialog(wx.Dialog):
    """An event and its answers. After ``wx.ID_OK``, :attr:`choice` is
    ("respond", 1|2|3), ("edit", None) or ("cancel", None)."""

    def __init__(self, main_window, msg, name_of):
        self._mw = main_window
        i18n = main_window.i18n
        t = i18n.t
        self.choice = None
        self._event = ev.event_content(msg.get("message") or {})
        from_me = bool((msg.get("key") or {}).get("fromMe"))
        super().__init__(main_window, title=self._event.get("name") or t("event_label"),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        details = wx.TextCtrl(panel, value=self._details_text(i18n, name_of),
                              style=wx.TE_MULTILINE | wx.TE_READONLY, size=(420, 260))
        details.SetName(split_mnemonic(t("event_details_label"))[0])
        sizer.Add(wx.StaticText(panel, label=t("event_details_label")), 0, wx.LEFT | wx.TOP, 8)
        sizer.Add(details, 1, wx.EXPAND | wx.ALL, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        can_answer = ev.can_answer(self._event, from_me, time.time())
        mine = ev.my_response(self._event)
        for response in ev.RESPONSES:
            button = wx.Button(panel, label=t(ev.RESPONSE_KEYS[response]))
            button.Bind(wx.EVT_BUTTON, lambda e, r=response: self._choose(("respond", r)))
            button.Enable(can_answer and response != mine)
            row.Add(button, 0, wx.ALL, 4)
        sizer.Add(row, 0, wx.LEFT, 4)
        if from_me and not self._event.get("isCanceled"):
            own = wx.BoxSizer(wx.HORIZONTAL)
            edit = wx.Button(panel, label=t("event_menu_edit"))
            edit.Bind(wx.EVT_BUTTON, lambda e: self._choose(("edit", None)))
            own.Add(edit, 0, wx.ALL, 4)
            cancel_event = wx.Button(panel, label=t("event_menu_cancel"))
            cancel_event.Bind(wx.EVT_BUTTON, lambda e: self._choose(("cancel", None)))
            own.Add(cancel_event, 0, wx.ALL, 4)
            sizer.Add(own, 0, wx.LEFT, 4)
        close = wx.Button(panel, wx.ID_CANCEL, label=t("close"))
        sizer.Add(close, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
        panel.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)
        self.Fit()
        self.CentreOnParent()
        details.SetFocus()

    def _details_text(self, i18n, name_of) -> str:
        t = i18n.t
        lines = [ev.event_text({k: v for k, v in self._event.items() if k != "responses"},
                               i18n, time.time())]
        if self._event.get("joinLink"):
            lines.append(t("event_link_label").format(link=self._event["joinLink"]))
        groups = {r: [] for r in ev.RESPONSES}
        for answer in self._event.get("responses") or ():
            kind = answer.get("response")
            if kind in groups:
                name = (self._mw.self_reference_label() if answer.get("isMe")
                        else name_of(answer.get("sender") or ""))
                guests = int(answer.get("extraGuestCount") or 0)
                groups[kind].append(t("event_with_guests").format(name=name, count=guests)
                                    if guests else name)
        if "responses" not in self._event:
            lines.append(t("event_responses_unknown"))
        for kind in ev.RESPONSES:
            names = groups[kind]
            lines.append(t(ev.RESPONSE_GROUP_KEYS[kind]).format(count=len(names))
                         + (": " + ", ".join(names) if names else ""))
        return "\n".join(lines)

    def _choose(self, choice):
        self.choice = choice
        self.EndModal(wx.ID_OK)
