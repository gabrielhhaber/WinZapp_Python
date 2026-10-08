"""Accessible RAM-only window for one AI action: a transcript, a description or a
converted PDF, with follow-up questions for pictures and videos.

Owned by the account's conversation panel (ui/conversation_panel/ai_actions.py),
which builds it, runs it modally and tears it down.
"""
from ui.shortcut_bindings import command_key_event
import wx

from app_paths import active_account_id, global_dir
from core.ai_credentials import CredentialStore, CredentialError
from core.ai_media import config as ai_config
from core.ai_media.config import MAX_QUESTION_CHARS, PROVIDERS, TITLE_KEY, asks_questions
from core.ai_media.errors import DescriptionError
from core.ai_media.payload import prepare_media
from core.ai_media.prompts import first_question, instructions
from core.ai_media.service import run_chain, submit
from core.ai_media.session import MediaSession
from core.ai_media.diagnostics import record
from core.ai_media.feedback import ProcessingCue
from core.ai_media.transcript import render_history, spoken_answer
from ui.accessible import AccessibleAskQuestion

#: Extra time over the request's own overall budget before the window gives up
#: waiting for a worker that never reported back.
_WATCHDOG_GRACE_SECONDS = 5


def plain(i18n, key):
    """A label without its mnemonic marker. Shortcuts of this window are
    announced by the controls' accessible objects (ui/accessible.py), never
    written into a label: a mnemonic also fires on the bare letter while a
    button has focus."""
    return i18n.t(key).replace("&", "")


def provider_names(providers):
    return ", ".join(PROVIDERS[p].name for p in providers)


def failure_text(i18n, error):
    """The sentence for a failed action. A chain that tried several providers
    lists each one's reason so the person can see what to fix."""
    attempts = getattr(error, "attempts", ())
    if len(attempts) > 1:
        lines = [i18n.t("ai_attempt_line").format(provider=PROVIDERS[p].name,
                                                  reason=i18n.t(f"ai_error_{category}"))
                 for p, category in attempts]
        return i18n.t("ai_chain_failed") + "\n" + "\n".join(lines)
    return i18n.t(str(error))


def consent_text(i18n, providers, kind, locked):
    """What the person is told before media leaves the machine."""
    message = i18n.t("ai_consent").format(providers=provider_names(providers))
    if kind in ("video", "audio", "pdf"):
        # Pictures are re-encoded (metadata removed); these go out untouched.
        message += "\n\n" + i18n.t("ai_metadata_notice")
    if "gemini" in providers:
        message += "\n\n" + i18n.t("ai_gemini_notice")
    if locked:
        message += "\n\n" + i18n.t("ai_locked_consent")
    return message


class AIConsentDialog(wx.Dialog):
    """Asks before media leaves the machine; names every provider that may
    receive it, in the order they would be tried. Unlike the result window it
    carries mnemonics: its few controls are all reachable from the keyboard by
    letter."""

    def __init__(self, parent, i18n, providers, kind, locked):
        super().__init__(parent, title=i18n.t("ai_consent_title"))
        layout = wx.BoxSizer(wx.VERTICAL)
        text = wx.TextCtrl(self, value=consent_text(i18n, providers, kind, locked), style=wx.TE_MULTILINE | wx.TE_READONLY,
                           size=(520, 220), name=plain(i18n, "ai_consent_title"))
        layout.Add(text, 1, wx.EXPAND | wx.ALL, 10)
        self.remember = wx.CheckBox(self, label=i18n.t("ai_remember_consent"))
        self.remember.Enable(not locked)
        layout.Add(self.remember, 0, wx.ALL, 10)
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        buttons.Add(wx.Button(self, wx.ID_OK, label=i18n.t("ai_send_media")), 0, wx.ALL, 8)
        cancel = wx.Button(self, wx.ID_CANCEL, label=i18n.t("cancel"))
        buttons.Add(cancel, 0, wx.ALL, 8)
        layout.Add(buttons)
        self.SetSizerAndFit(layout)
        cancel.SetDefault()
        text.SetFocus()


