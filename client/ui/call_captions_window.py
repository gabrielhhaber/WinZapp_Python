"""The live captions window of a call (ACC-04): a plain list, read on demand.

A modeless top-level window like the call window. What matters here:

* **It never takes the focus by itself.** ``open_quietly()`` shows it without
  activating it and rows are appended without touching selection or focus; the
  call window keeps its controls. The user moves here with the Live captions
  command (``focus_list()``), reads the rows like any list, and leaves with
  Escape or Close.
* **Rows are only appended.** A finished phrase is one new row, never an
  edit of an old one (a screen reader reads a rewritten focused row aloud).
  Appending runs inside ``Freeze()``/``Thaw()``.
* **Nothing is saved or copied but by a button.** The text lives in memory
  until the window closes; Copy and Save act on the user's request.
* **Speaking the captions is a checkbox, off by default**, kept in
  ``settings["calls"]["captions_speak"]``. The sentences go through
  ``speak_output`` and are rationed by ``SpeechCoalescer``.

The decisions about phrases, rows and speech are ``core/call_captions.py``.
"""

import logging
import time

import wx

from core import call_captions as cc
from core.transcription import errors
from core.save_location import resolve_save_dialog_folder
from ui.dialogs.transcription_result import with_txt_extension, write_transcript

SPEECH_TICK_MS = 500


class CallCaptionsWindow(wx.Frame):
    def __init__(self, main_window, *, on_close):
        self._mw = main_window
        self._on_close_callback = on_close
        self._entries = []
        self._speech = cc.SpeechCoalescer()
        self._speech_timer = None
        self._alive = True
        self._was_active = False
        i18n = main_window.i18n
        super().__init__(None, title=i18n.t("call_captions_title"), size=(640, 420))
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(panel, label=i18n.t("call_captions_list_label")),
                  0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self._list = wx.ListBox(panel, style=wx.LB_SINGLE)
        sizer.Add(self._list, 1, wx.EXPAND | wx.ALL, 8)
        self._status = wx.StaticText(panel, label=i18n.t("call_captions_status_listening"))
        sizer.Add(self._status, 0, wx.LEFT | wx.RIGHT, 8)
        self._speak = wx.CheckBox(panel, label=i18n.t("call_captions_speak"))
        self._speak.SetValue(bool((main_window.settings.get("calls") or {})
                                  .get("captions_speak", False)))
        self._speak.Bind(wx.EVT_CHECKBOX, self._on_speak_toggled)
        sizer.Add(self._speak, 0, wx.ALL, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        copy = wx.Button(panel, label=i18n.t("transcription_result_copy"))
        save = wx.Button(panel, label=i18n.t("transcription_result_save"))
        close = wx.Button(panel, label=i18n.t("close"))
        copy.Bind(wx.EVT_BUTTON, self._on_copy)
        save.Bind(wx.EVT_BUTTON, self._on_save)
        close.Bind(wx.EVT_BUTTON, lambda _event: self.Close())
        for button in (copy, save, close):
            buttons.Add(button, 0, wx.ALL, 8)
        sizer.Add(buttons, 0, wx.ALIGN_CENTER)
        panel.SetSizer(sizer)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char)

    # ── What the call shows ──────────────────────────────────────────────────

    def open_quietly(self) -> None:
        """Show the window without activating it."""
        self.ShowWithoutActivating()

    def focus_list(self) -> None:
        """The user asked to read the captions: activate and focus the list."""
        if not self.IsShown():
            self.Show()
        self.Raise()
        self._list.SetFocus()

    def add_entries(self, entries) -> None:
        if not self._alive or not entries:
            return
        self._list.Freeze()
        try:
            for entry in entries:
                self._entries.append(entry)
                self._list.Append(cc.format_row(entry))
        finally:
            self._list.Thaw()
        if self._speak.GetValue():
            for entry in entries:
                text = self._speech.add(cc.format_row(entry), time.monotonic())
                if text:
                    self._say(text)
            if self._speech.has_pending:
                self._arm_speech_timer()

    def set_status(self, text: str) -> None:
        if self._alive and self._status.GetLabel() != text:
            self._status.SetLabel(text)

    @property
    def entries(self) -> list:
        return list(self._entries)

    # ── Speech ───────────────────────────────────────────────────────────────

    def _say(self, text) -> None:
        self._mw.speak_output.output(text)

    def _arm_speech_timer(self) -> None:
        if self._speech_timer is None and self._alive:
            self._speech_timer = wx.CallLater(SPEECH_TICK_MS, self._speech_tick)

    def _speech_tick(self) -> None:
        self._speech_timer = None
        if not self._alive or not self._speak.GetValue():
            return
        text = self._speech.due(time.monotonic())
        if text:
            self._say(text)
        elif self._speech.has_pending:
            self._arm_speech_timer()

    def _on_speak_toggled(self, _event) -> None:
        on = self._speak.GetValue()
        if not on:
            self._speech.clear()
        try:
            self._mw.settings.setdefault("calls", {})["captions_speak"] = bool(on)
            self._mw.save_settings()
        except Exception as exc:
            logging.warning("[call_captions] could not save the speak choice: %s",
                            type(exc).__name__)

    # ── Copy and save: only on a press ───────────────────────────────────────

    def _on_copy(self, _event) -> None:
        i18n = self._mw.i18n
        if not self._entries:
            self._mw.speak_output.output(i18n.t("call_captions_empty"))
            return
        copied = False
        if wx.TheClipboard.Open():
            try:
                copied = wx.TheClipboard.SetData(
                    wx.TextDataObject(cc.transcript_text(self._entries)))
            finally:
                wx.TheClipboard.Close()
        self._mw.speak_output.output(i18n.t(
            "transcription_result_copied" if copied else "transcription_result_copy_failed"))

    def _on_save(self, _event) -> None:
        i18n = self._mw.i18n
        if not self._entries:
            self._mw.speak_output.output(i18n.t("call_captions_empty"))
            return
        with wx.FileDialog(
            self, i18n.t("transcription_save_title"),
            defaultDir=resolve_save_dialog_folder(self._mw.settings),
            defaultFile=i18n.t("call_captions_title") + ".txt",
            wildcard=i18n.t("transcription_save_wildcard"),
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            destination = with_txt_extension(dialog.GetPath())
        try:
            write_transcript(destination, cc.transcript_text(self._entries))
        except OSError as exc:
            # Numbers only: an OSError's text is the path the user chose.
            logging.info("[call_captions] saving failed: %s errno=%s",
                         type(exc).__name__, exc.errno)
            self._mw.speak_output.output(i18n.t(errors.error_i18n_key(errors.SAVE_FAILED)))
            return
        self._mw.remember_save_folder(destination)
        self._mw.speak_output.output(i18n.t("transcription_result_saved"))

    # ── Closing ──────────────────────────────────────────────────────────────

    def _on_char(self, event) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE and not event.HasModifiers():
            self.Close()
            return
        event.Skip()

    @property
    def was_active(self) -> bool:
        """Whether the window had the focus when it was closed."""
        return self._was_active

    def _on_close(self, _event) -> None:
        self._was_active = self.IsActive()
        self.dispose()
        callback, self._on_close_callback = self._on_close_callback, None
        if callback is not None:
            callback(self)
        self.Destroy()

    def dispose(self) -> None:
        """Drop what the window holds; the text goes with it."""
        self._alive = False
        if self._speech_timer is not None:
            self._speech_timer.Stop()
            self._speech_timer = None
        self._speech.clear()
        self._entries = []
