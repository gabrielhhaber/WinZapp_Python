"""Stream-owned NetEq playout, one instance per sender, with the existing
reservoir as fallback.

A direct call has one sender: a new SSRC or codec clock replaces its NetEq.
A group call (protocol 3, ``mixed``) gives every participant its own NetEq on
the device clock, the way WebRTC's mixer pulls each receive stream: each
sender's jitter, loss concealment and time stretching stay its own, and the
device callback sums the 10 ms blocks. A sender that stops sending is closed
after ``STREAM_IDLE_SECONDS`` by the worker, never by the device callback.
"""
import logging
import threading
import time

import numpy as np

from core.call_neteq import RATES, load_library

# A group call can hold 32 people; the senders heard at once are far fewer, and
# a silent NetEq in its muted state costs little. Past this the least recently
# heard sender is closed.
MAX_STREAMS = 16
STREAM_IDLE_SECONDS = 5.0


# The worker's timed ingress keeps at most eight 60 ms frames per sender.
TIMED_FRAMES_PER_STREAM = 8


def timed_ingress_limit(stream_count, mixed):
    """Timed frames the player queue may hold: 480 ms of a direct call, or of
    each group sender heard so far plus one that may be joining."""
    if not mixed:
        return TIMED_FRAMES_PER_STREAM
    return TIMED_FRAMES_PER_STREAM * max(1, min(MAX_STREAMS, int(stream_count or 0) + 1))


class _Stream:
    __slots__ = ('native', 'source', 'timestamp', 'mapped', 'carry', 'inserted')

    def __init__(self, native, source, timestamp, now):
        self.native, self.source = native, source
        self.timestamp, self.mapped, self.carry = timestamp, 0, 0
        self.inserted = now


class NetEqOutputBuffer:
    def __init__(self, legacy, rate, *, library_loader=load_library, clock=time.monotonic):
        self._legacy = legacy
        self._lock = threading.Lock()
        self._owner = None
        self._streams = {}  # SSRC -> _Stream, in the order they were created
        self._scratch = np.zeros(0, dtype=np.float32)
        self._clock = clock
        self._failed = False
        self._failure_reported = False
        self._rate = rate
        self._library = library_loader()

    def __getattr__(self, name):
        return getattr(self._legacy, name)

    @property
    def native_available(self):
        return self._library is not None and self._rate in RATES and not self._failed

    @property
    def native_failed(self):
        return self._failed

    @property
    def stream_count(self):
        return len(self._streams)

    def set_owner(self, owner):
        with self._lock:
            self._owner = owner
            self._legacy.set_owner(owner)

    def _close_stream(self, ssrc):
        stream = self._streams.pop(ssrc, None)
        if stream is not None:
            stream.native.close()

    def _close_native(self):
        for ssrc in list(self._streams):
            self._close_stream(ssrc)

    def reset(self):
        with self._lock:
            self._close_native()
            self._legacy.reset()

    def release_failed(self):
        with self._lock:
            if self._failed:
                self._close_native()
                if not self._failure_reported:
                    self._failure_reported = True
                    logging.warning('[call_audio] NetEq failed; restoring compatible playout')

    def configure(self, rate):
        with self._lock:
            self._close_native()
            self._rate = rate
            self._legacy.configure(rate)

    def push(self, samples):
        with self._lock:
            if self._streams:
                self._close_native()
                self._legacy.reset()
            self._legacy.push(samples)

    def _retire_others(self, ssrc, mixed, now):
        if not mixed:
            # One direct sender: any other stream is one this sender replaced.
            for other in [key for key in self._streams if key != ssrc]:
                self._close_stream(other)
            return
        for other, stream in list(self._streams.items()):
            if other != ssrc and now - stream.inserted > STREAM_IDLE_SECONDS:
                self._close_stream(other)
        if ssrc not in self._streams and len(self._streams) >= MAX_STREAMS:
            self._close_stream(min(self._streams, key=lambda key: self._streams[key].inserted))

    def push_timed(self, samples, timestamp, source_rate, ssrc, clock, mixed=False):
        """``mixed`` PCM is one sender of a group call. Without NetEq it is
        dropped, never pushed into the single-source reservoir, where the
        senders would interleave: the worker then releases the native
        transport and the bridge's participant mixer takes the group over."""
        with self._lock:
            if not self.native_available:
                if not mixed:
                    self._legacy.push(samples)
                return
            now = self._clock()
            try:
                self._retire_others(ssrc, mixed, now)
                stream = self._streams.get(ssrc)
                source = (source_rate, clock)
                delta = 0
                if stream is not None:
                    delta = ((timestamp - stream.timestamp + 2**31) % 2**32) - 2**31
                if stream is None or stream.source != source or abs(delta) > source_rate * 2:
                    self._close_stream(ssrc)
                    stream = _Stream(self._library.create(self._rate), source, timestamp, now)
                    self._streams[ssrc] = stream
                    logging.info('[call_audio] playout=neteq rate=%s streams=%s',
                                 self._rate, len(self._streams))
                    delta = 0
                    self._legacy.reset()
                advance, stream.carry = divmod(delta * self._rate + stream.carry, source_rate)
                stream.mapped = (stream.mapped + advance) % 2**32
                stream.timestamp = timestamp
                stream.inserted = now
                stream.native.insert(stream.mapped, samples)
            except Exception:
                self._failed = True
                self._close_native()
                self._legacy.reset()
                if not mixed:
                    self._legacy.push(samples)

    def _render_mix(self, out):
        streams = iter(self._streams.values())
        if not next(streams).native.render(out):
            return False
        if len(self._streams) == 1:
            return True
        if self._scratch.size < out.size:
            self._scratch = np.zeros(out.size, dtype=np.float32)
        scratch = self._scratch[:out.size]
        for stream in streams:
            if not stream.native.render(scratch):
                return False
            out += scratch
        np.clip(out, -1.0, 1.0, out=out)
        return True

    def fill(self, out, *, owner=None):
        with self._lock:
            if owner is not None and owner is not self._owner:
                out[:] = 0
                return False
            if not self._streams or self._failed:
                return self._legacy.fill(out, owner=owner)
            try:
                if self._render_mix(out):
                    return True
            except Exception:
                pass
            # Realtime failure: silence and a flag only. The player worker
            # closes the transport/handles and logs outside the device callback.
            self._failed = True
            out[:] = 0
            return True
