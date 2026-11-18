"""
WinZapp – New Poll Dialog
=========================
Modal dialog opened by the attachment menu's "Poll" item. The author types the
question, adds the options one at a time (Enter in the option field adds it),
chooses whether a voter may pick several, and sends.

Plain wx controls only (labelled text fields, a list box, buttons), so a screen
reader reads each one by its label. Validation is core.polls.poll_input_error().
"""

import wx

from core.polls import (
    MAX_OPTIONS, TIME_LIMITS, clean_poll_input, move_option, poll_input_error,
    selectable_count, time_limit_seconds,
)


class NewPollDialog(wx.Dialog):
    """After ``wx.ID_OK``: :attr:`question`, :attr:`options`, :attr:`selectable`,
    :attr:`hide_voter_names` and :attr:`duration_seconds` (0 = no time limit)."""

    def __init__(self, main_window):
        self._mw = main_window
        i18n = main_window.i18n
        super().__init__(
            main_window,
            title=i18n.t("new_poll_title"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.question: str = ""
        self.options: list[str] = []
        self.selectable: int = 1
        self.hide_voter_names: bool = False
        self.duration_seconds: int = 0

        self._build_ui(i18n)
        self.SetMinSize((420, 420))
        self.Fit()
        self.CentreOnParent()

    def _build_ui(self, i18n):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(wx.StaticText(panel, label=i18n.t("new_poll_question_label")),
                  0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self._question = wx.TextCtrl(panel)
        sizer.Add(self._question, 0, wx.EXPAND | wx.ALL, 8)

        sizer.Add(wx.StaticText(panel, label=i18n.t("new_poll_option_label")),
                  0, wx.LEFT | wx.RIGHT, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self._option = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self._option.Bind(wx.EVT_TEXT_ENTER, self._on_add_option)
        row.Add(self._option, 1, wx.EXPAND | wx.RIGHT, 6)
        add_btn = wx.Button(panel, label=i18n.t("new_poll_add_option"))
        add_btn.Bind(wx.EVT_BUTTON, self._on_add_option)
        row.Add(add_btn, 0)
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 8)

        sizer.Add(wx.StaticText(panel, label=i18n.t("new_poll_options_list_label")),
                  0, wx.LEFT | wx.RIGHT, 8)
        self._list = wx.ListBox(panel, style=wx.LB_SINGLE)
        sizer.Add(self._list, 1, wx.EXPAND | wx.ALL, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self._remove_btn = wx.Button(panel, label=i18n.t("new_poll_remove_option"))
        self._remove_btn.Bind(wx.EVT_BUTTON, self._on_remove_option)
        self._remove_btn.Disable()   # nothing to remove until an option is added
        buttons.Add(self._remove_btn, 0, wx.RIGHT, 6)
        # Moving needs two options to swap; like the AI provider list, the
        # buttons stay enabled at the ends and simply do nothing there.
        self._up_btn = wx.Button(panel, label=i18n.t("new_poll_move_up"))
        self._up_btn.Bind(wx.EVT_BUTTON, lambda event: self._on_move_option(-1))
        self._down_btn = wx.Button(panel, label=i18n.t("new_poll_move_down"))
        self._down_btn.Bind(wx.EVT_BUTTON, lambda event: self._on_move_option(1))
        self._up_btn.Disable()
        self._down_btn.Disable()
        buttons.Add(self._up_btn, 0, wx.RIGHT, 6)
        buttons.Add(self._down_btn, 0)
        sizer.Add(buttons, 0, wx.LEFT | wx.RIGHT, 8)

        self._multiple = wx.CheckBox(panel, label=i18n.t("new_poll_allow_multiple"))
        sizer.Add(self._multiple, 0, wx.ALL, 8)
        self._hide_voters = wx.CheckBox(panel, label=i18n.t("new_poll_hide_voters"))
        sizer.Add(self._hide_voters, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # The label comes right before the choice, so its Alt letter lands on it.
        sizer.Add(wx.StaticText(panel, label=i18n.t("new_poll_time_limit")),
                  0, wx.LEFT | wx.RIGHT, 8)
        self._time_limit = wx.Choice(panel, choices=[i18n.t(key) for key, _ in TIME_LIMITS])
        self._time_limit.SetSelection(0)
        sizer.Add(self._time_limit, 0, wx.EXPAND | wx.ALL, 8)

        btns = wx.BoxSizer(wx.HORIZONTAL)
        ok_btn = wx.Button(panel, wx.ID_OK, label=i18n.t("send_attachment"))
        ok_btn.Bind(wx.EVT_BUTTON, self._on_ok)
        ok_btn.SetDefault()
        btns.Add(ok_btn, 0, wx.ALL, 5)
        btns.Add(wx.Button(panel, wx.ID_CANCEL, label=i18n.t("cancel")), 0, wx.ALL, 5)
        sizer.Add(btns, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 8)

        panel.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)
        self._question.SetFocus()

    def _warn(self, key: str, focus: wx.Window):
        i18n = self._mw.i18n
        wx.MessageBox(i18n.t(key), i18n.t("app_name"), wx.OK | wx.ICON_WARNING, self)
        focus.SetFocus()

    def _current_options(self) -> list[str]:
        return list(self._list.GetStrings())

    def _on_add_option(self, event):
        text = self._option.GetValue().strip()
        if not text:
            self._option.SetFocus()
            return
        if len(self._current_options()) >= MAX_OPTIONS:
            self._warn("poll_error_too_many_options", self._option)
            return
        if text.casefold() in {o.casefold() for o in self._current_options()}:
            self._warn("poll_error_duplicate_option", self._option)
            return
        self._list.Append(text)
        self._remove_btn.Enable()
        self._update_move_buttons()
        self._option.Clear()
        self._option.SetFocus()

    def _on_remove_option(self, event):
        index = self._list.GetSelection()
        if index == wx.NOT_FOUND:
            return
        self._list.Delete(index)
        count = self._list.GetCount()
        if count:
            self._list.SetSelection(min(index, count - 1))
        else:
            self._remove_btn.Disable()
            self._option.SetFocus()
        self._update_move_buttons()

    def _update_move_buttons(self):
        """Enabled while there are two options to swap. (A button that disables
        itself under focus drops the screen reader's place, so the ends are
        not a reason to disable.)"""
        enabled = self._list.GetCount() >= 2
        self._up_btn.Enable(enabled)
        self._down_btn.Enable(enabled)

    def _on_move_option(self, direction: int):
        index = self._list.GetSelection()
        if index == wx.NOT_FOUND:
            return
        options, target = move_option(self._current_options(), index, direction)
        if target == index:
            return
        self._list.Freeze()
        try:
            self._list.Set(options)
            self._list.SetSelection(target)
        finally:
            self._list.Thaw()

    def _on_ok(self, event):
        # An option typed but not yet added still counts: Send is the natural
        # next key after the last option, and losing it would be silent.
        if self._option.GetValue().strip():
            self._on_add_option(event)
            if self._option.GetValue().strip():
                return
        question, options = clean_poll_input(self._question.GetValue(), self._current_options())
        error = poll_input_error(question, options)
        if error:
            self._warn(error, self._question if error == "poll_error_no_question" else self._option)
            return
        self.question = question
        self.options = options
        self.selectable = selectable_count(self._multiple.GetValue())
        self.hide_voter_names = self._hide_voters.GetValue()
        self.duration_seconds = time_limit_seconds(self._time_limit.GetSelection())
        self.EndModal(wx.ID_OK)
