"""transcription_segments.py — the `[mm:ss]` list inside the result window.

For a long recording the result window also shows its segments as a plain
`wx.ListBox`, one `[mm:ss] text` row each (core/transcription/segment_list.py
has the rules and why saved transcriptions have none). A list box and two
buttons, nothing drawn by hand: the arrow keys read row by row, and the
window's text field above stays the whole text with no times in it.

* **Enter on a row, or "Listen from here", jumps the voice message to that
  second** — only when that message is the one playing (see
  ui/conversation_panel/audio_seek.py for why). Otherwise it says how to make
  it work instead of doing nothing. When the caller has no player to offer
  (a status), the button is not shown and Enter does nothing, as for a list.
* **"Copy with times"** puts every row, stamps included, on the clipboard; the
  window's own Copy stays the clean text.
* **Nothing is logged**: a row is what was said.

The labels carry no Alt letter on purpose: the window's four buttons and two
labels already use them in every language, and a second control on one letter
only moves the focus.
"""

import logging

import pyperclip
import wx

from core.transcription import segment_list


class TranscriptionSegmentList(wx.Panel):
    """The label, the rows and the two buttons, as one block of the window."""

    def __init__(self, parent, main_window, segments, seek=None):
        super().__init__(parent)
        self._main_window = main_window
        self._i18n = main_window.i18n
        self._segments = segment_list.usable(segments)
        self._seek = seek

        i18n = self._i18n
        sizer = wx.BoxSizer(wx.VERTICAL)
        # The label right before its list: that ordering is what hands the text
        # to the list as its accessible name.
        sizer.Add(wx.StaticText(self, label=i18n.t("transcription_segments_label")),
                  0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self._list = wx.ListBox(self, choices=segment_list.rows(self._segments),
                                size=(520, 130))
        sizer.Add(self._list, 1, wx.EXPAND | wx.ALL, 8)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self._listen_btn = None
        if self._seek is not None:
            self._listen_btn = wx.Button(self, label=i18n.t("transcription_segments_listen"))
            self._listen_btn.Bind(wx.EVT_BUTTON, self._on_listen)
            buttons.Add(self._listen_btn, 0, wx.ALL, 4)
        self._copy_btn = wx.Button(self, label=i18n.t("transcription_segments_copy"))
        self._copy_btn.Bind(wx.EVT_BUTTON, self._on_copy)
        buttons.Add(self._copy_btn, 0, wx.ALL, 4)
        sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.RIGHT, 4)
        self.SetSizer(sizer)

        if self._segments:
            self._list.SetSelection(0)
        self._list.Bind(wx.EVT_KEY_DOWN, self._on_key)
        self._list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_listen)

    def _on_key(self, event):
        if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and self._seek is not None:
            self._on_listen()
            return
        event.Skip()

    def _on_listen(self, _event=None):
        say = self._main_window.speak_output.output
        seconds = segment_list.start_of(self._segments, self._list.GetSelection())
        if seconds is None or self._seek is None:
            return
        if self._seek(seconds):
            say(self._i18n.t("transcription_segments_playing").format(
                time=segment_list.format_stamp(seconds)), interrupt=True)
        else:
            say(self._i18n.t("transcription_segments_play_first"), interrupt=True)

    def _on_copy(self, _event=None):
        say = self._main_window.speak_output.output
        try:
            pyperclip.copy(segment_list.stamped_text(self._segments))
        except Exception as exc:
            logging.info("[transcription] copying the segments failed: %s", type(exc).__name__)
            say(self._i18n.t("transcription_result_copy_failed"))
            return
        say(self._i18n.t("transcription_segments_copied"))
