"""Bounded local counters; an accepted native write is not peer delivery."""
import logging
import time

from call_audio_signal import SignalWindow


NAMES = frozenset({"mic_packets", "mic_bytes", "mic_not_connected", "mic_invalid_payload",
    "mic_invalid_format", "mic_format_unavailable", "mic_native_rejected", "mic_native_write_ok",
    "mic_native_bytes", "remote_emit_attempts", "remote_bytes", "remote_rejected", "remote_emit_errors",
    "mic_owner_rejected", "mic_stale_stream"})

SIGNAL_STAGES = ("mic_native_accepted", "remote_native_pcm")


class AudioDelivery:
    """Accessed under CallMedia's lifecycle gate, never on a device callback."""
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._last = clock()
        self._counts = {}
        self._signals = {stage: SignalWindow() for stage in SIGNAL_STAGES}
        self._signal_start = self._last
        self._signal_dirty = False
        self._signal_errors = 0

    def observe(self, stage, pcm, sample_rate):
        """Observe a fixed boundary; diagnostics must never stop media delivery."""
        if stage not in SIGNAL_STAGES:
            raise ValueError("invalid call audio signal stage")
        self._signal_dirty = True
        try:
            return self._signals[stage].add(pcm, sample_rate)
        except Exception:
            # No exception detail: it could contain payload or native data.
            self._signal_errors += 1
            return False

    def signal_summary(self):
        return {"window_ms": round(max(0, self._clock() - self._signal_start) * 1000),
                "measurement_errors": self._signal_errors,
                "stages": {stage: window.summary() for stage, window in self._signals.items()}}

    def add(self, name, count=1):
        if name not in NAMES or type(count) is not int or count < 0:
            raise ValueError("invalid call audio counter")
        self._counts[name] = self._counts.get(name, 0) + count

    def summary(self):
        return dict(self._counts)

    def report(self, *, force=False):
        now = self._clock()
        if not (force or now - self._last >= 5):
            return
        had_signal = self._signal_dirty
        if self._counts:
            logging.getLogger("bridge").info("call audio delivery counters: %s", self.summary())
        if self._signal_dirty:
            try:
                logging.getLogger("bridge").info("call audio signal summary: %s", self.signal_summary())
                for window in self._signals.values():
                    window.summary(reset=True)
                self._signal_start = now
                self._signal_errors = 0
                self._signal_dirty = False
            except Exception:
                # Keep the window for the next report without affecting audio.
                pass
        if self._counts or had_signal:
            self._last = now


def counters(call):
    if "audio_delivery" not in call:
        call["audio_delivery"] = AudioDelivery()
    return call["audio_delivery"]
