"""Bounded FIFO media work off aiohttp's loop; no device or native imports."""
from collections import deque
import logging
import threading
import time

log = logging.getLogger('bridge')


class _Lane:
    def __init__(self, name, limit, byte_limit, clock):
        self.name, self.limit, self.byte_limit = name, limit, byte_limit
        self.clock = clock
        self._condition = threading.Condition()
        self._pending = deque()
        self._bytes = 0
        self._thread = None
        self._closed = False
        self._counts = {}
        self._reported = clock() - 5  # The first failure must be observable.

    def submit(self, callback, weight, *, control=False, owner=None, epoch=None,
               starting=False, stopping=False):
        with self._condition:
            if self._closed or weight > self.byte_limit:
                return False
            if (starting and epoch is not None and any(
                    item[3] is owner and item[5] and item[4] is not None and item[4] >= epoch
                    for item in self._pending)):
                self._count('stale')
                return False
            if control:
                kept = deque()
                for item in self._pending:
                    older = (epoch is None or item[4] is None
                             or (item[4] < epoch if starting else item[4] <= epoch))
                    if item[3] is owner and older:
                        self._bytes -= item[2]
                        self._count('superseded')
                    else:
                        kept.append(item)
                self._pending = kept
            while self._pending and (len(self._pending) >= self.limit
                                     or self._bytes + weight > self.byte_limit):
                # Preserve a stop barrier while dropping old frame backlog.
                index = next((i for i, item in enumerate(self._pending) if item[1] is not None), None)
                if index is None:
                    if not control:
                        self._count('overflow')
                        return False
                    index = 0
                item = self._pending[index]
                del self._pending[index]
                size = item[2]
                self._bytes -= size
                self._count('overflow')
            self._pending.append((callback, None if control else self.clock() + .2, weight, owner, epoch, stopping))
            self._bytes += weight
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True,
                                                name=f'call-{self.name}-ingress')
                try:
                    self._thread.start()
                except Exception:
                    self._thread = None
                    self._pending.clear()
                    self._bytes = 0
                    raise
            self._condition.notify()
            return True

    def _count(self, name, amount=1):
        self._counts[name] = self._counts.get(name, 0) + amount

    def _run(self):
        while True:
            with self._condition:
                while not self._pending and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                callback, deadline, weight, _, _, _ = self._pending.popleft()
                self._bytes -= weight
                expired = deadline is not None and self.clock() > deadline
                if expired:
                    self._count('expired')
            if not expired:
                try:
                    if callback() is False:
                        with self._condition:
                            self._count('stale')
                except Exception:
                    # Arbitrary native/transport exception text can contain
                    # account identifiers. Count the stage, never that text.
                    with self._condition:
                        self._count('failed')
            with self._condition:
                now = self.clock()
                if self._counts and now - self._reported >= 5:
                    counts = dict(self._counts)
                    self._reported = now
                else:
                    counts = None
            if counts:
                log.warning('call media ingress %s counters: %s', self.name, counts)

    def close(self):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._pending.clear()
            self._bytes = 0
            self._condition.notify_all()
            counts = dict(self._counts)
        if counts:
            log.warning('call media ingress %s final counters: %s', self.name, counts)

    def join(self, timeout=.5):
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def summary(self):
        with self._condition:
            return {**self._counts, 'queued': len(self._pending), 'bytes': self._bytes}


class MediaIngress:
    """Audio has its own worker: video encoder startup cannot queue ahead of it.

    Callers capture an immutable owner at arrival and recheck it inside each
    callback. Queue limits bound memory and age, not native execution time.
    close() invalidates pending work; it cannot forcibly interrupt native code.
    """
    def __init__(self, clock=time.monotonic):
        self.audio = _Lane('audio', 10, 19200, clock)  # 200 ms PCM48 s16le
        self.video = _Lane('video', 2, 512000, clock)  # two bounded JPEGs

    def submit(self, kind, callback, *, weight=0, owner=None, epoch=None):
        lane = self.audio if kind in ('mic', 'audio_stop') else self.video
        return lane.submit(callback, weight, control=kind.endswith('_stop') or kind == 'camera_start',
                           owner=owner, epoch=epoch, starting=kind == 'camera_start', stopping=kind.endswith('_stop'))

    def close(self):
        self.audio.close()
        self.video.close()

    def join(self, timeout=.5):
        self.audio.join(timeout)
        self.video.join(timeout)
