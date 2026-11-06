"""Bounded local counters; an accepted native write is not peer delivery."""
import logging
import time


NAMES = frozenset({"mic_packets", "mic_bytes", "mic_not_connected", "mic_invalid_payload",
    "mic_invalid_format", "mic_format_unavailable", "mic_native_rejected", "mic_native_write_ok",
    "mic_native_bytes", "remote_emit_attempts", "remote_bytes", "remote_rejected"})


class AudioDelivery:
    """Accessed under CallMedia's lifecycle gate, never on a device callback."""
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._last = clock()
        self._counts = {}

    def add(self, name, count=1):
        if name not in NAMES or type(count) is not int or count < 0:
            raise ValueError("invalid call audio counter")
        self._counts[name] = self._counts.get(name, 0) + count

    def summary(self):
        return dict(self._counts)

    def report(self, *, force=False):
        now = self._clock()
        if self._counts and (force or now - self._last >= 5):
            logging.getLogger("bridge").info("call audio delivery counters: %s", self.summary())
            self._last = now


def counters(call):
    if "audio_delivery" not in call:
        call["audio_delivery"] = AudioDelivery()
    return call["audio_delivery"]
