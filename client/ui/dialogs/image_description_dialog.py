"""Accessible RAM-only photo Q&A, owned by the account's conversation panel."""
import wx

from app_paths import active_account_id, global_dir
from core.ai_credentials import CredentialStore, CredentialError
from core.image_description.config import MAX_QUESTION_CHARS, PROVIDERS
from core.image_description.errors import DescriptionError
from core.image_description.image_input import prepare_image
from core.image_description.prompts import instructions
from core.image_description.service import request_answer, submit
from core.image_description.session import PhotoSession
from core.image_description.diagnostics import record
from core.image_description.feedback import ProcessingCue
from core.image_description.transcript import render_history, spoken_answer


class PhotoConsentDialog(wx.Dialog):
    def __init__(self, parent, i18n, provider, locked):
        super().__init__(parent, title=i18n.t("ai_consent_title"))
        layout = wx.BoxSizer(wx.VERTICAL)
        message = i18n.t("ai_consent").format(provider=PROVIDERS[provider].name)
        if provider == "gemini":
            message += "\n\n" + i18n.t("ai_gemini_notice")
        if locked:
            message += "\n\n" + i18n.t("ai_locked_consent")
        text = wx.TextCtrl(self, value=message, style=wx.TE_MULTILINE | wx.TE_READONLY,
                           size=(520, 220), name=i18n.t("ai_consent_title"))
        layout.Add(text, 1, wx.EXPAND | wx.ALL, 10)
        self.remember = wx.CheckBox(self, label=i18n.t("ai_remember_consent"))
        self.remember.Enable(not locked)
        layout.Add(self.remember, 0, wx.ALL, 10)
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        buttons.Add(wx.Button(self, wx.ID_OK, label=i18n.t("ai_send_photo")), 0, wx.ALL, 8)
        cancel = wx.Button(self, wx.ID_CANCEL, label=i18n.t("cancel"))
        buttons.Add(cancel, 0, wx.ALL, 8)
        layout.Add(buttons)
        self.SetSizerAndFit(layout)
        cancel.SetDefault()
        text.SetFocus()


