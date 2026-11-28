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
from core.ai_media import full_video
from core.ai_media.config import MAX_QUESTION_CHARS, PROVIDERS, TITLE_KEY, asks_questions
from core.ai_media.context import with_context
from core.ai_media.modes import MODES, offered as modes_offered
from core.ai_media.errors import DescriptionError
from core.ai_media.payload import prepare_media
from core.ai_media.prompts import first_question, full_video_instructions, instructions
from core.ai_media import usage as ai_usage
from core.ai_media.service import run_chain, submit
from core.ai_media.session import MediaSession
from core.ai_media.diagnostics import record
from core.ai_media.feedback import ProcessingCue
from core.ai_media.transcript import render_history, spoken_answer
from ui.accessible import AccessibleAskQuestion
from ui.dialogs.ai_full_video import AIFullVideoMixin, answer_notes
from ui.dialogs.ai_local_actions import AILocalActionsMixin
from ui.dialogs.ai_media_modes import AIMediaModesMixin
from ui.dialogs.ai_result_streaming import AIStreamingMixin
from ui.dialogs.ai_summary_parts import AISummaryPartsMixin

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
        text = i18n.t("ai_chain_failed") + "\n" + "\n".join(lines)
    else:
        text = i18n.t(str(error))
    part = getattr(error, "part", None)
    if part:
        # A hierarchical summary stopped at this part (core/ai_media/summary_parts.py).
        text = i18n.t("ai_summary_part_failed").format(part=part, total=error.total) + "\n" + text
    return text


def consent_text(i18n, providers, kind, locked, video_frames="several", config=None):
    """What the person is told before media leaves the machine."""
    if kind == "text":
        # A summary sends the readable text of the messages themselves, with
        # the senders' names: the media consent's "names are not sent" would
        # be false here, so it has its own wording (and its own locked one).
        message = i18n.t("ai_summary_consent").format(providers=provider_names(providers))
        for key in ai_config.notice_keys(providers, config):
            message += "\n\n" + i18n.t(key)
        if locked:
            message += "\n\n" + i18n.t("ai_summary_locked_consent")
        return message
    message = i18n.t("ai_consent").format(providers=provider_names(providers))
    if kind in ("audio", "pdf"):
        # Pictures are re-encoded (metadata removed); these go out untouched.
        message += "\n\n" + i18n.t("ai_metadata_notice")
    if kind == "video":
        message += "\n\n" + i18n.t(
            "ai_video_frame_notice" if video_frames == "one" else "ai_video_frames_notice")
    for key in ai_config.notice_keys(providers, config):
        message += "\n\n" + i18n.t(key)
    if locked:
        message += "\n\n" + i18n.t("ai_locked_consent")
    return message


class AIConsentDialog(wx.Dialog):
    """Asks before media leaves the machine; names every provider that may
    receive it, in the order they would be tried. Unlike the result window it
    carries mnemonics: its few controls are all reachable from the keyboard by
    letter."""

    def __init__(self, parent, i18n, providers, kind, locked, text=None, video_frames="several",
                 config=None):
        """``text`` replaces the computed message (the text-only windows name
        their own destination); the kind notices stay the media windows'."""
        super().__init__(parent, title=i18n.t("ai_consent_title"))
        layout = wx.BoxSizer(wx.VERTICAL)
        text = wx.TextCtrl(self, value=text if text is not None else consent_text(i18n, providers, kind, locked, video_frames, config), style=wx.TE_MULTILINE | wx.TE_READONLY,
                           size=(520, 220), name=plain(i18n, "ai_consent_title"))
        layout.Add(text, 1, wx.EXPAND | wx.ALL, 10)
        text_kind = kind == "text"
        self.remember = wx.CheckBox(self, label=i18n.t(
            "ai_summary_remember_consent" if text_kind else "ai_remember_consent"))
        self.remember.Enable(not locked)
        layout.Add(self.remember, 0, wx.ALL, 10)
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        buttons.Add(wx.Button(self, wx.ID_OK, label=i18n.t(
            "ai_summary_send" if text_kind else "ai_send_media")), 0, wx.ALL, 8)
        cancel = wx.Button(self, wx.ID_CANCEL, label=i18n.t("cancel"))
        buttons.Add(cancel, 0, wx.ALL, 8)
        layout.Add(buttons)
        self.SetSizerAndFit(layout)
        cancel.SetDefault()
        text.SetFocus()


