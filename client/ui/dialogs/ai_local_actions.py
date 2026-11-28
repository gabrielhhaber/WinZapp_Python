"""AIResultDialog's on-this-computer actions: Windows OCR for pictures and the
text layer of a PDF. Nothing here asks for consent because nothing leaves the
machine; the dialog's session, watchdog and focus rules are the ones its online
requests use (ai_result_dialog.py).

Methods run with ``self`` bound to the AIResultDialog.
"""
import wx

from core.ai_media.errors import DescriptionError
from core.ai_media.local_text import ocr_image, pdf_native_text
from core.ai_media.payload import prepare_media
from core.ai_media.service import submit
from core.ai_media.transcript import render_history, spoken_answer

#: Failures that end the action; any other failure of reading a PDF's own text
#: falls back to asking the providers, which may well read the file.
_FINAL_ERRORS = ("ai_error_media", "ai_error_media_size", "ai_error_cancelled", "ai_error_timeout")

_OCR_QUESTION = "Read the text in this picture."
_PDF_QUESTION = "Read the text of this PDF."


class AILocalActionsMixin:
    def _local_begin(self, question, loading_key):
        """Open one local action: its generation, busy state and status.
        Returns (generation, operation) or None when it cannot start."""
        if not self._valid() or self.session.active is not None:
            return None
        try:
            generation, operation = self.session.begin(question)
        except DescriptionError as exc:
            self._show_error(exc.key)
            return None
        self._busy(True)
        loading = self.i18n.t(loading_key)
        self.status.ChangeValue(loading)
        self._speak(loading)
        return generation, operation

    def _local_run(self, generation, operation, work, complete):
        """Run ``work`` on the worker; ``complete(generation, result, error)``
        comes back on the GUI thread. The watchdog is armed first, as for an
        online request."""
        self._timer = wx.CallLater(int((operation.total_seconds + 5) * 1000),
                                   self._timeout, generation)
        try:
            submit(work, lambda result, error: wx.CallAfter(complete, generation, result, error))
        except DescriptionError as exc:
            self._cancel()
            self._show_error(exc.key)
        except Exception:
            self._cancel()
            self._show_error("ai_error_response")

    def _local_finish(self, generation):
        """The checks every completion shares; False when it must be ignored."""
        if not self._valid() or generation != self.session.generation or self.session.active is None:
            return False
        try:
            self.session.active.check()
        except DescriptionError as exc:
            self._cancel()
            self._show_error(exc.key)
            return False
        if self._timer:
            self._timer.Stop()
            self._timer = None
        return True

    def _show_local_answer(self, generation, question, media, text, status_text):
        """Make ``text`` the window's first answer (follow-ups and "Run again"
        then behave as after an online one) and say where it came from."""
        self.session.history.clear()
        if not self.session.accept(generation, question, text):
            return
        self.session.media = media
        self._description_first = True
        self._latest = text
        self._busy(False)
        self.result.ChangeValue(render_history(
            self.session.history, description_first=True,
            question_label=self.i18n.t("ai_history_question"),
            answer_label=self.i18n.t("ai_history_answer")))
        self.status.ChangeValue(status_text)
        self._speak(spoken_answer(text) if self.config["read_answers"] else status_text)

    # ── Windows OCR on a picture ─────────────────────────────────────────

    def _read_offline(self, event=None):
        started = self._local_begin(_OCR_QUESTION, "ai_offline_reading")
        if started is None:
            return
        generation, operation = started
        media, kind, mime, loader = self.session.media, self.kind, self.mime, self.loader
        profile = self.config["profile"]
        frames = self.config.get("video_frames", "several")

        def work():
            operation.check()
            prepared = media or prepare_media(kind, loader(operation), mime, profile, frames)
            operation.check()
            return prepared, ocr_image(prepared.data)
        self._local_run(generation, operation, work, self._offline_done)

    def _offline_done(self, generation, result, error):
        if not self._local_finish(generation):
            return
        if error is not None:
            self.session.accept(generation, _OCR_QUESTION, None)
            self._busy(False)
            self._show_error(error)
            return
        media, text = result
        if not text:
            self.session.accept(generation, _OCR_QUESTION, None)
            self.session.media = media
            self._busy(False)
            empty = self.i18n.t("ai_offline_no_text")
            self.status.ChangeValue(empty)
            self._speak(empty)
            return
        self._show_local_answer(generation, _OCR_QUESTION, media, text,
                                self.i18n.t("ai_offline_ready"))

    # ── The text layer of a PDF ──────────────────────────────────────────

    def _start_pdf_local(self):
        """A PDF's own text first: free, instant and private. A scanned PDF
        (no text layer) goes on to the providers as before."""
        started = self._local_begin(_PDF_QUESTION, "ai_pdf_reading_local")
        if started is None:
            return
        generation, operation = started
        kind, mime, loader = self.kind, self.mime, self.loader

        def work():
            operation.check()
            prepared = prepare_media(kind, loader(operation), mime)
            operation.check()
            return prepared, pdf_native_text(prepared.data, operation.check)
        self._local_run(generation, operation, work, self._pdf_local_done)

    def _pdf_local_done(self, generation, result, error):
        if not self._local_finish(generation):
            return
        if error is not None:
            self.session.accept(generation, _PDF_QUESTION, None)
            self._busy(False)
            if error in _FINAL_ERRORS:
                self._show_error(error)
            else:
                self._regenerate()
            return
        media, native = result
        if not native.usable:
            self.session.accept(generation, _PDF_QUESTION, None)
            self.session.media = media
            self._busy(False)
            scanned = self.i18n.t("ai_pdf_scanned")
            self.status.ChangeValue(scanned)
            self._speak(scanned)
            self._regenerate()
            return
        notes = [self.i18n.t("ai_pdf_local_ready")]
        if native.pages_without_text:
            notes.append(self.i18n.t("ai_pdf_local_partial").format(
                count=native.pages_without_text, total=native.pages))
        if not native.complete:
            notes.append(self.i18n.t("ai_pdf_local_incomplete").format(pages=native.pages))
        self._show_local_answer(generation, _PDF_QUESTION, media, native.text, " ".join(notes))
