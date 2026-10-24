"""Bound logical waits without pretending to cancel a blocking HTTP thread.

Physical slots remain occupied until workers actually finish, across epochs.
The UI supplies its scheduler; all time, scheduling and delivery are injectable.
"""
import math
import threading

WORK_SECONDS = 30
WORK_LIMIT = 2  # Per kind, independently in each pool.


class PresenceWork:
    def __init__(self, clock, call_later, call_after):
        self.clock, self.call_later, self.call_after = clock, call_later, call_after
        self.lock = threading.Lock()
        self.running = set()
        self.pending = set()  # Includes completed workers awaiting UI delivery.

    def claim(self, kind, keys=(), *, exclusive=False):
        keys = frozenset(keys)
        with self.lock:
            same_kind = [job for job in self.running | self.pending if job.kind == kind]
            if len(same_kind) >= WORK_LIMIT:
                return None
            # A still-running POST may have reached the provider. Do not send
            # an overlapping subscription merely because its UI wait expired.
            # Timer-free profile callers also request exclusive identity keys.
            if (kind == "post" or exclusive) and any(job.keys & keys for job in same_kind):
                return None
            job = PresenceJob(self, kind, keys, self.clock() + WORK_SECONDS)
            self.running.add(job)
            self.pending.add(job)
            return job

    def reap(self):
        with self.lock:
            jobs = tuple(self.pending)
        return any([job.expire() for job in jobs])

    def invalidate(self):
        with self.lock:
            jobs = tuple(self.running | self.pending)
            for job in jobs:
                job.invalid = True
        for job in jobs:
            job.stop_timer()


class PresenceJob:
    def __init__(self, pool, kind, keys, deadline):
        self.pool, self.kind, self.keys, self.deadline = pool, kind, keys, deadline
        self.invalid, self.finished, self.timer = False, False, None
        self.timer_generation = 0
        self.on_expire = None

    @property
    def active(self):
        with self.pool.lock:
            return not self.invalid and self.pool.clock() < self.deadline

    def apply(self, callback):
        """Commit small cache changes atomically with deadline/invalidation."""
        with self.pool.lock:
            if self.invalid or self.pool.clock() >= self.deadline:
                return False
            callback()
            return True

    def arm(self, on_expire):
        self.on_expire = on_expire
        with self.pool.lock:
            self.timer_generation += 1
            generation = self.timer_generation
        def fire():
            with self.pool.lock:
                if generation != self.timer_generation:
                    return
            if not self.expire() and self.active:
                self.arm(on_expire)  # Early timer: retain the original deadline.
        delay = max(1, math.ceil((self.deadline - self.pool.clock()) * 1000))
        self.timer = self.pool.call_later(delay, fire)

    def expire(self):
        with self.pool.lock:
            if self.invalid or self.pool.clock() < self.deadline:
                return False
            self.invalid = True
        self.stop_timer()
        if self.on_expire is not None:
            self.on_expire()
        return True

    def stop_timer(self):
        with self.pool.lock:
            self.pool.pending.discard(self)
            self.timer_generation += 1
        timer, self.timer = self.timer, None
        if timer is not None:
            try:
                timer.Stop()
            except RuntimeError:
                pass

    def finish(self):
        """Worker thread: release only the physical slot, never touch a timer."""
        with self.pool.lock:
            self.finished = True
            self.pool.running.discard(self)

    def cancel_unstarted(self):
        with self.pool.lock:
            self.invalid = True
        self.stop_timer()
        self.finish()
