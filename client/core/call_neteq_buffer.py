"""A stream-owned NetEq reservoir, with the existing reservoir as fallback."""
import logging
import threading

from core.call_neteq import RATES, load_library


class NetEqOutputBuffer:
    def __init__(self, legacy, rate, *, library_loader=load_library):
        self._legacy = legacy
        self._lock = threading.Lock()
        self._owner = None
        self._native = None
        self._source = None
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

    def set_owner(self, owner):
        with self._lock:
            self._owner = owner
            self._legacy.set_owner(owner)

    def _close_native(self):
        if self._native is not None:
            self._native.close()
            self._native = None
        self._source = None

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
            if self._native is not None:
                self._close_native()
                self._legacy.reset()
            self._legacy.push(samples)

    def push_timed(self, samples, timestamp, source_rate, ssrc, clock):
        with self._lock:
            if not self.native_available:
                self._legacy.push(samples)
                return
            source = (source_rate, ssrc, clock)
            delta = ((timestamp - getattr(self, '_timestamp', timestamp) + 2**31) % 2**32) - 2**31
            try:
                if self._source != source or abs(delta) > source_rate * 2:
                    self._close_native()
                    self._native = self._library.create(self._rate)
                    logging.info('[call_audio] playout=neteq rate=%s', self._rate)
                    self._source = source
                    self._mapped = self._carry = 0
                    delta = 0
                    self._legacy.reset()
                advance, self._carry = divmod(delta * self._rate + self._carry, source_rate)
                self._mapped = (self._mapped + advance) % 2**32
                self._timestamp = timestamp
                self._native.insert(self._mapped, samples)
            except Exception:
                self._failed = True
                self._close_native()
                self._legacy.reset()
                self._legacy.push(samples)

    def fill(self, out, *, owner=None):
        with self._lock:
            if owner is not None and owner is not self._owner:
                out[:] = 0
                return False
            if self._native is None or self._failed:
                return self._legacy.fill(out, owner=owner)
            try:
                if self._native.render(out):
                    return True
            except Exception:
                pass
            # Realtime failure: silence and a flag only. The player worker
            # closes the transport/handle and logs outside the device callback.
            self._failed = True
            out[:] = 0
            return True
