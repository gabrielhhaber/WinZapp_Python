"""transcription_result.py — the window a finished transcription is read in.

A transcription is a block of text a screen-reader user needs to *read at
their own pace* — word by word, back up a line, spell a name — so it lives in
a read-only multi-line `wx.TextCtrl`, the one control every screen reader
navigates like a document. The shape follows `_show_message_text_popup()`
(ui/conversation_panel/message_menu.py), made modal because three of its four
buttons act on the conversation the user came from.

Decisions worth keeping:

* **Focus starts on the text.** It is what the user asked for, and NVDA reads
  the line under the cursor as the dialog opens.

* **The caveats get a read-only field of their own, above the text, and only
  when there are any.** A `wx.StaticText` nobody is focused on is silent to a
  screen reader; a read-only `TextCtrl` is reachable with Shift+Tab from the
  text and is read like any other field. Above rather than below because it
  is about the text, and a user tabbing forward from it should land on the
  buttons, not on a note they have already been pointed to.

* **What is spoken is decided by the caller and said once the dialog is on
  screen.** Spoken *before* `ShowModal()`, it lands underneath the screen
  reader's own announcement of the new window — NVDA cancels speech when the
  foreground changes — and the one sentence that must be heard (the voice
  filter warning, see transcription_flow.py) is exactly the one that would be
  cut. So it is queued with `wx.CallAfter` before `ShowModal()`, which runs it
  from inside the modal loop, after the window and its focus exist: the same
  arrangement `TranscriptionProgressDialog._start_job()` uses for its first
  line.

* **Mnemonics are per language and change the letter, never the word.** The
  label is what the screen reader reads on every pass of the focus, so it
  stays the plain word; where the natural letter is taken (Close is "&Fechar"
  in Portuguese, "&Close" in English, "&Zamknij" in Polish), another letter of
  the same word carries the Alt key.

* **Insert does not act from in here.** It ends the dialog and the caller
  writes into the message field once this window is gone, because it is the
  one exit where the focus must land somewhere other than the message the
  user came from — and a focus move made while a modal dialog is still up is
  undone when the dialog closes.
"""

import logging
import os
import re

import pyperclip
import wx

from core.save_location import resolve_save_dialog_folder
from core.transcription import errors

# Characters Windows refuses in a file name, plus the control characters. A
# contact's name can hold any of them ("Ana / trabalho"), and a default name
# the save dialog rejects is a dialog that fails before the user typed a thing.
_UNSAFE_FILE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')

# Long enough to recognise, short enough that the full path stays far below
# MAX_PATH in any Downloads folder.
_MAX_FILE_NAME_CHARS = 80

# Trimmed off both ends of the name. Spaces and dots because Windows drops
# them from a file name anyway; the dashes because the translated pattern
# joins the word to the name with one, and with no name to join it to
# "Transcrição -.txt" is what would be offered — and read out.
_FILE_NAME_EDGES = " .-–—"

# The stem when both translated sentences clean down to nothing (a locale file
# holding only punctuation there): ".txt" alone is a hidden, nameless file the
# save dialog offers and a screen reader reads as "dot t x t". The app's own
# name, because it is the one word that is the same in every language.
_LAST_RESORT_FILE_STEM = "WinZapp"


def default_file_name(i18n, name) -> str:
    """The name the save dialog offers: the translated word and who it is from.

    Never the message id (a meaningless string of hex a screen reader would
    spell out) and never a phone number when a name exists — `name` is what the
    window title shows.
    """
    base = _file_stem(i18n.t("transcription_save_default_name").format(name=name or ""))
    # A pattern that sanitises to nothing falls back to the window's own
    # translated word, never to a literal in one language.
    base = base or _file_stem(i18n.t("transcription_progress_title"))
    return f"{base or _LAST_RESORT_FILE_STEM}.txt"


def _file_stem(text) -> str:
    """`text` made safe to be a Windows file name, without the extension."""
    text = _UNSAFE_FILE_NAME.sub(" ", text)
    text = " ".join(text.split()).strip(_FILE_NAME_EDGES)
    return text[:_MAX_FILE_NAME_CHARS].strip(_FILE_NAME_EDGES)


def with_txt_extension(path) -> str:
    """`path`, with `.txt` added when the user typed a name with no extension.

    The Windows save dialog only appends the filter's extension on its own in
    some configurations; a file saved without one opens nowhere on a double
    click, and the user has no way to see that it happened.
    """
    return path if os.path.splitext(path)[1] else f"{path}.txt"


def write_transcript(path, text) -> None:
    """Write the transcription as UTF-8 text with Windows line endings.

    UTF-8 because a transcription is in whatever language was spoken, and the
    locale's ANSI code page cannot hold most of them; with a BOM (`utf-8-sig`)
    because Notepad on older Windows 10 builds reads a BOM-less file as ANSI and
    shows every accented letter broken. `\\r\\n` for the same readers.
    """
    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as handle:
        handle.write(text)


