"""One async metadata write plus one replaceable full snapshot, on the DB loop.

Only offers/reservations cross threads. Transactions are never cancelled or
timed out here; close/reset fences run on the same async gate as writes.
"""
import asyncio
import logging
import threading
from concurrent.futures import Future


class LatestMetadataWrite:
    def __init__(self, write):
        self._write = write
        self._lock = threading.Lock()
        self._gate = asyncio.Lock()
        self._pending = self._active = self._latest = None
        self._generation = 0

    def offer(self, value):
        """Copy the full value; return a lease only when a runner is needed."""
        snapshot = dict(value)
        with self._lock:
            self._pending = self._latest = self._generation, snapshot
            return self._reserve()

    def _reserve(self):
        # Caller owns _lock. Retain one failed snapshot for a later offer/close.
        if self._active is not None or self._pending is None:
            return None
        self._active = Future()
        return self._active

    def abort_start(self, lease):
        """Scheduling failed: retain data without stranding the drain fence."""
        with self._lock:
            if self._active is lease:
                self._active = None
        if not lease.done():
            lease.set_result(None)

    async def run(self, lease):
        try:
            while True:
                with self._lock:
                    if self._active is not lease:
                        return
                    item, self._pending = self._pending, None
                    if item is None:
                        # An offer after this point must reserve its own runner;
                        # the old finally must not retire that successor.
                        self._active = None
                        return
                generation, snapshot = item
                async with self._gate:
                    with self._lock:
                        current = generation == self._generation
                    if not current:
                        continue  # A completed reset invalidated this old value.
                    try:
                        await self._write(snapshot)
                    except Exception as error:
                        logging.warning("[presence-names] write failed: %s", type(error).__name__)
                        with self._lock:
                            if self._pending is None:
                                if generation == self._generation:
                                    self._pending = item
                                self._active = None
                                return  # No automatic retry loop on a broken DB.
        finally:
            self.abort_start(lease)

    async def wait_idle(self):
        with self._lock:
            active = self._active
        if active is not None:
            # A cancelled waiter must not cancel a transaction's completion.
            await asyncio.shield(asyncio.wrap_future(active))

    async def flush(self):
        """Close fence: await accepted work, retry a retained failure once."""
        await self.wait_idle()
        with self._lock:
            lease = self._reserve()
        if lease is not None:
            await self.run(lease)

    async def reset(self, operation):
        """Serialize a destructive metadata reset with in-progress writes."""
        try:
            async with self._gate:
                with self._lock:
                    previous = self._latest
                    self._generation += 1
                    self._pending = self._latest = None
                try:
                    return await operation()
                except Exception:
                    # A failed reset did not authorize losing accepted names.
                    # A newer offer during the reset still takes precedence.
                    with self._lock:
                        if self._latest is None and previous is not None:
                            self._pending = self._latest = self._generation, previous[1]
                    raise
        except Exception:
            await self.flush()
            raise
