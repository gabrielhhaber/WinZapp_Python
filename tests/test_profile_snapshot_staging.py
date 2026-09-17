"""Staged snapshots and the one-copy-at-a-time lock (core/profile_recovery.py).

A backup with WinZapp open copies the released profile, restarts the session
on those bytes, and only then decides whether the copy may replace the restore
point. Until it is confirmed it must touch neither generation — and no two
copies may ever run at once, or they destroy each other's staging and the
known-good `.prev`.
"""

import os

from core import profile_recovery as pr

SESSION = "sess0123456789"


def _profile(global_dir, marker):
    live = pr.profile_dir(str(global_dir), SESSION)
    os.makedirs(os.path.join(live, "Default"), exist_ok=True)
    with open(os.path.join(live, "Default", "state"), "w") as f:
        f.write(marker)


def _generation(path):
    try:
        with open(os.path.join(path, "Default", "state")) as f:
            return f.read()
    except OSError:
        return None


def _newest(global_dir):
    return _generation(pr.snapshot_dir(str(global_dir), SESSION))


def _prev(global_dir):
    return _generation(pr.previous_snapshot_dir(str(global_dir), SESSION))


def _pending(global_dir):
    return _generation(pr.pending_snapshot_dir(str(global_dir), SESSION))


class TestStaging:
    def test_a_staged_copy_touches_neither_generation(self, tmp_path):
        _profile(tmp_path, "good")
        assert pr.capture_snapshot(str(tmp_path), SESSION)
        _profile(tmp_path, "older-good")
        # Make "good" the current and "older-good" nowhere yet: rotate once.
        _profile(tmp_path, "candidate")

        assert pr.capture_snapshot(str(tmp_path), SESSION, max_age=0, stage_only=True)

        assert _newest(tmp_path) == "good"
        assert _pending(tmp_path) == "candidate"

    def test_promoting_rotates_like_a_regular_capture(self, tmp_path):
        _profile(tmp_path, "good")
        pr.capture_snapshot(str(tmp_path), SESSION)
        _profile(tmp_path, "candidate")
        pr.capture_snapshot(str(tmp_path), SESSION, max_age=0, stage_only=True)

        assert pr.promote_pending_snapshot(str(tmp_path), SESSION)

        assert _newest(tmp_path) == "candidate"
        assert _prev(tmp_path) == "good"
        assert _pending(tmp_path) is None

    def test_discarding_leaves_the_restore_point_as_it_was(self, tmp_path):
        _profile(tmp_path, "good")
        pr.capture_snapshot(str(tmp_path), SESSION)
        _profile(tmp_path, "refused")
        pr.capture_snapshot(str(tmp_path), SESSION, max_age=0, stage_only=True)

        pr.discard_pending_snapshot(str(tmp_path), SESSION)

        assert _newest(tmp_path) == "good"
        assert _pending(tmp_path) is None
        assert not pr.promote_pending_snapshot(str(tmp_path), SESSION)

    def test_a_regular_capture_drops_an_older_staged_copy(self, tmp_path):
        """Otherwise a later promotion would put older bytes over newer ones."""
        _profile(tmp_path, "staged")
        pr.capture_snapshot(str(tmp_path), SESSION, max_age=0, stage_only=True)
        _profile(tmp_path, "at-close")

        assert pr.capture_snapshot(str(tmp_path), SESSION, max_age=0)

        assert _newest(tmp_path) == "at-close"
        assert _pending(tmp_path) is None


class TestOneCopyAtATime:
    def test_a_second_copy_gives_up_while_one_is_running(self, tmp_path):
        _profile(tmp_path, "good")
        pr.capture_snapshot(str(tmp_path), SESSION)
        _profile(tmp_path, "new")
        assert pr._CAPTURE_LOCK.acquire(blocking=False)
        try:
            assert pr.capture_snapshot(str(tmp_path), SESSION, max_age=0) is False
            assert pr.capture_snapshot(str(tmp_path), SESSION, max_age=0, stage_only=True) is False
        finally:
            pr._CAPTURE_LOCK.release()
        assert _newest(tmp_path) == "good"
        assert _prev(tmp_path) is None

    def test_promotion_waits_for_no_copy(self, tmp_path):
        _profile(tmp_path, "good")
        pr.capture_snapshot(str(tmp_path), SESSION)
        _profile(tmp_path, "candidate")
        pr.capture_snapshot(str(tmp_path), SESSION, max_age=0, stage_only=True)
        assert pr._CAPTURE_LOCK.acquire(blocking=False)
        try:
            assert pr.promote_pending_snapshot(str(tmp_path), SESSION) is False
        finally:
            pr._CAPTURE_LOCK.release()
        assert _newest(tmp_path) == "good"

    def test_a_discard_never_races_a_promotion_in_progress(self, tmp_path):
        """Closing WinZapp while a confirmed copy is being promoted: the close
        path's cleanup must not delete the directory being renamed into place."""
        _profile(tmp_path, "good")
        pr.capture_snapshot(str(tmp_path), SESSION)
        _profile(tmp_path, "candidate")
        pr.capture_snapshot(str(tmp_path), SESSION, max_age=0, stage_only=True)
        assert pr._CAPTURE_LOCK.acquire(blocking=False)
        try:
            assert pr.discard_pending_snapshot(str(tmp_path), SESSION) is False
            assert _pending(tmp_path) == "candidate"
        finally:
            pr._CAPTURE_LOCK.release()
        assert pr.discard_pending_snapshot(str(tmp_path), SESSION) is True
        assert _pending(tmp_path) is None

    def test_a_short_wait_catches_a_copy_that_is_letting_go(self, tmp_path):
        """The close path waits a moment: a live copy cancelled because the
        app is closing releases the lock almost at once."""
        import threading

        _profile(tmp_path, "at-close")
        assert pr._CAPTURE_LOCK.acquire(blocking=False)
        threading.Timer(0.2, pr._CAPTURE_LOCK.release).start()

        assert pr.capture_snapshot(str(tmp_path), SESSION, max_age=0, lock_wait=2.0)
        assert _newest(tmp_path) == "at-close"

    def test_the_lock_is_released_after_every_copy(self, tmp_path):
        _profile(tmp_path, "x")
        pr.capture_snapshot(str(tmp_path), SESSION, max_age=0)
        pr.capture_snapshot(str(tmp_path), SESSION, max_age=0, cancel=lambda: True)
        assert pr._CAPTURE_LOCK.acquire(blocking=False)
        pr._CAPTURE_LOCK.release()


class TestCancel:
    def test_a_cancelled_copy_keeps_both_generations(self, tmp_path):
        """WinZapp began closing mid-copy: stop, and leave the restore points alone."""
        _profile(tmp_path, "good")
        pr.capture_snapshot(str(tmp_path), SESSION)
        _profile(tmp_path, "half")

        assert pr.capture_snapshot(str(tmp_path), SESSION, max_age=0, cancel=lambda: True) is False
        assert pr.capture_snapshot(
            str(tmp_path), SESSION, max_age=0, cancel=lambda: True, stage_only=True) is False

        assert _newest(tmp_path) == "good"
        assert _pending(tmp_path) is None
        assert not os.path.exists(pr.snapshot_dir(str(tmp_path), SESSION) + ".partial")
