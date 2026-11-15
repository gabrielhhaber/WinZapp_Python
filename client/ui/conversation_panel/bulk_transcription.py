"""BulkTranscriptionMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Transcribing the selected messages in one go (issue #469), in the two ways a
single message can be transcribed:

* like Alt+Shift+T — every result is kept with its message and, at the end,
  all of them open together in the result window (Ctrl+Alt+Shift+T);
* like Alt+Shift+N — every result goes into its message's line, and the end is
  one sentence (Ctrl+Alt+Shift+N).

Reached like every other bulk message action: the "Ações em massa" submenu,
their own shortcuts (customisable in Settings > Shortcuts), and — when Settings
> User Interface > "Substituir atalhos por ações em massa..." is on and
messages are selected — the single-message keys themselves (Alt+Shift+T, and
Alt+Shift+N when it transcribes).

* **The plan is said and agreed first**: how many will be run, how many are
  skipped (nothing to transcribe) and how many already have a transcription
  for the selected model (reused, never run again).
* **One after another, in the background**, oldest first, on one worker
  thread: the conversation stays usable. Each start is announced ("message 3
  of 12"). Each result is kept with its message as an inline transcription —
  the row then reads it — exactly as Alt+Shift+N keeps one (and the cache
  settings apply: a model with the cache off keeps nothing).
* **A failure is counted and the queue goes on**, except a failure that means
  "not set up" (no model, no program), which would fail every message the same
  way: that one stops the queue and is said once.
* **Cancellable**: the same shortcut (or the submenu) while it runs asks
  whether to cancel; the message being transcribed stops at its next check.
* **Locked chats**: if the vault hides the chat while the queue runs, it stops
  and says nothing about the messages.
* At the end one sentence gives the tally, and the context menu offers every
  transcription of the run together in one window (copy all, save as text).

Nothing here logs the text, the contact or a message id.
"""

import logging
import threading
import time

import wx

from app_paths import data_path
from coord_locks import LockTimeout
from core.transcription import auto_queue, bulk, errors, external_models, message_run
from core.transcription import preferences as transcription_preferences
from core.transcription import stored as stored_transcription


#: The worker's "the vault hid the chat" ending, told apart from any sentence.
_LOCKED = object()


class _BulkRun:
    """The state of one bulk transcription, shared with its worker thread."""

    def __init__(self, jid, order, to_run, skipped, inline):
        self.jid = jid
        #: Alt+Shift+N's way (into the lines) rather than Alt+Shift+T's (window).
        self.inline = inline
        #: (msg, saved text or None), oldest first.
        self.order = order
        self.total = to_run
        self.index = 0
        self.tally = bulk.BulkTally(skipped=skipped)
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.current = None
        self.running = True
        #: bulk.entry() dicts, oldest first.
        self.entries = []
        #: Results the cache did not keep (the model's cache is off).
        self.not_kept = 0

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            current = self.current
        if current is not None:
            current.cancel()


