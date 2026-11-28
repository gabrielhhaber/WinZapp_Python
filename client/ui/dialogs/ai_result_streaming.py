"""AIResultDialog's streamed answer: the result field fills a settled paragraph
at a time while a provider is still writing (core/ai_media/streaming.py).

Why it is this quiet, for a screen-reader user:

- The field is only rewritten with whole lines or sentences and at most once a
  second (``PartialPacer``), never a character at a time.
- Nothing is spoken per update. The window says once that the answer is
  arriving (stopping the waiting sound first) and the answer is read, as
  before, once it is complete. Several announcements of a growing text would
  talk over each other and over the person reading the field.
- The field is rewritten with ``ChangeValue`` (no focus change, no event
  handler) and the caret or selection the person moved is put back; text only
  ever grows at its end, so every position stays valid.
- A partial answer is never kept: a failure, a cancel or a timeout puts back
  what the field showed before the request.

Methods run with ``self`` bound to the AIResultDialog.
"""
import wx

from core.ai_media.streaming import STREAM_KINDS, PartialPacer
from core.ai_media.transcript import render_history


def stream_base(history, question, *, regenerate, description_first, heading, question_label,
                answer_label):
    """What the field shows ahead of the streamed answer: exactly the final
    layout up to where the answer begins, so the finished answer replaces the
    streamed text without moving anything the person was reading."""
    pending = (() if regenerate else tuple(history)) + (("user", question), ("assistant", ""))
    text = render_history(pending, description_first=True if regenerate else description_first,
                          question_label=question_label, answer_label=answer_label)
    return f"{heading}\n\n{text}" if heading else text


class AIStreamingMixin:
    def _stream_begin(self, generation, question, regenerate):
        """The ``on_partial`` for this request, or None when streaming is off
        or this window's kind is not plain text (a voice message)."""
        if self.config.get("ai_streaming") is not True or self.kind not in STREAM_KINDS:
            return None
        original = self.result.GetValue()
        base = stream_base(
            self.session.history, question, regenerate=regenerate,
            description_first=self._description_first, heading=self._summary_heading(),
            question_label=self.i18n.t("ai_history_question"),
            answer_label=self.i18n.t("ai_history_answer"))
        # A regenerated answer replaces the field: its caret is not kept.
        self._stream = {"generation": generation, "base": base, "original": original,
                        "written": "" if regenerate else original, "announced": False}
        pacer = PartialPacer()

        def on_partial(text, final=False):
            # Worker thread. The complete answer arrives through _complete.
            if final:
                return
            shown = pacer.offer(text)
            if shown is not None:
                wx.CallAfter(self._stream_show, generation, shown)
        return on_partial

    def _stream_show(self, generation, shown):
        state = self._stream
        if (state is None or state["generation"] != generation or not self._valid()
                or generation != self.session.generation or self.session.active is None):
            return
        self._write_result(state["base"] + shown)
        if shown and not state["announced"]:
            state["announced"] = True
            # The waiting sound ends before the first words, as for any answer.
            self._processing.stop()
            arriving = self.i18n.t("ai_streaming_arriving")
            self.status.ChangeValue(arriving)
            self._speak(arriving)

    def _write_result(self, text):
        """Set the result field, keeping the caret or selection the person
        moved while the answer was streaming (only when the new text still
        begins with what the field held)."""
        state = self._stream
        previous = state["written"] if state else ""
        start, end = self.result.GetSelection() if previous else (0, 0)
        self.result.ChangeValue(text)
        if previous and (start, end) != (0, 0) and text.startswith(previous):
            self.result.SetSelection(min(start, len(text)), min(end, len(text)))
        if state:
            state["written"] = text

    def _stream_discard(self):
        """Drop a partial answer: the field shows what it showed before."""
        state, self._stream = self._stream, None
        if state is not None and state["announced"] and self.result:
            self.result.ChangeValue(state["original"])