class AIResultDialog(wx.Dialog):
    def __init__(self, panel, identity, kind, locked, config, app_settings, loader, mime):
        self.panel = panel
        self.main_window = panel.main_window
        self.i18n = self.main_window.i18n
        self.session = MediaSession(*identity, kind, locked=locked)
        self.kind = kind
        self.config = config
        self.app = app_settings
        self.loader = loader
        self.mime = mime
        self._consented = False
        self._consent_dialog = None
        self._latest = ""
        self._timer = None
        self._description_first = False
        self._provider = None
        self._processing = ProcessingCue(self.main_window)
        super().__init__(panel, title=self.i18n.t(TITLE_KEY[kind]),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER, size=(680, 560))
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(wx.StaticText(self, label=self.i18n.t("status")), 0, wx.LEFT | wx.TOP, 8)
        self.status = wx.TextCtrl(self, style=wx.TE_READONLY, name=self.i18n.t("status"))
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        layout.Add(wx.StaticText(self, label=self.i18n.t("ai_result")), 0, wx.LEFT, 8)
        self.result = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE,
                                 name=self.i18n.t("ai_result"))
        layout.Add(self.result, 1, wx.EXPAND | wx.ALL, 8)
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        self.question = None
        self.ask = None
        if asks_questions(kind):
            layout.Add(wx.StaticText(self, label=self.i18n.t("ai_question")), 0, wx.LEFT, 8)
            self.question = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(-1, 75),
                                       name=self.i18n.t("ai_question"))
            self.question.SetMaxLength(MAX_QUESTION_CHARS)
            # Ctrl+Enter sends (see _key); the shortcut is announced by the
            # control's accessible object, never written into its label.
            self.question.SetAccessible(AccessibleAskQuestion())
            layout.Add(self.question, 0, wx.EXPAND | wx.ALL, 8)
            self.ask = self._button(buttons, "ai_ask", self._ask)
            self.ask.SetAccessible(AccessibleAskQuestion())
        self.copy = self._button(buttons, "ai_copy_answer", self._copy)
        self.regenerate = self._button(buttons, "ai_regenerate", self._regenerate)
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
        self.result.SetFocus()
        # ShowModal's event loop must start before consent, loading or errors.
        wx.CallAfter(self._regenerate)

    def _button(self, row, key, handler):
        button = wx.Button(self, label=plain(self.i18n, key))
        button.Bind(wx.EVT_BUTTON, handler)
        row.Add(button, 0, wx.ALL, 5)
        return button

    def _valid(self):
        return (not self.session.closed and active_account_id() == self.session.identity[0]
                and not getattr(self.main_window, "_shutting_down", False)
                and (not self.session.locked or getattr(self.main_window, "_chat_lock_unlocked", False)))

    def _speak(self, text):
        if self._valid() and self.main_window.IsShown():
            self.main_window.output(text)

    def _chain(self, store):
        """Providers to try now: the person's order, on, with a saved key and
        taking this kind; the one that answered first leads follow-ups."""
        saved = store.saved()
        return ai_config.chain(self.config, self.kind, lambda p: p in saved, prefer=self._provider)

    def _consent(self, providers):
        """Consent covers every provider the media may reach. Remembered per
        provider, never for a locked chat."""
        needed = [p for p in providers if p not in self.config["consented"]]
        if self._consented or (not needed and not self.session.locked):
            self._consented = True
            return True
        dialog = AIConsentDialog(self, self.i18n, providers, self.kind, self.session.locked)
        self._consent_dialog = dialog
        try:
            accepted = dialog.ShowModal() == wx.ID_OK and self._valid()
            if accepted and dialog.remember.GetValue() and not self.session.locked:
                def merge(old):
                    value = dict(old) if isinstance(old, dict) else {}
                    current = value.get("consented", [])
                    consented = set(current if isinstance(current, list) else [])
                    value["consented"] = sorted(consented | set(providers))
                    return value
                try:
                    self.app.update("ai_media", merge)
                    self.config["consented"] = sorted(set(self.config["consented"]) | set(providers))
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
        if self.ask is not None:
            self.ask.Enable(not busy)
        self.regenerate.Enable(not busy)
        self.cancel.Enable(busy)
        self.copy.Enable(bool(self._latest))

    def _key(self, event):
        event = command_key_event(self, 'ai_result', event)
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self._close()
            return
        if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and event.ControlDown():
            if self.question is not None and wx.Window.FindFocus() is self.question:
                self._ask()
                return
        event.Skip()  # Plain Enter remains a newline in the question editor.

    def _ask(self, event=None):
        self._start(self.question.GetValue().strip())

    def _show_error(self, error):
        text = failure_text(self.i18n, error)
        self.status.ChangeValue(text)
        record("ui_error", category=str(error).removeprefix("ai_error_"))
        self._speak(text)

    def _regenerate(self, event=None):
        self._start(first_question(self.kind), regenerate=True)

    def _start(self, question, regenerate=False):
        if not self._valid() or self.session.active is not None:
            return
        if not question or len(question) > MAX_QUESTION_CHARS:
            self._show_error("ai_error_question")
            return
        try:
            store = CredentialStore(global_dir())
            providers = self._chain(store)
            if not providers:
                raise DescriptionError("providers")
            accepted = self._consent(providers)
            if not accepted and not self._latest and self._valid():
                # Declining the first prompt leaves nothing to show: close the
                # window rather than dropping focus into an empty result.
                self._close()
                return
            if not accepted or not self._valid():
                return
            generation, operation = self.session.begin(question)
            record("ui_start", generation=generation)
            self._busy(True)
            loading = self.i18n.t("ai_processing_msg" if regenerate else "ai_question_loading")
            self.status.ChangeValue(loading)
            self._speak(loading)
            media = self.session.media
            history = () if regenerate else tuple(self.session.history)
            config = dict(self.config)
            kind, mime, loader = self.kind, self.mime, self.loader
            # The language the window shows, not re-read from settings:
            # self.i18n is the window's own instance, and get_language() on
            # it applies a language another account chose -- under this
            # dialog, with nothing repainted, and the switch the window had
            # pending then finds it "already applied" (see
            # MainWindow._apply_pending_language_switch()).
            language = self.i18n.language
            def work():
                operation.check()
                record("media_load_started", generation=generation)
                prepared = media or prepare_media(kind, loader(operation), mime, config["profile"])
                record("media_ready", generation=generation)
                operation.check()
                answer = run_chain(operation, providers, config["models"], store.get, prepared, history,
                                   question, instructions(language, config["profile"], kind),
                                   config["profile"])
                return prepared, answer
            # Arm the GUI watchdog BEFORE dispatch; even a failed/very fast
            # worker callback must never leave an unprotected busy state.
            self._timer = wx.CallLater(int((operation.total_seconds + _WATCHDOG_GRACE_SECONDS) * 1000),
                                       self._timeout, generation)
            record("ui_waiting", generation=generation)
            submit(work, lambda result, error: wx.CallAfter(self._complete, generation, question,
                                                          regenerate, result, error))
        except (DescriptionError, CredentialError) as exc:
            self._cancel()
            self._show_error(exc.key if isinstance(exc, DescriptionError) else str(exc))
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
            media, (answer, provider) = result
            if regenerate:
                self.session.history.clear()
            if not self.session.accept(generation, question, answer):
                return
            if regenerate:
                self._description_first = True
            self.session.media = media
            self._provider = provider
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
            if self.question is not None and not regenerate and self.question.GetValue().strip() == question:
                self.question.ChangeValue("")
            ready = self.i18n.t("ai_ready") + " " + self.i18n.t("ai_answered_by").format(
                provider=PROVIDERS[provider].name)
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
        self.loader = None
        self._latest = ""
        if self._timer:
            self._timer.Stop()
            self._timer = None

    def _close(self, event=None):
        self._dispose()
        self.result.ChangeValue("")
        if self.question is not None:
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