class ImageDescriptionDialog(wx.Dialog):
    def __init__(self, panel, identity, locked, config, app_settings, loader):
        self.panel = panel
        self.main_window = panel.main_window
        self.i18n = self.main_window.i18n
        self.session = PhotoSession(*identity, locked=locked)
        self.config = config
        self.app = app_settings
        self.loader = loader
        self._consented = False
        self._consent_dialog = None
        self._latest = ""
        self._timer = None
        self._description_first = False
        self._processing = ProcessingCue(self.main_window)
        super().__init__(panel, title=self.i18n.t("ai_title"),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER, size=(680, 560))
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(wx.StaticText(self, label=PROVIDERS[config["provider"]].name + " — " + config["model"]),
                   0, wx.ALL, 8)
        layout.Add(wx.StaticText(self, label=self.i18n.t("ai_status")), 0, wx.LEFT, 8)
        self.status = wx.TextCtrl(self, style=wx.TE_READONLY, name=self.i18n.t("ai_status"))
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        layout.Add(wx.StaticText(self, label=self.i18n.t("ai_result")), 0, wx.LEFT, 8)
        self.result = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE,
                                 name=self.i18n.t("ai_result"))
        layout.Add(self.result, 1, wx.EXPAND | wx.ALL, 8)
        layout.Add(wx.StaticText(self, label=self.i18n.t("ai_question")), 0, wx.LEFT, 8)
        self.question = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(-1, 75),
                                   name=self.i18n.t("ai_question"))
        self.question.SetMaxLength(MAX_QUESTION_CHARS)
        layout.Add(self.question, 0, wx.EXPAND | wx.ALL, 8)
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        self.ask = self._button(buttons, "ai_ask", self._ask)
        self.copy = self._button(buttons, "ai_copy_answer", self._copy)
        self.regenerate = self._button(buttons, "ai_regenerate", self._regenerate)
        self.cancel = self._button(buttons, "cancel", self._cancel)
        close = wx.Button(self, wx.ID_CANCEL, label=self.i18n.t("close"))
        close.Bind(wx.EVT_BUTTON, self._close)
        buttons.Add(close, 0, wx.ALL, 5)
        layout.Add(buttons, 0, wx.ALL, 5)
        self.SetSizer(layout)
        self.SetMinSize((560, 450))
        self.Bind(wx.EVT_CLOSE, self._close)
        self.Bind(wx.EVT_CHAR_HOOK, self._key)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        self._busy(False)
        self.result.SetFocus()
        # ShowModal's event loop must start before consent, loading or errors.
        wx.CallAfter(self._regenerate)

    def _button(self, row, key, handler):
        button = wx.Button(self, label=self.i18n.t(key))
        button.Bind(wx.EVT_BUTTON, handler)
        row.Add(button, 0, wx.ALL, 5)
        return button

    def _valid(self):
        return (not self.session.closed and active_account_id() == self.session.identity[0]
                and not getattr(self.main_window, "_shutting_down", False)
                and (not self.session.locked or getattr(self.main_window, "_chat_lock_unlocked", False)))

    def _consent(self):
        if self._consented:
            return True
        if self.config["consented"] and not self.session.locked:
            self._consented = True
            return True
        dialog = PhotoConsentDialog(self, self.i18n, self.config["provider"], self.session.locked)
        self._consent_dialog = dialog
        try:
            accepted = dialog.ShowModal() == wx.ID_OK and self._valid()
            if accepted and dialog.remember.GetValue() and not self.session.locked:
                provider = self.config["provider"]
                def merge(old):
                    value = dict(old) if isinstance(old, dict) else {}
                    current = value.get("consented", [])
                    consented = set(current if isinstance(current, list) else [])
                    value["consented"] = sorted(consented | {provider})
                    return value
                try:
                    self.app.update("image_description", merge)
                except (OSError, RuntimeError):
                    # Consent is valid for this session even if remembering failed.
                    pass
            self._consented = accepted
            return accepted
        finally:
            self._consent_dialog = None
            if dialog:
                dialog.Destroy()
            # EndModal only requests exit. The child ShowModal must return
            # before MSW can end the parent's now-active modal event loop.
            if self.session.closed and self:
                self._finish_close()

    def _busy(self, busy):
        if busy:
            self._processing.start()
        else:
            self._processing.stop()
        self.ask.Enable(not busy)
        self.regenerate.Enable(not busy)
        self.cancel.Enable(busy)
        self.copy.Enable(bool(self._latest))

    def _key(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self._close()
            return
        if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and event.ControlDown():
            if wx.Window.FindFocus() is self.question:
                self._ask()
                return
        event.Skip()  # Plain Enter remains a newline in the question editor.

    def _ask(self, event=None):
        self._start(self.question.GetValue().strip())

    def _show_error(self, key):
        text = self.i18n.t(key)
        self.status.ChangeValue(text)
        record("ui_error", category=key.removeprefix("ai_error_"))
        if self._valid() and self.main_window.IsShown():
            self.main_window.output(text)

    def _regenerate(self, event=None):
        self._start(self.i18n.t("ai_describe_prompt"), regenerate=True)

    def _start(self, question, regenerate=False):
        if not self._valid() or self.session.active is not None:
            return
        if not question or len(question) > MAX_QUESTION_CHARS:
            self._show_error("ai_error_question")
            return
        try:
            key = CredentialStore(global_dir()).get(self.config["provider"])
            if not key:
                raise DescriptionError("credentials")
            if not self._consent() or not self._valid():
                return
            generation, token = self.session.begin(question)
            record("ui_start", generation=generation)
            self._busy(True)
            self.status.ChangeValue(self.i18n.t("ai_description_loading") if regenerate
                                    else self.i18n.t("ai_question_loading"))
            image = self.session.image
            history = () if regenerate else tuple(self.session.history)
            config = dict(self.config)
            language = self.i18n.get_language()
            loader = self.loader
            def work():
                token.check()
                record("image_load_started", generation=generation)
                prepared = image or prepare_image(loader(token), config["profile"])
                record("image_ready", generation=generation)
                token.check()
                answer = request_answer(config["provider"], config["model"], key, prepared,
                                        history, question, instructions(language, config["profile"]),
                                        config["profile"], token)
                return prepared, answer
            # Arm the GUI watchdog BEFORE dispatch; even a failed/very fast
            # worker callback must never leave an unprotected busy state.
            self._timer = wx.CallLater(60_000, self._timeout, generation)
            record("ui_waiting", generation=generation)
            submit(work, lambda result, error: wx.CallAfter(self._complete, generation, question,
                                                          regenerate, result, error))
        except (DescriptionError, CredentialError) as exc:
            self._cancel()
            self._show_error(str(exc))
        except Exception as exc:
            record("ui_error", category="response", exception_type=type(exc).__name__)
            self._cancel()
            self._show_error("ai_error_response")

    def _complete(self, generation, question, regenerate, result, error):
        if not self._valid() or generation != self.session.generation or self.session.active is None:
            record("ui_completion_ignored", generation=generation)
            return
        record("ui_completed", generation=generation)
        try:
            self.session.active.check()
        except DescriptionError as exc:
            self._cancel()
            self._show_error(str(exc))
            return
        if self._timer:
            self._timer.Stop()
            self._timer = None
        if error:
            self.session.accept(generation, question, None)
            self._busy(False)
            self._show_error(error)
        else:
            image, answer = result
            if regenerate:
                self.session.history.clear()
            if not self.session.accept(generation, question, answer):
                return
            if regenerate:
                self._description_first = True
            self.session.image = image
            self._latest = answer
            # Stop/free the waiting stream before updating or speaking. The
            # latest answer must already exist when Copy is re-enabled.
            self._busy(False)
            # One update per complete response, never token-by-token. Keep the
            # input's current value/focus: the user may be writing the next question.
            text = render_history(self.session.history, description_first=self._description_first,
                                  question_label=self.i18n.t("ai_history_question"),
                                  answer_label=self.i18n.t("ai_history_answer"))
            self.result.ChangeValue(text)
            if not regenerate and self.question.GetValue().strip() == question:
                self.question.ChangeValue("")
            self.status.ChangeValue(self.i18n.t("ai_ready"))
            if self.main_window.IsShown():
                self.main_window.output(spoken_answer(answer) if self.config["read_answers"] else self.i18n.t("ai_ready"))

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
        self.loader = None
        self._latest = ""
        if self._timer:
            self._timer.Stop()
            self._timer = None

    def _close(self, event=None):
        self._dispose()
        self.result.ChangeValue("")
        self.question.ChangeValue("")
        self.status.ChangeValue("")
        # Clear private state immediately, but unwind nested modal loops
        # inside-out. Ending the parent in the child's active loop leaves
        # the main window disabled on MSW.
        if self._consent_dialog is not None:
            if self._consent_dialog and self._consent_dialog.IsModal():
                self._consent_dialog.EndModal(wx.ID_CANCEL)
            return  # _consent's finally finishes the outer close.
        self._finish_close()

    def _finish_close(self):
        if self.IsModal():
            record("ui_close_finished")
            self.EndModal(wx.ID_CANCEL)

    def _destroyed(self, event):
        if event.GetEventObject() is self:
            self._dispose()
        event.Skip()
