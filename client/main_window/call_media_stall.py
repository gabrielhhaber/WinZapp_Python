"""CallMediaStallMixin -- part of MainWindow (see main_window/__init__.py).

Tells the user when a call that shows as connected receives no media at all
(core/call_media_watchdog.py judges the samples the page sends every few
seconds). The engine gives no signal of its own in that state: on 2026-11-21 a
link call stayed ACTIVE and silent for ten minutes.
"""

import logging
import time

from core.call_media_watchdog import CallMediaWatchdog


class CallMediaStallMixin:
    """Receives the media samples of the call in progress."""

    def on_call_media_sample(self, sample: dict):
        if not isinstance(sample, dict):
            return
        if not getattr(self, "_active_voice_call", None):
            self._call_media_watchdog = None
            return
        watchdog = getattr(self, "_call_media_watchdog", None)
        if watchdog is None:
            watchdog = self._call_media_watchdog = CallMediaWatchdog()
        verdict = watchdog.feed(sample, time.monotonic())
        if verdict == "stalled":
            logging.warning(
                "[call] media never arrived: bytes_received=%s remotes=%s",
                sample.get("bytesReceived"), len(sample.get("remotes") or []))
            self.output(self.i18n.t("call_media_stalled"), True)
        elif verdict == "recovered":
            logging.info("[call] media arrived again")