class TranscriptionResultDialog(wx.Dialog):
    """Shows one transcription. Used once, then destroyed.

    `run()` returns the modal code; `insert_requested` tells the caller that the
    user chose "Insert into the message" rather than closing.
    """

    def __init__(self, parent, main_window, title, text, notes=(),
                 spoken="", default_file="", can_insert=True):
        self._main_window = main_window
        self._i18n = main_window.i18n
        self._text = text
        self._notes = tuple(notes)
        self._spoken = spoken
        self._default_file = default_file or default_file_name(self._i18n, "")
        self._can_insert = bool(can_insert)
        super().__init__(
            parent,
            title=title,
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )

        #: True when the dialog was closed through "Insert into the message".
        self.insert_requested = False

        self._build_ui()

    def _build_ui(self):
        i18n = self._i18n
        sizer = wx.BoxSizer(wx.VERTICAL)

        self._notes_field = None
        if self._notes:
            # The label is created right before its field: that ordering is what
            # makes Windows hand the label to the field as its accessible name.
            notes_label = wx.StaticText(self, label=i18n.t("transcription_result_notes_label"))
            self._notes_field = wx.TextCtrl(
                self,
                value="\n".join(self._notes),
                style=wx.TE_MULTILINE | wx.TE_READONLY,
                size=(520, 70),
            )
            sizer.Add(notes_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
            sizer.Add(self._notes_field, 0, wx.EXPAND | wx.ALL, 8)

        text_label = wx.StaticText(self, label=i18n.t("transcription_result_text_label"))
        self._text_field = wx.TextCtrl(
            self,
            value=self._text,
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(520, 260),
        )
        sizer.Add(text_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(self._text_field, 1, wx.EXPAND | wx.ALL, 8)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self._copy_btn = wx.Button(self, label=i18n.t("transcription_result_copy"))
        self._save_btn = wx.Button(self, label=i18n.t("transcription_result_save"))
        self._insert_btn = wx.Button(self, label=i18n.t("transcription_result_insert"))
        # wx.ID_CANCEL is what makes Escape close the dialog: wxDialog turns the
        # key into a click on the button carrying that id, and with no handler
        # of our own bound to it the click ends the modal loop by itself.
        self._close_btn = wx.Button(self, wx.ID_CANCEL, label=i18n.t("close"))
        self._copy_btn.Bind(wx.EVT_BUTTON, self._on_copy)
        self._save_btn.Bind(wx.EVT_BUTTON, self._on_save)
        self._insert_btn.Bind(wx.EVT_BUTTON, self._on_insert)
        for button in (self._copy_btn, self._save_btn, self._insert_btn, self._close_btn):
            buttons.Add(button, 0, wx.ALL, 4)
        # A status has no message field to insert into (the status tab's
        # result window): the button is not offered rather than left dead.
        self._insert_btn.Show(self._can_insert)
        sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 4)

        self.SetSizer(sizer)
        sizer.Fit(self)
        self.CentreOnParent()
        self._text_field.SetInsertionPoint(0)
        self._text_field.SetFocus()

    def run(self):
        """Show the dialog; say what the caller asked for once it is up."""
        if self._spoken:
            # Queued before ShowModal() so it runs inside the modal loop, after
            # the screen reader has announced the window — see the module
            # docstring for why the order matters.
            wx.CallAfter(self._announce)
        return self.ShowModal()

    def _announce(self):
        if not self:
            return
        # No interrupt: the screen reader is finishing the window's own
        # announcement, and this is meant to follow it, not replace it.
        self._main_window.speak_output.output(self._spoken)

    # ── Buttons ──────────────────────────────────────────────────────────────

    def _on_copy(self, _event=None):
        try:
            pyperclip.copy(self._text)
        except Exception as exc:
            logging.info("[transcription] copying the result failed: %s", type(exc).__name__)
            self._main_window.speak_output.output(self._i18n.t("transcription_result_copy_failed"))
            return
        self._main_window.speak_output.output(self._i18n.t("transcription_result_copied"))

    def _on_save(self, _event=None):
        i18n = self._i18n
        settings = self._main_window.settings
        with wx.FileDialog(
            self,
            i18n.t("transcription_save_title"),
            defaultDir=resolve_save_dialog_folder(settings),
            defaultFile=self._default_file,
            wildcard=i18n.t("transcription_save_wildcard"),
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            destination = with_txt_extension(dlg.GetPath())
        self._save_to(destination)

    def _save_to(self, destination):
        """Write the file and say how it went. Split out so it can be tested."""
        i18n = self._i18n
        try:
            write_transcript(destination, self._text)
        except OSError as exc:
            # Numbers only: the text of an OSError about a file is its path,
            # and the path the user picked may name the contact.
            logging.info(
                "[transcription] saving the result failed: %s errno=%s winerror=%s",
                type(exc).__name__, exc.errno, getattr(exc, "winerror", None),
            )
            error_sound = getattr(self._main_window, "error_sound", None)
            if error_sound is not None:
                # A sound that raises must not cost the sentence below — the
                # only thing that tells the user the file was not written.
                # The type only, not exception_report(): raised in here, the
                # sound's exception carries the OSError above as its
                # __context__, and that one's text is the path the user picked.
                try:
                    error_sound.play()
                except Exception as sound_exc:
                    logging.warning("[transcription] could not play the error sound: %s",
                                    type(sound_exc).__name__)
            self._main_window.speak_output.output(
                i18n.t(errors.error_i18n_key(errors.SAVE_FAILED))
            )
            return False
        self._main_window.remember_save_folder(destination)
        self._main_window.speak_output.output(i18n.t("transcription_result_saved"))
        return True

    def _on_insert(self, _event=None):
        self.insert_requested = True
        self.EndModal(wx.ID_OK)