def needs_consent(config, providers, kind, locked):
    """Whether the person must be asked before ``providers`` receive ``kind``:
    a provider without remembered consent, or a locked chat (asked every time)."""
    # Only what leaves this computer needs consent: a local model asks nothing
    # (not even for a locked chat), unless its address is a remote server.
    providers = ai_config.outbound(config, providers)
    if not providers:
        return False
    remembered = config[ai_config.consent_key(kind)]
    return locked or any(p not in remembered for p in providers)


def ask_consent(parent, i18n, app, config, providers, kind, locked, valid=lambda: True, track=None,
                text=None):
    """Show the consent window when one is needed and remember the answer;
    True when the media or text may be sent.

    Consent covers every provider the data may reach. Remembered per provider,
    never for a locked chat. ``valid`` is asked once the window closes (the
    vault or the app may have closed under it); ``track`` is told the open
    window, then None, so its owner can dismiss it while closing itself. ``text``
    replaces the usual wording (a summary's online voice transcription names
    its own messages and provider).
    """
    if not needs_consent(config, providers, kind, locked):
        return True
    providers = ai_config.outbound(config, providers)  # the consent names only these
    # Only a video's wording depends on the frames preference.
    frames = {"video_frames": config.get("video_frames", "several")} if kind == "video" else {}
    # The config only matters for the cloud-model notice of a local provider.
    if any(ai_config.cloud_model(config, p) for p in providers):
        frames["config"] = config
    dialog = (AIConsentDialog(parent, i18n, providers, kind, locked, **frames) if text is None
              else AIConsentDialog(parent, i18n, providers, kind, locked, text=text))
    if track is not None:
        track(dialog)
    try:
        accepted = dialog.ShowModal() == wx.ID_OK and valid()
        if accepted and dialog.remember.GetValue() and not locked:
            key = ai_config.consent_key(kind)
            def merge(old):
                return ai_config.remember_consent(old, kind, providers, locked)
            try:
                app.update("ai_media", merge)
                config[key] = sorted(set(config[key]) | set(providers))
            except (OSError, RuntimeError):
                # Consent is valid for this session even if remembering failed.
                pass
        return accepted
    finally:
        if track is not None:
            track(None)
        if dialog:
            dialog.Destroy()


