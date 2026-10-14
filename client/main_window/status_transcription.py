"""StatusTranscriptionMixin — part of MainWindow (see main_window/__init__.py).

The window's half of transcribing status updates on this computer: keeping the
text with the status, deleting it, clearing every one, fetching a status's
media for a run, and the automatic transcription of statuses as they arrive.
The rules are core/transcription/statuses.py; the keys and the row are
status_tab/status_transcription.py; the flow is ui/status_transcription_flow.py.

The text lives in the status's own record (the ``status_updates`` table), so
the periodic prune of expired statuses takes it away with the status. There is
no second store to keep in step and nothing to clean up afterwards.

Nothing here logs a status id, a poster or a word of the text.

Methods run with ``self`` bound to the MainWindow instance.
"""

import logging
import os
import threading
import time

import wx

from core.transcription import _fileops, message_run, statuses
from core.transcription import stored as stored_transcription
from core.utils import encrypt
from status_tab.status_rules import _download_status_media


class StatusTranscriptionMixin:
    """Stored transcriptions of statuses, and their automatic runs."""

    # ── The copies ───────────────────────────────────────────────────────────

    def _status_panel_get(self):
        navigation = getattr(self, "navigation_panel", None)
        return getattr(navigation, "status_panel", None)

    def _cached_status_records(self, msg_id: str) -> list:
        """(participant, record) for every cached record of this status."""
        found = []
        for participant, bucket in list(getattr(self, "_status_updates", {}).items()):
            for record in bucket or ():
                if isinstance(record, dict) and statuses.status_id(record) == msg_id:
                    found.append((participant, record))
        return found

    def _shown_status_copies(self, msg_id: str) -> list:
        """The Status tab's own dicts for this status (it builds its list from
        the API's answer first, so they are often not the cached records)."""
        panel = self._status_panel_get()
        if panel is None:
            return []
        lists = [entry.get("statuses") for entry in getattr(panel, "_status_contacts", ())
                 if isinstance(entry, dict)]
        lists.append(getattr(panel, "_my_statuses", ()))
        lists.append([getattr(panel, "_current_status", None)])
        return statuses.records_with_id(lists, msg_id)

    def _persist_status_records(self, pairs) -> None:
        """Write (participant, record) pairs back, off the wx thread."""
        pairs = [(participant, record) for participant, record in pairs if participant]
        if not pairs:
            return

        def _write():
            for participant, record in pairs:
                try:
                    self.db.upsert_status_update(participant, record)
                except Exception as exc:
                    logging.warning("[transcription] a status transcription was not "
                                    "written: %s", type(exc).__name__)

        threading.Thread(target=_write, daemon=True,
                         name="winzapp-status-transcription").start()

    # ── Keeping, deleting, clearing ──────────────────────────────────────────

    def store_status_transcription(self, status: dict, value: dict,
                                   existing: bool = False) -> str:
        """Keep `value` with the status; one of stored's SAVE_* answers.

        `existing` is a text already kept that is only being marked (shown in
        the line): the cache switch is about keeping new ones.
        """
        msg_id = statuses.status_id(status)
        if not msg_id:
            return stored_transcription.SAVE_MISSING
        if not existing and not statuses.may_cache(self.settings, value):
            return stored_transcription.SAVE_DISABLED
        cached = self._cached_status_records(msg_id)
        if not cached:
            # Shown from the API's answer and never cached: it becomes a
            # cached status like any other, pruned when it expires.
            # One of the account's own statuses names no poster: it is filed
            # under the account, as _store_status_update() files it.
            participant = statuses.participant_of(status) or (
                (status.get("key") or {}).get("fromMe") and getattr(self, "my_jid", "")) or ""
            if not participant:
                return stored_transcription.SAVE_MISSING
            if not hasattr(self, "_status_updates"):
                self._status_updates = {}
            self._status_updates.setdefault(participant, []).append(status)
            cached = [(participant, status)]
        statuses.set_on([record for _participant, record in cached], value)
        statuses.set_on([status], value)
        statuses.set_on(self._shown_status_copies(msg_id), value)
        self._persist_status_records(cached)
        return stored_transcription.SAVE_STORED

    def delete_status_transcription(self, status: dict, on_done) -> None:
        """Delete the saved transcription of one status; `on_done(ok)` on the
        wx thread. A dated tombstone, like a message's: a stale copy still
        holding the text must lose to it."""
        msg_id = statuses.status_id(status)
        previous = status.get(statuses.KEY) if isinstance(status, dict) else None
        cached = self._cached_status_records(msg_id) if msg_id else []
        if not msg_id or previous is None:
            wx.CallAfter(on_done, False)
            return
        value = stored_transcription.tombstone(
            stored_transcription.next_decision_time(time.time(), previous))
        statuses.set_on([record for _participant, record in cached], value)
        statuses.set_on([status], value)
        statuses.set_on(self._shown_status_copies(msg_id), value)
        self._persist_status_records(cached)
        wx.CallAfter(on_done, True)

    def clear_status_transcriptions(self, on_done) -> None:
        """Delete the saved transcriptions of every status; `on_done(count)`
        on the wx thread, count being how many statuses held one."""
        decided_at = time.time()
        changed, seen = [], set()
        for participant, bucket in list(getattr(self, "_status_updates", {}).items()):
            for record in bucket or ():
                if not isinstance(record, dict) or not statuses.holds_text(record):
                    continue
                previous = record.get(statuses.KEY)
                value, _dropped = stored_transcription.drop_all(
                    previous, stored_transcription.next_decision_time(decided_at, previous))
                record[statuses.KEY] = value
                msg_id = statuses.status_id(record)
                statuses.set_on(self._shown_status_copies(msg_id), value)
                changed.append((participant, record))
                seen.add(msg_id)
        self._persist_status_records(changed)
        panel = self._status_panel_get()
        repaint = getattr(panel, "_repaint_status_row", None)
        if callable(repaint):
            for msg_id in seen:
                try:
                    repaint(msg_id)
                except Exception as exc:
                    logging.warning("[transcription] repainting a status row failed: %s",
                                    type(exc).__name__)
        wx.CallAfter(on_done, len(seen))

    # ── The media of a run ───────────────────────────────────────────────────

    def _fetch_status_media(self, status, media_path) -> bool:
        """Download the status's media to `media_path`, encrypted the way the
        run expects every local media file; whether it is there. On a worker
        thread, and silent: the run reports what it could not get."""
        try:
            data = _download_status_media(self, status)
            os.makedirs(os.path.dirname(media_path), exist_ok=True)
            with open(media_path, "wb") as target:
                target.write(encrypt(data, self.key))
        except Exception as exc:
            logging.info("[transcription] a status download raised: %s", type(exc).__name__)
        return os.path.isfile(media_path)

    # ── Automatic transcription of statuses as they arrive ───────────────────

    def _maybe_auto_transcribe_status(self, status: dict) -> None:
        """Queue a status that just arrived, if the setting asks for it. Cheap
        when it is off. Shares the queue of the received messages: one
        transcription at a time, whatever it is of."""
        if not statuses.auto_enabled(self.settings):
            return
        try:
            if not statuses.auto_eligible(status):
                return
            # A model chosen explicitly whose cache is off would run for
            # nothing: the result could not be kept in the line.
            _backend, model = stored_transcription.selection_from_settings(self.settings)
            if model != "auto" and not statuses.cache_enabled_for(self.settings, model):
                return
            msg_id = statuses.status_id(status)
            self._auto_transcription_queue_get().submit(
                (statuses.STATUS_JID, msg_id), (statuses.STATUS_JID, msg_id, status))
        except Exception as exc:
            # Whatever this does must never cost the arrival of a status.
            logging.warning("[transcription] could not queue an automatic status "
                            "transcription: %s", type(exc).__name__)

    def _run_status_auto_transcription(self, status: dict) -> None:
        """One automatic run, on the queue's worker thread."""
        if not statuses.auto_enabled(self.settings):
            return
        outcome = {}

        def _finished(result, error):
            outcome["result"] = result
            outcome["error"] = error

        stored_models_dir, references = self._auto_install_wide_models()
        with _fileops.private_temp_dir(statuses.TEMP_PREFIX) as folder:
            run = message_run.MessageTranscription(
                status,
                self.settings,
                self.key,
                folder,
                folder,
                stored_models_dir=stored_models_dir,
                ui_language=getattr(self.i18n, "language", ""),
                external_references=references,
                find_ffmpeg=self._find_api_ffmpeg,
                is_online=lambda: bool(getattr(self, "_wa_connected", False)),
                fetch_media=self._fetch_status_media,
                on_finished=_finished,
            )
            with self._auto_transcription_lock_get():
                self._auto_transcription_current = run
            queue = getattr(self, "_auto_transcription_queue", None)
            if queue is not None and queue.stopped:
                # Quitting got here first: its cancel missed this run.
                run.cancel()
            try:
                run.start()
                run.join()
            finally:
                with self._auto_transcription_lock_get():
                    self._auto_transcription_current = None
        error = outcome.get("error")
        result = outcome.get("result")
        if error is not None:
            self._auto_transcription_failed(error, getattr(run, "resolution", None))
            return
        self._auto_pause_notices_get().forget()
        if result is None or result.is_empty:
            return
        wx.CallAfter(self._store_status_auto_transcription, status, result)

    def _store_status_auto_transcription(self, status: dict, result) -> None:
        """Keep `result` as the status's inline transcription and redraw its row."""
        if not statuses.auto_enabled(self.settings):
            return
        if status.get(statuses.KEY) is not None:
            # The user got there first (a manual run, or a deletion).
            return
        value = stored_transcription.with_result(None, stored_transcription.record_from_result(
            result, stored_transcription.next_decision_time(time.time()), inline=True))
        try:
            answer = self.store_status_transcription(status, value)
        except Exception as exc:
            logging.warning("[transcription] storing an automatic status transcription "
                            "failed: %s", type(exc).__name__)
            return
        if answer != stored_transcription.SAVE_STORED:
            return
        panel = self._status_panel_get()
        repaint = getattr(panel, "_repaint_status_row", None)
        if callable(repaint):
            try:
                repaint(statuses.status_id(status))
            except Exception as exc:
                logging.warning("[transcription] repainting an automatic status "
                                "transcription failed: %s", type(exc).__name__)
