"""A restore point for the Chrome profile WhatsApp Web's login lives in.

WPPConnect's own file token store is empty on a real install even while a
session is healthy and connected, so `api/userDataDir/<session>` is the sole
carrier of the WhatsApp login. Damage it and there is no fallback: the user
re-pairs. And damaging it is routine — a real shutdown_audit.log covering 159
launches holds seventeen runs that ended with no `_stop_wpp_server` line,
eleven of them overnight gaps of 7-12 hours, and the last one immediately
preceded a session that authenticated but never reached `WPP.isReady`.

What is pinned here is mostly the *safety* of the mechanism, because every
failure mode of a restore point is worse than not having one: snapshotting a
live profile would preserve a half-written LevelDB, an interrupted copy left in
place would replace a working profile with a truncated one, and a restore that
throws halfway would leave the account with no profile at all.
"""

import os
import time

import pytest

from core import profile_recovery as pr


def _make_profile(root, session="s1", files=(("Default/IndexedDB/x.ldb", "login"),)):
    """A stand-in for api/userDataDir/<session> with content at real depth."""
    profile = pr.profile_dir(str(root), session)
    for rel, text in files:
        path = os.path.join(profile, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return profile


class TestWhereThingsLive:
    def test_the_profile_path_matches_what_node_is_given(self, tmp_path):
        """Mirrors _start_wpp_background()'s WINZAPP_USER_DATA_DIR. Getting
        this wrong snapshots an empty directory and reports success."""
        assert pr.profile_dir("/g", "abc") == os.path.join(
            "/g", "api", "userDataDir", "abc")

    def test_snapshots_live_beside_the_profiles_never_inside_one(self, tmp_path):
        """Anything under userDataDir/<session>/ is handed to Chrome, which is
        free to walk, rewrite or delete what it finds there."""
        snap = pr.snapshot_dir("/g", "abc")
        assert pr.profile_dir("/g", "abc") not in snap
        assert snap.startswith(os.path.join("/g", "api"))


class TestTakingASnapshot:
    def test_a_profile_is_copied_whole(self, tmp_path):
        _make_profile(tmp_path, files=(
            ("Default/IndexedDB/x.ldb", "login"),
            ("Default/Local Storage/y", "prefs"),
        ))
        assert pr.capture_snapshot(str(tmp_path), "s1") is True
        snap = pr.snapshot_dir(str(tmp_path), "s1")
        assert os.path.isfile(os.path.join(snap, "Default", "IndexedDB", "x.ldb"))
        assert os.path.isfile(os.path.join(snap, "Default", "Local Storage", "y"))

    def test_chromes_singleton_markers_are_left_behind(self, tmp_path):
        """A stale SingletonLock inside a restored copy is one of the things
        that makes a relaunch refuse the profile."""
        _make_profile(tmp_path, files=(
            ("Default/IndexedDB/x.ldb", "login"),
            ("SingletonLock", "pid"),
            ("lockfile", "pid"),
        ))
        pr.capture_snapshot(str(tmp_path), "s1")
        snap = pr.snapshot_dir(str(tmp_path), "s1")
        assert not os.path.exists(os.path.join(snap, "SingletonLock"))
        assert not os.path.exists(os.path.join(snap, "lockfile"))

    def test_a_missing_profile_is_not_an_error(self, tmp_path):
        assert pr.capture_snapshot(str(tmp_path), "nope") is False

    def test_a_fresh_snapshot_is_not_retaken(self, tmp_path):
        """A user who opens and closes WinZapp ten times in an afternoon pays
        the copy once, not ten times."""
        _make_profile(tmp_path)
        assert pr.capture_snapshot(str(tmp_path), "s1") is True
        assert pr.capture_snapshot(str(tmp_path), "s1") is False

    def test_a_stale_snapshot_is_refreshed(self, tmp_path):
        _make_profile(tmp_path)
        pr.capture_snapshot(str(tmp_path), "s1")
        snap = pr.snapshot_dir(str(tmp_path), "s1")
        old = time.time() - (pr.SNAPSHOT_MAX_AGE_SECONDS + 60)
        os.utime(snap, (old, old))
        assert pr.capture_snapshot(str(tmp_path), "s1") is True


class TestAFailedSnapshotNeverCostsTheOldOne:
    """Everything is staged and renamed into place, so the previous restore
    point survives every way the copy can fail."""

    def test_a_budget_overrun_leaves_the_previous_snapshot_intact(self, tmp_path):
        _make_profile(tmp_path, files=(("Default/IndexedDB/x.ldb", "good"),))
        assert pr.capture_snapshot(str(tmp_path), "s1") is True
        snap = pr.snapshot_dir(str(tmp_path), "s1")
        old = time.time() - (pr.SNAPSHOT_MAX_AGE_SECONDS + 60)
        os.utime(snap, (old, old))

        # Rewrite the live profile, then deny the copy any time at all.
        _make_profile(tmp_path, files=(("Default/IndexedDB/x.ldb", "corrupt"),))
        assert pr.capture_snapshot(str(tmp_path), "s1", budget=-1) is False

        with open(os.path.join(snap, "Default", "IndexedDB", "x.ldb"),
                  encoding="utf-8") as fh:
            assert fh.read() == "good"

    def test_no_partial_directory_is_left_behind(self, tmp_path):
        _make_profile(tmp_path)
        pr.capture_snapshot(str(tmp_path), "s1", budget=-1)
        assert not os.path.exists(pr.snapshot_dir(str(tmp_path), "s1") + ".partial")

    def test_an_unreadable_file_does_not_lose_the_whole_restore_point(
        self, tmp_path, monkeypatch
    ):
        """Chrome leaves sockets and transient caches that copy badly. The
        login does not live in them."""
        _make_profile(tmp_path, files=(
            ("Default/IndexedDB/x.ldb", "login"),
            ("Default/weird.sock", "junk"),
        ))
        real_copy = pr.shutil.copy2

        def _flaky(src, dst, *a, **kw):
            if src.endswith("weird.sock"):
                raise OSError(13, "Permission denied")
            return real_copy(src, dst, *a, **kw)

        monkeypatch.setattr(pr.shutil, "copy2", _flaky)
        assert pr.capture_snapshot(str(tmp_path), "s1") is True
        snap = pr.snapshot_dir(str(tmp_path), "s1")
        assert os.path.isfile(os.path.join(snap, "Default", "IndexedDB", "x.ldb"))


class TestRestoring:
    def test_the_saved_profile_replaces_the_live_one(self, tmp_path):
        _make_profile(tmp_path, files=(("Default/IndexedDB/x.ldb", "good"),))
        pr.capture_snapshot(str(tmp_path), "s1")
        _make_profile(tmp_path, files=(("Default/IndexedDB/x.ldb", "corrupt"),))

        assert pr.restore_snapshot(str(tmp_path), "s1") is True

        live = pr.profile_dir(str(tmp_path), "s1")
        with open(os.path.join(live, "Default", "IndexedDB", "x.ldb"),
                  encoding="utf-8") as fh:
            assert fh.read() == "good"

    def test_the_broken_profile_is_kept_not_deleted(self, tmp_path):
        """It is the only evidence of a failure nobody has reproduced."""
        _make_profile(tmp_path, files=(("Default/IndexedDB/x.ldb", "good"),))
        pr.capture_snapshot(str(tmp_path), "s1")
        _make_profile(tmp_path, files=(("Default/IndexedDB/x.ldb", "corrupt"),))

        pr.restore_snapshot(str(tmp_path), "s1")

        broken = pr.profile_dir(str(tmp_path), "s1") + ".broken"
        with open(os.path.join(broken, "Default", "IndexedDB", "x.ldb"),
                  encoding="utf-8") as fh:
            assert fh.read() == "corrupt"

    def test_restoring_without_a_snapshot_changes_nothing(self, tmp_path):
        _make_profile(tmp_path, files=(("Default/IndexedDB/x.ldb", "live"),))
        assert pr.restore_snapshot(str(tmp_path), "s1") is False
        live = pr.profile_dir(str(tmp_path), "s1")
        with open(os.path.join(live, "Default", "IndexedDB", "x.ldb"),
                  encoding="utf-8") as fh:
            assert fh.read() == "live"

    def test_a_restore_that_fails_puts_the_original_back(self, tmp_path, monkeypatch):
        """The one outcome that must be impossible is an account left with no
        profile at all."""
        _make_profile(tmp_path, files=(("Default/IndexedDB/x.ldb", "good"),))
        pr.capture_snapshot(str(tmp_path), "s1")
        _make_profile(tmp_path, files=(("Default/IndexedDB/x.ldb", "live"),))

        def _boom(*a, **kw):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(pr.shutil, "copytree", _boom)
        assert pr.restore_snapshot(str(tmp_path), "s1") is False

        live = pr.profile_dir(str(tmp_path), "s1")
        assert os.path.isdir(live)
        with open(os.path.join(live, "Default", "IndexedDB", "x.ldb"),
                  encoding="utf-8") as fh:
            assert fh.read() == "live"

    def test_a_restored_profile_can_be_snapshotted_again(self, tmp_path):
        """The .broken and .partial siblings must not confuse the next round."""
        _make_profile(tmp_path)
        pr.capture_snapshot(str(tmp_path), "s1")
        pr.restore_snapshot(str(tmp_path), "s1")
        snap = pr.snapshot_dir(str(tmp_path), "s1")
        old = time.time() - (pr.SNAPSHOT_MAX_AGE_SECONDS + 60)
        os.utime(snap, (old, old))
        assert pr.capture_snapshot(str(tmp_path), "s1") is True


class TestDecidingAProfileIsBroken:
    """The detector must be specific. Firing on an ordinary offline spell would
    replace a perfectly good profile with a day-old one for no reason."""

    def _cycle(self, tracker, times, paired=True):
        fired = False
        for _ in range(times):
            tracker.note_status("INITIALIZING", paired=paired)
            fired = tracker.note_status("CLOSED", paired=paired) or fired
        return fired

    def test_three_failed_cycles_are_needed(self):
        t = pr.ProfileHealthTracker()
        assert self._cycle(t, 2) is False
        assert self._cycle(t, 1) is True

    def test_it_fires_only_once_per_crossing(self):
        """A caller polling every 30s must not act on it over and over."""
        t = pr.ProfileHealthTracker()
        self._cycle(t, 3)
        assert self._cycle(t, 3) is False

    def test_a_single_successful_connection_clears_everything(self):
        t = pr.ProfileHealthTracker()
        self._cycle(t, 2)
        t.note_status("CONNECTED")
        assert self._cycle(t, 2) is False

    def test_closed_without_initializing_is_not_a_failed_cycle(self):
        """An idle session that was simply closed never tried to start."""
        t = pr.ProfileHealthTracker()
        for _ in range(10):
            assert t.note_status("CLOSED") is False

    def test_pairing_never_counts(self):
        """Mid-pairing there is no login to lose, and the QR/code flow drives
        the session through these very states on purpose."""
        t = pr.ProfileHealthTracker()
        assert self._cycle(t, 5, paired=False) is False

    @pytest.mark.parametrize("status", ["QRCODE", "notLogged", "", None, "OPENING"])
    def test_unrelated_states_are_ignored(self, status):
        t = pr.ProfileHealthTracker()
        t.note_status("INITIALIZING")
        assert t.note_status(status) is False

    def test_the_status_string_is_read_case_insensitively(self):
        t = pr.ProfileHealthTracker()
        for _ in range(3):
            t.note_status("initializing")
            fired = t.note_status("closed")
        assert fired is True