class AIResultDialog(AIMediaModesMixin, AILocalActionsMixin, AIFullVideoMixin, AIStreamingMixin,
                     AISummaryPartsMixin, wx.Dialog):
    def __init__(self, panel, identity, kind, locked, config, app_settings, loader, mime,
                 notice="", preconsented=False, saved_text="", context="", parts=(),
                 full_video=None, offline_only=False, initial_action=None, initial_mode=None):
        self.panel = panel
        self._offline_only = bool(offline_only)
        #: "Understand full video" (ai_full_video.py): what the panel gives a
        #: video's window to transcribe its sound; None where it is not offered.
        self._full_hooks = full_video
        self._full_running = False
        self._full_cache = None
        #: Providers the person agreed to send the frames and the speech text to,
        #: in this window only (never remembered).
        self._full_consented = set()
        #: The speech text the answer on screen was made with (None for a plain
        #: description), and what the request under way carries.
        self._full_speech = None
        self._pending_speech = None
        self._pending_images_only = False
        self.full_video = None
        #: A long backlog's transcript in parts (ai_summary_parts.py); () otherwise.
        self._parts = tuple(parts)
        #: State of the answer being streamed (ai_result_streaming.py), or None.
        self._stream = None
        #: Caption and/or quoted message to send with the opening description
        #: (core/ai_media/context.py); "" unless the person switched them on.
        self._context = context
        self._context_consented = False
        #: The answer on screen came from a quick analysis mode, not a description.
        self._task_answer = False
        self._pending_task = False
        #: A description already stored with the message: shown on opening
        #: instead of a new (paid) request.
        self._saved_text = saved_text
        #: What the caller says about the summary's input (messages left out,
        #: voice messages without words); shown above a summary and spoken with it.
        self.notice = notice
        self.main_window = panel.main_window
        self.i18n = self.main_window.i18n
        self.session = MediaSession(*identity, kind, locked=locked)
        self.kind = kind
        self.config = config
        self.app = app_settings
        self.loader = loader
        self.mime = mime
        #: The panel asked before it transcribed anything (summary_actions.py):
        #: asking again would be the same question twice, locked chat included.
        self._consented = preconsented
        self._consent_dialog = None
        self._latest = ""
        self._timer = None
        self._description_first = False
        self._provider = None
        self._processing = ProcessingCue(self.main_window)
        title = plain(self.i18n, "ai_read_text_offline") if self._offline_only else self.i18n.t(TITLE_KEY[kind])
        super().__init__(panel, title=title,
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
        self.mode = self.mode_run = None
        if not self._offline_only and modes_offered(kind):
            self._build_mode_controls(layout)
            if self.mode is not None and initial_mode in MODES:
                self.mode.SetSelection(MODES.index(initial_mode))
                self._mode_changed()
        self.question = None
        self.ask = None
        if not self._offline_only and asks_questions(kind):
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
        if self.mode is not None:
            self.mode_run = self._button(buttons, "ai_mode_run", self._run_mode)
        self.copy = self._button(buttons, "ai_copy_answer", self._copy)
        self.regenerate = None
        if not self._offline_only:
            self.regenerate = self._button(buttons, "ai_regenerate", self._regenerate)
        if not self._offline_only and self._full_hooks is not None:
            self._build_full_video_button(buttons)
        # A transcription or a description has a message line to go to; the
        # stored result is the offline transcription's own
        # (core/transcription/stored.py). A PDF's text is too long for a line.
        self.offline = None
        if kind in ("image", "sticker", "video"):
            # Windows OCR: the text in the picture, nothing sent anywhere.
            self.offline = self._button(buttons, "ai_read_text_offline", self._read_offline)
        self.inline = None
        if not self._offline_only and kind in ("audio", "image", "sticker", "video"):
            self.inline = self._button(
                buttons, "ai_put_in_line" if kind == "audio" else "ai_put_description_in_line",
                self._put_in_line)
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
        if initial_action == "offline_ocr" and self.offline is not None:
            wx.CallAfter(self._read_offline)
        elif initial_action == "full_video" and self._full_hooks is not None:
            wx.CallAfter(self._full_video)
        elif initial_mode and self.mode is not None and initial_mode in MODES:
            wx.CallAfter(self._run_mode)
        elif self._saved_text:
            wx.CallAfter(self._show_saved)
        elif self._offline_only:
            wx.CallAfter(self._read_offline)
        elif kind == "pdf":
            wx.CallAfter(self._start_pdf_local)
        else:
            wx.CallAfter(self._regenerate)

    def _show_saved(self):
        """Show the description stored with the message, with no request and no
        consent (nothing leaves the machine). It becomes the first answer, so a
        follow-up question or "Run again" behaves as after a fresh one."""
        if not self._valid() or self.session.active is not None or self.session.history:
            return
        saved = self._saved_text
        self.session.history.extend((("user", first_question(self.kind)), ("assistant", saved)))
        self._description_first = True
        self._latest = saved
        self._busy(False)
        self.result.ChangeValue(render_history(
            self.session.history, description_first=True,
            question_label=self.i18n.t("ai_history_question"),
            answer_label=self.i18n.t("ai_history_answer")))
        ready = self.i18n.t("ai_saved_ready")
        self.status.ChangeValue(ready)
        self._speak(spoken_answer(saved) if self.config["read_answers"] else ready)

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
        """Consent covers every provider the media may reach (ask_consent).
        Chat summaries (kind "text") keep their remembered consent apart from
        media's."""
        if self._consented or not needs_consent(self.config, providers, self.kind, self.session.locked):
            self._consented = True
            return True
        try:
            accepted = ask_consent(self, self.i18n, self.app, self.config, providers, self.kind,
                                   self.session.locked, valid=self._valid,
                                   track=lambda dialog: setattr(self, "_consent_dialog", dialog))
            self._consented = accepted
            return accepted
        finally:
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
        if self.regenerate is not None:
            self.regenerate.Enable(not busy)
        if getattr(self, "mode_run", None) is not None:
            self.mode_run.Enable(not busy)
        if getattr(self, "offline", None) is not None:
            self.offline.Enable(not busy)
        if getattr(self, "full_video", None) is not None:
            self.full_video.Enable(not busy)
        self.cancel.Enable(busy)
        self.copy.Enable(bool(self._latest))
        inline = getattr(self, "inline", None)
        if inline is not None:
            inline.Enable(not busy and bool(self._latest) and self._provider is not None
                          and not getattr(self, "_task_answer", False))

    def _key(self, event):
        event = command_key_event(self, 'ai_result', event)
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self._close()
            return
        if self._mode_enter(event):
            return
        if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and event.ControlDown():
            if self.question is not None and wx.Window.FindFocus() is self.question:
                self._ask()
                return
        event.Skip()  # Plain Enter remains a newline in the question editor.

    def _ask(self, event=None):
        # After "Understand full video" a follow-up still carries the speech text.
        speech = getattr(self, "_full_speech", None)
        self._start(self.question.GetValue().strip(), **({"speech": speech} if speech else {}))

    def _show_error(self, error):
        text = failure_text(self.i18n, error)
        self.status.ChangeValue(text)
        record("ui_error", category=str(error).removeprefix("ai_error_"))
        self._speak(text)

    def _regenerate(self, event=None, images_only=False):
        """Run again. After "Understand full video" that is the same request
        again; ``images_only`` is the plain description chosen instead of it."""
        speech = None if images_only else getattr(self, "_full_speech", None)
        if speech:
            self._start(full_video.QUESTION, regenerate=True, speech=speech)
            return
        context = getattr(self, "_context", "")
        self._start(with_context(first_question(self.kind), context), regenerate=True,
                    sends_context=bool(context), **({"images_only": True} if images_only else {}))

    def _start(self, question, regenerate=False, task=False, sends_context=False, speech=None,
               images_only=False):
        """``speech``: the video's speech text this request sends with its frames
        (ai_full_video.py), under a consent of its own; ``images_only``: this
        description stands in for "Understand full video" and says so."""
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
            # The speech text has a consent of its own: the description's does not cover it.
            accepted = (self._consent(providers) if speech is None
                        else self._full_video_consent(providers, speech))
            if accepted and sends_context and self._valid():
                accepted = self._consent_context(providers)
            if not accepted and speech is not None and self._valid():
                # The window keeps what it shows: only this request is dropped.
                self._full_video_say("ai_full_video_declined")
                return
            if not accepted and not self._latest and self._valid():
                # Declining the first prompt leaves nothing to show: close the
                # window rather than dropping focus into an empty result.
                self._close()
                return
            if not accepted or not self._valid():
                return
            in_parts = self._parts_run(regenerate, task)
            generation, operation = self.session.begin(
                question, local=ai_config.has_local(providers),
                steps=len(self._parts) + 1 if in_parts else 1)
            record("ui_start", generation=generation)
            self._busy(True)
            loading = (self._parts_loading() if in_parts else
                       self.i18n.t("ai_processing_msg" if regenerate else "ai_question_loading"))
            self.status.ChangeValue(loading)
            self._speak(loading)
            media = self.session.media
            history = () if regenerate else tuple(self.session.history)
            config = dict(self.config)
            kind, mime, loader = self.kind, self.mime, self.loader
            self._pending_task = task
            self._pending_speech, self._pending_images_only = speech, images_only
            on_partial = None if in_parts else self._stream_begin(generation, question, regenerate)
            streaming = {} if on_partial is None else {"on_partial": on_partial}
            noted = bool(getattr(self, "_context", ""))
            # The language the window shows, not re-read from settings:
            # self.i18n is the window's own instance, and get_language() on
            # it applies a language another account chose -- under this
            # dialog, with nothing repainted, and the switch the window had
            # pending then finds it "already applied" (see
            # MainWindow._apply_pending_language_switch()).
            language = self.i18n.language
            def work():
                operation.check()
                if in_parts:
                    return self._parts_work(operation, providers, store, config, language, generation)
                record("media_load_started", generation=generation)
                prepared = media or prepare_media(kind, loader(operation), mime, config["profile"],
                                                  config.get("video_frames", "several"))
                record("media_ready", generation=generation)
                operation.check()
                system = (instructions(language, config["profile"], kind,
                                       config.get("video_frames", "several"), task, noted)
                          if speech is None else
                          full_video_instructions(language, config["profile"],
                                                  config.get("video_frames", "several"),
                                                  speech["text"], speech["truncated"]))
                answer = run_chain(operation, providers, ai_config.models_for(config, kind),
                                   store.get, prepared, history,
                                   question, system,
                                   config["profile"], usage_sink=ai_usage.persist,
                                   endpoints=ai_config.endpoints(config), **streaming)
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
            self._stream_discard()
            self._busy(False)
            self._show_error(error)
        else:
            media, (answer, provider) = result
            if regenerate:
                self.session.history.clear()
            if not self.session.accept(generation, question, answer):
                return
            pending = getattr(self, "_pending_speech", None)
            if regenerate:
                self._description_first = True
                # An answer made with the speech text is not the message's
                # description: it is never put in the line (it would overwrite it).
                self._full_speech = pending
                self._task_answer = getattr(self, "_pending_task", False) or pending is not None
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
            heading = self._summary_heading()
            self._write_result(f"{heading}\n\n{text}" if heading else text)
            self._stream = None
            if self.question is not None and not regenerate and self.question.GetValue().strip() == question:
                self.question.ChangeValue("")
            notes = answer_notes(self.i18n.t, regenerate, pending,
                                 getattr(self, "_pending_images_only", False))
            ready = self.i18n.t("ai_ready") + " " + self.i18n.t("ai_answered_by").format(
                provider=PROVIDERS[provider].name) + notes
            self.status.ChangeValue(ready)
            spoken = spoken_answer(answer) + notes if self.config["read_answers"] else ready
            if regenerate and heading:
                spoken = f"{spoken} {heading}"
            self._speak(spoken)

    def _summary_heading(self):
        """For a summary: what the caller said about its input, then the
        warning that an AI wrote it. Not part of the answer: Copy leaves it out."""
        if self.kind != "text":
            return ""
        return "\n".join(part for part in (self.notice, self._parts_failed_notice(),
                                           self.i18n.t("ai_summary_disclaimer")) if part)

    def _timeout(self, generation):
        record("ui_watchdog", generation=generation)
        if self._valid() and generation == self.session.generation:
            self._cancel()
            self._show_error("ai_error_timeout")

    def _cancel(self, event=None):
        record("ui_cancel")
        self.session.cancel()
        self._stream_discard()
        if self._timer:
            self._timer.Stop()
            self._timer = None
        self._busy(False)
        self.status.ChangeValue(self.i18n.t("ai_cancel_notice"))

    def _inline_text(self):
        """The text that goes in the line. A transcription is the latest
        answer; a description is the first one — a follow-up question's answer
        describes the question, not the picture."""
        if self.kind == "audio":
            return spoken_answer(self._latest)
        for role, text in self.session.history:
            if role == "assistant":
                return spoken_answer(text)
        return spoken_answer(self._latest)

    def _put_in_line(self, event=None):
        """Keep the result with its message and show it in the message's own
        line of the list — what Alt+Shift+N does for an offline transcription."""
        if not self._valid() or not self._latest or self._provider is None:
            return
        import time
        from types import SimpleNamespace
        from core.transcription import stored as stored_transcription
        from ui import transcription_flow
        mw = self.main_window
        _, jid, msg_id = self.session.identity
        text = self._inline_text()
        copies = mw._transcription_copies(jid, msg_id)
        current = stored_transcription.find_record(copies, msg_id)
        previous = current.get(stored_transcription.TRANSCRIPTION_KEY) if current else None
        now = stored_transcription.next_decision_time(time.time(), previous)
        described = self.kind != "audio"
        record = stored_transcription.record_from_result(
            SimpleNamespace(text=text, language=None,
                            language_probability=None, model_id=self._provider,
                            backend=stored_transcription.ONLINE_BACKEND, vad_used=True),
            now, inline=True,
            kind=stored_transcription.DESCRIPTION_KIND if described else None)
        answer = mw.store_message_transcription(
            jid, msg_id, stored_transcription.with_result(previous, record))
        if answer == stored_transcription.SAVE_STORED:
            repaint = getattr(self.panel, "_repaint_or_repopulate", None)
            conversation = getattr(self.panel, "conversation", None)
            if (repaint is not None and isinstance(conversation, dict)
                    and conversation.get("remoteJid", "") == jid):
                repaint([msg_id])
            if described:
                self._speak(self.i18n.t("description_inline_spoken").format(text=text))
            else:
                self._speak(transcription_flow.inline_speech(self.i18n, text, ()))
        elif answer == stored_transcription.SAVE_WITHDRAWN:
            self._speak(self.i18n.t(transcription_flow.WITHDRAWN_I18N_KEY))
        else:
            key = transcription_flow.NOT_SAVED_I18N_KEYS.get(answer)
            if key:
                self.status.ChangeValue(self.i18n.t(key))
                self._speak(self.i18n.t(key))

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
        self._stream = None
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
        if getattr(self, "_full_running", False):
            # The transcription window is up: stop the run, and _full_video
            # finishes the close once that window has returned.
            self._full_video_cancel()
            return
        self._finish_close()

    def _finish_close(self):
        if self.IsModal():
            record("ui_close_finished")
            self.EndModal(wx.ID_CANCEL)

    def _destroyed(self, event):
        if event.GetEventObject() is self:
            self._dispose()
        event.Skip()