class BulkTranscriptionMixin:
    """Transcribe the selected messages, for ConversationsPanel."""

    # ── Entry points ─────────────────────────────────────────────────────────

    def _bulk_transcription_running(self) -> bool:
        run = getattr(self, "_bulk_transcription", None)
        return run is not None and run.running

    def _cancel_bulk_transcription_now(self):
        """Quitting: cancel a bulk run with no question asked; the transcription
        it is running (to wait for), or None."""
        run = getattr(self, "_bulk_transcription", None)
        if run is None or not run.running:
            return None
        run.cancel()
        with run.lock:
            return run.current

    def _on_accel_bulk_transcribe(self, event):
        """Ctrl+Alt+Shift+T: transcribe the selected messages and show them
        together, or offer to cancel the run under way."""
        self._bulk_transcription_key(self._on_mass_transcribe_messages, event)

    def _on_accel_bulk_transcribe_inline(self, event):
        """Ctrl+Alt+Shift+N: transcribe the selected messages into their
        lines, or offer to cancel the run under way."""
        self._bulk_transcription_key(self._on_mass_transcribe_inline_messages, event)

    def _bulk_transcription_key(self, handler, event):
        if self._bulk_transcription_running():
            self._ask_cancel_bulk_transcription()
            return
        self._run_bulk_message_action(handler, event)

    def _on_mass_transcribe_messages(self, event=None):
        """Alt+Shift+T's bulk form: kept, then shown together in the window."""
        self._start_bulk_transcription(inline=False)

    def _on_mass_transcribe_inline_messages(self, event=None):
        """Alt+Shift+N's bulk form: each one put in its message's line."""
        self._start_bulk_transcription(inline=True)

    def _start_bulk_transcription(self, inline):
        i18n = self.main_window.i18n
        if self._bulk_transcription_running():
            self._ask_cancel_bulk_transcription()
            return
        if self._summary_voice_running():
            self.main_window.output(i18n.t("ai_summary_voice_running"), interrupt=True)
            return
        if not self.selected_messages or not self.conversation:
            self.main_window.output(i18n.t("bulk_no_message_selection"), interrupt=True)
            return
        selection = stored_transcription.selection_from_settings(self.main_window.settings)
        the_plan = bulk.plan(self._sorted_messages, self.selected_messages, selection,
                             self._is_separator)
        if not the_plan.total:
            self.main_window.output(i18n.t("bulk_transcription_none"), interrupt=True)
            return
        text = i18n.t("bulk_transcription_confirm").format(
            count=len(the_plan.to_run), already=len(the_plan.already),
            skipped=the_plan.skipped)
        if wx.MessageBox(text, i18n.t("bulk_transcription_title"),
                         wx.YES_NO | wx.ICON_QUESTION, self) != wx.YES:
            return
        jid = self.conversation.get("remoteJid", "")
        running = {id(m) for m in the_plan.to_run}
        already = {id(m) for m in the_plan.already}
        order = []
        for msg in self._sorted_messages:
            if id(msg) in running:
                order.append((msg, None))
            elif id(msg) in already:
                saved = stored_transcription.saved_transcription(msg, selection)
                order.append((msg, (saved or {}).get("text") or None))
        run = _BulkRun(jid, order, len(the_plan.to_run), the_plan.skipped, inline)
        self._bulk_transcription = run
        # Like the other bulk actions: the selection is spent.
        done_ids = list(self.selected_messages)
        self.selected_messages.clear()
        self._refresh_message_rows_by_ids(done_ids)
        if inline:
            self._mark_bulk_saved_inline(jid, [msg for msg, saved in order if saved])
        threading.Thread(target=self._bulk_transcription_worker, args=(run,),
                         daemon=True, name="winzapp-bulk-transcription").start()

    def _mark_bulk_saved_inline(self, jid, messages):
        """Alt+Shift+N on a message that already has a transcription puts it in
        the line without a run; the bulk form does the same for each one."""
        selection = stored_transcription.selection_from_settings(self.main_window.settings)
        changed = []
        for msg in messages:
            value = stored_transcription.saved_transcription(msg, selection)
            if value is None or stored_transcription.is_inline(value):
                continue
            previous = msg.get(stored_transcription.TRANSCRIPTION_KEY)
            msg_id = (msg.get("key") or {}).get("id", "")
            if self.main_window.store_message_transcription(
                    jid, msg_id, stored_transcription.marked_inline(value, previous, time.time()),
                    existing=True,
            ) == stored_transcription.SAVE_STORED:
                changed.append(msg_id)
        if changed:
            self._repaint_or_repopulate(changed)

    def _bulk_transcription_menu_key(self):
        """The context menu's item about the bulk transcription, or None: cancel
        it while it runs, show its texts once it has ended (for the
        conversation it ran in)."""
        run = getattr(self, "_bulk_transcription", None)
        if run is None:
            return None
        if run.running:
            return "bulk_transcription_cancel_menu"
        conversation = self.conversation if isinstance(self.conversation, dict) else {}
        if run.entries and conversation.get("remoteJid", "") == run.jid:
            return "bulk_transcription_show_menu"
        return None

    def _ask_cancel_bulk_transcription(self):
        run = getattr(self, "_bulk_transcription", None)
        if run is None or not run.running:
            return
        i18n = self.main_window.i18n
        text = i18n.t("bulk_transcription_cancel_confirm").format(
            index=run.index, total=run.total)
        if wx.MessageBox(text, i18n.t("bulk_transcription_title"),
                         wx.YES_NO | wx.ICON_QUESTION, self) == wx.YES:
            run.cancel()

    def _on_show_bulk_transcriptions(self, event=None, spoken=""):
        """The context menu's "all the transcriptions together": the same
        result window as a single transcription (copy, save as text, insert)."""
        run = getattr(self, "_bulk_transcription", None)
        if run is None or run.running or not run.entries:
            return
        mw = self.main_window
        if mw.is_chat_hidden_by_vault(run.jid):
            return
        i18n = mw.i18n
        shown = self._bulk_shown_entries(run)
        if not shown:
            # The tally the window would have said, then why it did not open.
            nothing = i18n.t("bulk_transcription_nothing_to_show")
            mw.output(f"{spoken} {nothing}" if spoken else nothing, interrupt=not spoken)
            return
        text = bulk.combined_text(
            shown, self._sender_label, self._extract_timestamp, i18n.t("datetime_fmt"))
        from ui.dialogs.transcription_result import TranscriptionResultDialog, default_file_name
        from ui.transcription_flow import insertion_text

        dialog = TranscriptionResultDialog(
            mw, mw, i18n.t("bulk_transcription_window_title"), text,
            spoken=spoken, default_file=default_file_name(i18n, ""))
        try:
            dialog.run()
            insert = dialog.insert_requested
        finally:
            dialog.Destroy()
        conversation = self.conversation if isinstance(self.conversation, dict) else {}
        if (not insert or mw.is_chat_hidden_by_vault(run.jid)
                or conversation.get("remoteJid", "") != run.jid):
            return
        field = self.message_field
        start, end = field.GetSelection()
        if start != end:
            field.SetInsertionPoint(end)
        position = field.GetInsertionPoint()
        before = field.GetRange(position - 1, position) if position > 0 else ""
        piece = insertion_text(text, before, field.GetRange(position, position + 1))
        if piece:
            field.WriteText(piece)
        field.SetFocus()

    # ── The worker ───────────────────────────────────────────────────────────

    def _bulk_transcription_worker(self, run):
        i18n = self.main_window.i18n
        stop_sentence = None
        try:
            for msg, saved in run.order:
                if saved:
                    run.entries.append(bulk.entry(msg, saved, bulk.KEPT))
                    continue
                if run.cancelled.is_set():
                    break
                if self.main_window.is_chat_hidden_by_vault(run.jid):
                    stop_sentence = _LOCKED
                    break
                run.index += 1
                wx.CallAfter(self.main_window.output, i18n.t(
                    "bulk_transcription_progress").format(index=run.index, total=run.total))
                result, error = self._bulk_transcribe_one(run, msg)
                if error is not None:
                    if error.code == errors.CANCELLED:
                        break
                    run.tally.failed += 1
                    logging.info("[transcription] a bulk transcription item failed: %s",
                                 error.code)
                    if auto_queue.pauses_the_queue(error):
                        stop_sentence = i18n.t(errors.error_i18n_key(error.code)).format(
                            **errors.error_i18n_values(error.code))
                        break
                    continue
                if result is None or result.is_empty:
                    # No speech in it: nothing to keep or to show.
                    run.tally.skipped += 1
                    continue
                run.tally.transcribed += 1
                item = bulk.entry(msg, result.text)
                run.entries.append(item)
                wx.CallAfter(self._store_bulk_transcription, run.jid, msg, result,
                             run.inline, item)
        except Exception as exc:
            logging.error("[transcription] the bulk transcription stopped: %s",
                          type(exc).__name__)
            stop_sentence = i18n.t(errors.error_i18n_key(errors.BACKEND_ERROR))
        wx.CallAfter(self._finish_bulk_transcription, run, stop_sentence)

    def _bulk_transcribe_one(self, run, msg):
        """(result, error) of one message, on the worker thread."""
        outcome = {}

        def _finished(result, error):
            outcome["result"] = result
            outcome["error"] = error

        mw = self.main_window
        stored_models_dir, references = self._bulk_install_wide_models()
        job = message_run.MessageTranscription(
            msg, mw.settings, mw.key,
            data_path("voice_messages"), data_path("media"),
            stored_models_dir=stored_models_dir,
            ui_language=getattr(mw.i18n, "language", ""),
            external_references=references,
            find_ffmpeg=mw._find_api_ffmpeg,
            is_online=lambda: bool(getattr(mw, "_wa_connected", False)),
            fetch_media=self._download_media_to_disk,
            on_finished=_finished,
        )
        with run.lock:
            run.current = job
        if run.cancelled.is_set():
            job.cancel()
        try:
            job.start()
            job.join()
        finally:
            with run.lock:
                run.current = None
        error = outcome.get("error")
        if error is None and "result" not in outcome:
            error = errors.TranscriptionError(errors.BACKEND_ERROR, "no report")
        return outcome.get("result"), error

    def _bulk_install_wide_models(self):
        app_settings = getattr(self.main_window, "_app_settings", None)
        try:
            stored_models_dir = transcription_preferences.stored_models_dir(app_settings)
        except LockTimeout:
            return "", None
        references, known = external_models.read_references(app_settings)
        return stored_models_dir, references if known else None

    # ── Back on the wx thread ────────────────────────────────────────────────

    def _store_bulk_transcription(self, jid, msg, result, inline_run=False, item=None):
        if self.main_window.is_chat_hidden_by_vault(jid):
            # Queued by wx.CallAfter: the vault may have closed since the worker
            # produced it, and a locked chat's text is not stored while it is hidden.
            return
        msg_id = (msg.get("key") or {}).get("id", "")
        previous = msg.get(stored_transcription.TRANSCRIPTION_KEY)
        # Into the line for the Alt+Shift+N form; for the Alt+Shift+T form a
        # message already showing its text in the line keeps doing so, as a
        # single run from the window does.
        inline = bool(inline_run) or stored_transcription.is_inline(previous)
        value = stored_transcription.with_result(previous, stored_transcription.record_from_result(
            result, stored_transcription.next_decision_time(time.time(), previous), inline=inline))
        try:
            answer = self.main_window.store_message_transcription(jid, msg_id, value)
        except Exception as exc:
            logging.warning("[transcription] storing a bulk transcription failed: %s",
                            type(exc).__name__)
            answer = None
        if item is not None:
            # What the combined window may show of it later (bulk.shown_entries).
            item["kept"] = (bulk.KEPT if answer == stored_transcription.SAVE_STORED
                            else bulk.NOT_KEPT if answer == stored_transcription.SAVE_DISABLED
                            else bulk.REFUSED)
            if answer == stored_transcription.SAVE_DISABLED:
                run = getattr(self, "_bulk_transcription", None)
                if run is not None:
                    run.not_kept += 1
        if answer != stored_transcription.SAVE_STORED:
            return
        if self.main_window.is_chat_hidden_by_vault(jid):
            return
        conversation = self.conversation if isinstance(self.conversation, dict) else {}
        if conversation.get("remoteJid", "") == jid:
            try:
                self._repaint_or_repopulate([msg_id])
            except Exception as exc:
                logging.warning("[transcription] repainting a bulk transcription failed: %s",
                                type(exc).__name__)

    def _finish_bulk_transcription(self, run, stop_sentence):
        run.running = False
        i18n = self.main_window.i18n
        if stop_sentence is _LOCKED:
            # Nothing about the messages: the chat is hidden now.
            run.entries = []
            self.main_window.output(i18n.t("bulk_transcription_stopped_locked"))
            return
        tally = run.tally
        done = tally.transcribed + sum(1 for _msg, saved in run.order if saved)
        key = ("bulk_transcription_cancelled" if run.cancelled.is_set()
               else "bulk_transcription_done")
        text = i18n.t(key).format(done=done, skipped=tally.skipped, failed=tally.failed)
        if stop_sentence:
            # The reason first: it is what the user has to act on.
            text = f"{stop_sentence} {text}"
        if run.not_kept:
            # The model's cache is off: the texts were made but not kept.
            text = f"{text} {i18n.t('bulk_transcription_not_kept')}"
        if (not run.inline and not run.cancelled.is_set()
                and self._bulk_shown_entries(run)):
            # Alt+Shift+T's form ends where Alt+Shift+T does, in the window —
            # but only where the user still is: the conversation the run was
            # started in, with WinZapp's window active. The tally is then said
            # by the window, after its own announcement, rather than cut off
            # by the focus moving into it. Anywhere else the window would land
            # on someone typing in another conversation: it is offered instead.
            if self._bulk_window_may_open(run):
                wx.CallAfter(self._on_show_bulk_transcriptions, None, text)
                return
            text = f"{text} {i18n.t('bulk_transcription_open_hint')}"
        self.main_window.output(text)

    def _bulk_shown_entries(self, run) -> list:
        """bulk.shown_entries() of `run`, read from what memory holds now."""
        mw = self.main_window
        return bulk.shown_entries(
            run.entries,
            lambda msg_id: stored_transcription.find_record(
                mw._transcription_copies(run.jid, msg_id), msg_id),
            stored_transcription.selection_from_settings(mw.settings))

    def _bulk_window_may_open(self, run) -> bool:
        conversation = self.conversation if isinstance(self.conversation, dict) else {}
        if conversation.get("remoteJid", "") != run.jid:
            return False
        is_active = getattr(self.main_window, "IsActive", None)
        try:
            return bool(is_active()) if callable(is_active) else False
        except Exception:
            return False
