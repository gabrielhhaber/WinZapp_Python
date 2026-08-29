"""Tests for MainWindow._flush_pending_debounced_saves().

Reported gap (found by re-reviewing the shutdown path end to end, not from a
live report): _schedule_save() debounces chat/contacts persistence by 0.15s
and _schedule_save_settings() debounces settings.json by 2s, each on its own
daemon threading.Timer. _perform_shutdown() never cancelled or flushed
either before closing the DB and exiting -- so a save that was pending at
the exact moment of shutdown could be lost two different ways: db.close()
starts rejecting calls the instant it flips _closing (and _do_save() clears
_dirty_jids_for_save *before* attempting the write, so there is nothing
left to retry even if it were called again), or os._exit() simply kills the
daemon timer thread before it ever fires at all, since it does not wait for
other threads.

_flush_pending_debounced_saves() cancels both timers and runs their
callbacks synchronously, right in the shutdown path, before either of those
can cut them off. Called from both _perform_shutdown() (normal/IPC quit)
and _on_end_session() (WM_ENDSESSION) -- the latter never goes through
_perform_shutdown() at all.
"""

import threading

from main import MainWindow


class _Stub:
    _flush_pending_debounced_saves = MainWindow._flush_pending_debounced_saves

    def __init__(self):
        self._save_timer_lock = threading.Lock()
        self._save_timer = None
        self._settings_save_timer = None
        self.do_save_calls = 0
        self.save_settings_calls = 0
        self.do_save_raises = False
        self.save_settings_raises = False

    def _do_save(self):
        self.do_save_calls += 1
        if self.do_save_raises:
            raise RuntimeError("boom")

    def save_settings(self):
        self.save_settings_calls += 1
        if self.save_settings_raises:
            raise RuntimeError("boom")


class TestNoPendingTimers:
    def test_does_nothing_when_neither_timer_is_pending(self):
        s = _Stub()
        s._flush_pending_debounced_saves()
        assert s.do_save_calls == 0
        assert s.save_settings_calls == 0


class TestPendingChatSaveIsFlushed:
    def test_pending_chat_timer_is_cancelled_and_run_synchronously(self):
        s = _Stub()
        fired = threading.Event()
        t = threading.Timer(999, fired.set)  # long enough it would never fire on its own
        t.daemon = True
        t.start()
        s._save_timer = t

        s._flush_pending_debounced_saves()

        assert s.do_save_calls == 1
        assert s._save_timer is None
        assert not fired.is_set(), "the original timer callback must never fire"

    def test_an_exception_in_do_save_does_not_propagate(self):
        """A flush failure must not abort the rest of shutdown (settings
        flush, db.close(), process exit)."""
        s = _Stub()
        s.do_save_raises = True
        t = threading.Timer(999, lambda: None)
        t.daemon = True
        t.start()
        s._save_timer = t

        s._flush_pending_debounced_saves()  # must not raise

        assert s.do_save_calls == 1


class TestPendingSettingsSaveIsFlushed:
    def test_pending_settings_timer_is_cancelled_and_run_synchronously(self):
        s = _Stub()
        t = threading.Timer(999, lambda: None)
        t.daemon = True
        t.start()
        s._settings_save_timer = t

        s._flush_pending_debounced_saves()

        assert s.save_settings_calls == 1
        assert s._settings_save_timer is None

    def test_an_exception_in_save_settings_does_not_propagate(self):
        s = _Stub()
        s.save_settings_raises = True
        t = threading.Timer(999, lambda: None)
        t.daemon = True
        t.start()
        s._settings_save_timer = t

        s._flush_pending_debounced_saves()  # must not raise

        assert s.save_settings_calls == 1


class TestBothPendingAtOnce:
    def test_both_timers_flushed_independently(self):
        s = _Stub()
        t1 = threading.Timer(999, lambda: None)
        t1.daemon = True
        t1.start()
        s._save_timer = t1
        t2 = threading.Timer(999, lambda: None)
        t2.daemon = True
        t2.start()
        s._settings_save_timer = t2

        s._flush_pending_debounced_saves()

        assert s.do_save_calls == 1
        assert s.save_settings_calls == 1
