"""AISummaryPartsMixin — the part of AIResultDialog that runs a hierarchical
chat summary (core/ai_media/summary_parts.py).

The panel hands the dialog the parts of a long backlog (``parts``); the first
request of the window, and "Generate again", then make one request per part and
a final one that joins them. A follow-up question is the ordinary single
request, answered from the partial summaries (``session.media``).

Methods run with ``self`` bound to the AIResultDialog.
"""
import wx

from core.ai_media import config as ai_config
from core.ai_media import usage as ai_usage
from core.ai_media.errors import DescriptionError
from core.ai_media.payload import prepare_media
from core.ai_media.prompts import parts_instructions
from core.ai_media.service import run_chain
from core.ai_media.summary_parts import run_parts


class AISummaryPartsMixin:
    #: The transcript of each part, oldest first; empty for an ordinary summary.
    _parts = ()
    #: The last run's final join failed and the answer is the partial summaries.
    _parts_merge_failed = False

    def _parts_run(self, regenerate, task) -> bool:
        """The request about to start is the hierarchical one: the opening
        request of the window or "Generate again", not a follow-up question."""
        return bool(self._parts) and regenerate and not task

    def _parts_loading(self) -> str:
        """What the status says and the window speaks when it starts."""
        return self.i18n.t("ai_summary_part_progress").format(part=1, total=len(self._parts))

    def _parts_failed_notice(self) -> str:
        return self.i18n.t("ai_summary_parts_merge_failed") if self._parts_merge_failed else ""

    def _parts_work(self, operation, providers, store, config, language, generation):
        """Worker thread: every part, then the join. Returns what the ordinary
        ``work()`` does: ``(media, (answer, provider))``."""
        t = self.i18n.t
        self._parts_merge_failed = False

        def call(step, merging, question, text):
            media = prepare_media("text", ai_config.utf8_bytes(text), "text/plain", config["profile"])
            return run_chain(step, providers, ai_config.models_for(config, "text"), store.get, media,
                             (), question, parts_instructions(language, config["profile"], merging),
                             config["profile"], usage_sink=ai_usage.persist,
                             endpoints=ai_config.endpoints(config))

        def progress(step, total):
            wx.CallAfter(self._parts_progress, generation, step, total)

        def heading(part, total):
            return t("ai_summary_part_heading").format(part=part, total=total)

        result = run_parts(operation, self._parts, call, progress,
                           local=ai_config.has_local(providers), heading=heading)
        self._parts_merge_failed = not result.merged
        try:
            media = prepare_media("text", ai_config.utf8_bytes(result.joined), "text/plain",
                                  config["profile"])
        except DescriptionError:
            media = None  # a follow-up then starts again from the window's own data
        return media, (result.answer, result.provider)

    def _parts_progress(self, generation, step, total):
        """The status and, from the second request on, one spoken line: part N of
        M, then the join. Never one line per message or per second."""
        if not self._valid() or generation != self.session.generation or self.session.active is None:
            return
        t = self.i18n.t
        text = (t("ai_summary_parts_merging").format(total=total) if step > total
                else t("ai_summary_part_progress").format(part=step, total=total))
        self.status.ChangeValue(text)
        if step > 1:
            self._speak(text)
