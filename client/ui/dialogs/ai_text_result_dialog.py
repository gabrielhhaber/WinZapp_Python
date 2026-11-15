"""Shared machinery of the text-only AI windows (writing help, message
translation): a status line and a read-only result, Cancel/Regenerate while
the worker runs, the GUI watchdog and spoken feedback.

A subclass adds its own controls (_extra_controls), answers where the text
goes (_consent), what the worker does (_work) and what the done text is
spoken as; this base owns modal-loop unwinding and never lets a late worker
publish into a cancelled or stale generation. Modeled on AIResultDialog
(ai_result_dialog.py), which keeps the media/kind flow; nothing here loads,
stores or sends any media.
"""
import wx

from app_paths import active_account_id
from core.ai_credentials import CredentialError
from core.ai_media.errors import DescriptionError
from core.ai_media.service import submit
from core.ai_media.session import MediaSession
from core.ai_media.diagnostics import record
from core.ai_media.feedback import ProcessingCue
from core.ai_media.transcript import spoken_answer
from ui.dialogs.ai_result_dialog import AIConsentDialog, plain

#: Extra time over the request's own overall budget before the window gives up
#: waiting for a worker that never reported back (same rule as AIResultDialog).
_WATCHDOG_GRACE_SECONDS = 5


class TextAIResultDialog(wx.Dialog):
    """One text-in/text-out AI action window. Subclasses set has_replace for a
    "Replace draft" button and implement the hooks at the bottom."""

    has_replace = False

    def __init__(self, panel, identity, kind, locked, config, app_settings):
        self.panel = panel
        self.main_window = panel.main_window
        self.i18n = self.main_window.i18n
        self.session = MediaSession(*identity, kind, locked=locked)
        self.config = config
        self.app = app_settings
        self._consent_dialog = None
        self._latest = ""
        self._timer = None
        self._processing = ProcessingCue(self.main_window)
        super().__init__(panel, title=self.i18n.t(self.title_key()),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER, size=(680, 560))
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(wx.StaticText(self, label=self.i18n.t("status")), 0, wx.LEFT | wx.TOP, 8)
        self.status = wx.TextCtrl(self, style=wx.TE_READONLY, name=self.i18n.t("status"))
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        self._extra_controls(layout)
        layout.Add(wx.StaticText(self, label=self.i18n.t("ai_result")), 0, wx.LEFT, 8)
        self.result = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE,
                                  name=self.i18n.t("ai_result"))
        layout.Add(self.result, 1, wx.EXPAND | wx.ALL, 8)
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        self.replace = None
        if self.has_replace:
            self.replace = self._button(buttons, "writing_replace", self._replace)
        self.copy = self._button(buttons, "ai_copy_answer", self._copy)
        self.regenerate = self._button(buttons, self._regenerate_key(), self._regenerate)
        self.cancel = self._button(buttons, "cancel", self._cancel)
        close = wx.Button(self, wx.ID_CANCEL, label=plain(self.i18n, "close"))
        close.Bind(wx.EVT_BUTTON, self._close)
        buttons.Add(close, 0, wx.ALL, 5)
        layout.Add(buttons, 0, wx.ALL, 5)
        self.SetSizer(layout)
        self.SetMinSize((560, 450))
        self.Bind(wx.EVT_CLOSE, self._close)
        self.Bind(wx.EVT_CHAR_HOOK, self._key)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        self._busy(False)
        self._on_open()

    # ── subclass hooks ──────────────────────────────────────────────────────

    def title_key(self):
        raise NotImplementedError

    def _on_open(self):
        self.result.SetFocus()
        # ShowModal's event loop must start before consent, work or errors.
        wx.CallAfter(self._regenerate)

    def _regenerate_key(self):
        return "ai_regenerate"

    def _extra_controls(self, layout):
        """Subclass controls, sizered between the status line and the result."""

    def _question(self):
        """The question tracked in the session history ("" is fine)."""
        return ""

    def _consent(self):
        """Ask before text leaves the machine; True when it may. Runs on the
        GUI thread through _run_consent()."""
        raise NotImplementedError

    def _worker(self, operation):
        """The callable submit() runs: returns (answer, provider name shown)."""
        raise NotImplementedError

    def _replace(self, event=None):
        """has_replace windows: put the latest answer where the person wants it."""

    # ── consent plumbing ────────────────────────────────────────────────────

    def _run_consent(self, text):
        """Show the consent window; returns (accepted, remember-for-next-time).
        The window names where the text goes; a locked chat is asked every
        time (the remember checkbox stays disabled there)."""
        dialog = AIConsentDialog(self, self.i18n, (), "", self.session.locked, text=text)
        self._consent_dialog = dialog
        try:
            accepted = dialog.ShowModal() == wx.ID_OK and self._valid()
            return accepted, accepted and dialog.remember.GetValue() and not self.session.locked
        finally:
            self._consent_dialog = None
            if dialog:
                dialog.Destroy()
            # EndModal only requests exit. The child ShowModal must return
            # before MSW can end the parent's now-active modal event loop.
            if self.session.closed and self:
                self._finish_close()

    def _consent_translate(self):
        """Consent for Google Translate (no provider, no key): remembered in
        the consented_translate setting; a locked chat is asked every time."""
        if self.config["consented_translate"] and not self.session.locked:
            return True
        text = self.i18n.t("ai_translate_consent")
        if self.session.locked:
            text += "\n\n" + self.i18n.t("ai_locked_consent")
        accepted, remember = self._run_consent(text)
        if accepted and remember:
            def merge(old):
                value = dict(old) if isinstance(old, dict) else {}
                value["consented_translate"] = True
                return value
            try:
                self.app.update("ai_media", merge)
                self.config["consented_translate"] = True
            except (OSError, RuntimeError):
                # Consent is valid for this session even if remembering failed.
                pass
        return accepted

    # ── window machinery ────────────────────────────────────────────────────

    def _button(self, row, key, handler):
        button = wx.Button(self, label=plain(self.i18n, key))
        button.Bind(wx.EVT_BUTTON, handler)
        row.Add(button, 0, wx.ALL, 5)
        return button

    def _valid(self):
        return (not self.session.closed and active_account_id() == self.session.identity[0]
                and not getattr(self.main_window, "_shutting_down", False)
                and (self.panel.conversation or {}).get("remoteJid", "") == self.session.identity[1]
                and (not self.session.locked or getattr(self.main_window, "_chat_lock_unlocked", False)))

    def _speak(self, text):
        if self._valid() and self.main_window.IsShown():
            self.main_window.output(text)

    def _busy(self, busy):
        if busy:
            self._processing.start()
        else:
            self._processing.stop()
        self.regenerate.Enable(not busy)
        self.cancel.Enable(busy)
        self.copy.Enable(bool(self._latest))
        if self.replace is not None:
            self.replace.Enable(bool(self._latest) and not busy)

    def _key(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self._close()
            return
        event.Skip()

    def _show_error(self, error):
        text = self.i18n.t(str(error)) if not getattr(error, "attempts", ()) else _failure_text(self.i18n, error)
        self.status.ChangeValue(text)
        record("ui_error", category=str(error).removeprefix("ai_error_"))
        self._speak(text)

    def _regenerate(self, event=None):
        if not self._valid() or self.session.active is not None:
            return
        try:
            if not self._consent():
                if not self._latest and self._valid():
                    # Declining the first prompt leaves nothing to show: close
                    # the window rather than dropping focus into an empty result.
                    self._close()
                return
            question = self._question()
            generation, operation = self.session.begin(question)
            record("ui_start", generation=generation)
            self._busy(True)
            loading = self.i18n.t("ai_processing_msg")
            self.status.ChangeValue(loading)
            self._speak(loading)
            work = self._worker(operation)
            # Arm the GUI watchdog BEFORE dispatch; even a failed/very fast
            # worker callback must never leave an unprotected busy state.
            self._timer = wx.CallLater(int((operation.total_seconds + _WATCHDOG_GRACE_SECONDS) * 1000),
                                       self._timeout, generation)
            record("ui_waiting", generation=generation)
            submit(work, lambda result, error: wx.CallAfter(self._complete, generation, question,
                                                            result, error))
        except (DescriptionError, CredentialError) as exc:
            self._cancel()
            self._show_error(exc.key if isinstance(exc, DescriptionError) else str(exc))
        except Exception as exc:
            record("ui_error", category="response", exception_type=type(exc).__name__)
            self._cancel()
            self._show_error("ai_error_response")

    def _complete(self, generation, question, result, error):
        if not self._valid() or generation != self.session.generation or self.session.active is None:
            record("ui_completion_ignored", generation=generation)
            return
        record("ui_completed", generation=generation)
        try:
            self.session.active.check()
        except DescriptionError as exc:
            self._cancel()
            self._show_error(exc.key)
            return
        if self._timer:
            self._timer.Stop()
            self._timer = None
        if error is not None:
            self.session.accept(generation, question, None)
            self._busy(False)
            self._show_error(error)
        else:
            answer, provider = result
            if not self.session.accept(generation, question, answer):
                return
            self._latest = answer
            # Stop/free the waiting stream before updating or speaking. The
            # latest answer must already exist when Copy is re-enabled.
            self._busy(False)
            self.result.ChangeValue(answer)
            ready = self.i18n.t("ai_ready") + " " + self.i18n.t("ai_answered_by").format(
                provider=provider)
            self.status.ChangeValue(ready)
            self._speak(spoken_answer(answer) if self.config["read_answers"] else ready)

    def _timeout(self, generation):
        record("ui_watchdog", generation=generation)
        if self._valid() and generation == self.session.generation:
            self._cancel()
            self._show_error("ai_error_timeout")

    def _cancel(self, event=None):
        record("ui_cancel")
        self.session.cancel()
        if self._timer:
            self._timer.Stop()
            self._timer = None
        self._busy(False)
        self.status.ChangeValue(self.i18n.t("ai_cancel_notice"))

    def _copy(self, event):
        if self._latest and wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(wx.TextDataObject(self._latest))
            finally:
                wx.TheClipboard.Close()

    def _dispose(self):
        """Also reached on Escape/unexpected destruction, without wx calls."""
        if self.session.closed:
            return
        self._processing.stop()
        record("ui_close")
        self.session.close()
        self._latest = ""
        if self._timer:
            self._timer.Stop()
            self._timer = None

    def _close(self, event=None):
        self._dispose()
        self.result.ChangeValue("")
        self.status.ChangeValue("")
        # Clear private state immediately, but unwind nested modal loops
        # inside-out. Ending the parent in the child's active loop leaves
        # the main window disabled on MSW.
        if self._consent_dialog is not None:
            if self._consent_dialog and self._consent_dialog.IsModal():
                self._consent_dialog.EndModal(wx.ID_CANCEL)
            return  # _run_consent's finally finishes the outer close.
        self._finish_close()

    def _finish_close(self):
        if self.IsModal():
            record("ui_close_finished")
            self.EndModal(wx.ID_CANCEL)

    def _destroyed(self, event):
        if event.GetEventObject() is self:
            self._dispose()
        event.Skip()


def _failure_text(i18n, error):
    """The sentence for a chain that tried several providers (same shape as
    ai_result_dialog.failure_text)."""
    from core.ai_media.config import PROVIDERS
    attempts = getattr(error, "attempts", ())
    lines = [i18n.t("ai_attempt_line").format(provider=PROVIDERS[p].name,
                                              reason=i18n.t(f"ai_error_{category}"))
             for p, category in attempts]
    return i18n.t("ai_chain_failed") + "\n" + "\n".join(lines)
