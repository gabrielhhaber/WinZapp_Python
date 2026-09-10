"""A restore must not put back the profile that was just rejected.

Reported by a user whose session dropped again immediately after a "successful"
profile recovery. The log answers it exactly, via the `login_store=` fingerprint
that `_stop_wpp_server()` writes at close and `STARTUP` writes at the next
launch (2026-09-10):

    10:52:45  Chrome released the profile  files=21 bytes=27793052 newest=1789048351
    10:52:46  profile snapshot refreshed
    10:53:46  STARTUP                      files=21 bytes=27793052 newest=1789048351
    10:53:56  Session Unpaired -> post_logout=1&logout_reason=0
    10:55:48  profile restored from snapshot
    10:55:56  Session Unpaired -> post_logout=1&logout_reason=0

Byte-identical. The restore replaced the rejected profile with an identical
copy of itself, reported success, and was rejected again on the same 7.5 s
timing. It also spent the launch's one recovery attempt: the generation ladder
only climbs to `.prev` on the NEXT launch, so the user stayed offline until
they happened to restart.

Note what the snapshot's own gate could not catch: the run that produced it HAD
reported CONNECTED, which is exactly `capture_snapshot()`'s condition. A session
connecting is not evidence that the state it leaves behind will be accepted
next time — `previous_snapshot_dir()` already says so; what was missing was
acting on it before spending the attempt.
"""

import os
import types

import pytest

from core import profile_recovery
from main import MainWindow

LOGIN_STORE = os.path.join(
    "Default", "IndexedDB", "https_web.whatsapp.com_0.indexeddb.leveldb")
SESSION = "c77cc915f87e4b1a371ebe2c105cee9b"


def _write_login_store(root, payload):
    path = os.path.join(root, LOGIN_STORE)
    os.makedirs(path, exist_ok=True)
    for name, content in payload.items():
        with open(os.path.join(path, name), "w", encoding="utf-8") as f:
            f.write(content)
    return path


@pytest.fixture
def gd(tmp_path):
    return str(tmp_path)


def _seed(gd, live=None, snapshot=None, previous=None):
    if live is not None:
        _write_login_store(profile_recovery.profile_dir(gd, SESSION), live)
    if snapshot is not None:
        _write_login_store(profile_recovery.snapshot_dir(gd, SESSION), snapshot)
    if previous is not None:
        _write_login_store(
            profile_recovery.previous_snapshot_dir(gd, SESSION), previous)


class TestTheComparison:
    def test_an_identical_snapshot_is_recognised(self, gd):
        payload = {"000003.log": "same", "CURRENT": "MANIFEST-000002"}
        _seed(gd, live=payload, snapshot=dict(payload))
        assert profile_recovery.snapshot_matches_live_profile(gd, SESSION) is True

    def test_different_content_is_not_a_match(self, gd):
        _seed(gd, live={"000003.log": "aaaa"}, snapshot={"000003.log": "bbbbbb"})
        assert profile_recovery.snapshot_matches_live_profile(gd, SESSION) is False

    def test_a_different_file_count_is_not_a_match(self, gd):
        _seed(gd,
              live={"000003.log": "x", "000004.log": "y"},
              snapshot={"000003.log": "x"})
        assert profile_recovery.snapshot_matches_live_profile(gd, SESSION) is False

    def test_it_can_read_the_previous_generation(self, gd):
        payload = {"000003.log": "same"}
        _seed(gd, live=payload, snapshot={"000003.log": "other"},
              previous=dict(payload))
        assert profile_recovery.snapshot_matches_live_profile(
            gd, SESSION, prefer_previous=True) is True

    def test_an_unreadable_side_never_blocks_a_restore(self, gd):
        """An unknown fingerprint must not talk the caller out of an attempt
        that might work. Both directions answer False, which means 'go ahead'."""
        _seed(gd, live={"000003.log": "x"})          # no snapshot at all
        assert profile_recovery.snapshot_matches_live_profile(gd, SESSION) is False

        other = os.path.join(gd, "other")
        _seed(other, snapshot={"000003.log": "x"})   # no live profile
        assert profile_recovery.snapshot_matches_live_profile(other, SESSION) is False

    def test_a_faithful_copy_fingerprints_identically(self, gd):
        """The comparison only works because restore_snapshot() copies with
        shutil.copy2, which preserves mtimes. If that ever changes, a genuine
        no-op restore stops being recognisable and this test is the warning."""
        _seed(gd, live={"000003.log": "payload", "CURRENT": "MANIFEST-000002"})
        profile_recovery.capture_snapshot(gd, SESSION)
        assert profile_recovery.snapshot_matches_live_profile(gd, SESSION) is True


# ── the policy: what the recovery does about it ─────────────────────────────


class _MetadataDB:
    def __init__(self):
        self.values = {}

    def get_metadata_json(self, key, default=None):
        return self.values.get(key, default)

    def set_metadata_json(self, key, value):
        self.values[key] = value


class _Stub:
    _PROFILE_RECOVERY_GENERATION_KEY = MainWindow._PROFILE_RECOVERY_GENERATION_KEY
    _profile_recovery_generation = MainWindow._profile_recovery_generation
    _set_profile_recovery_generation = MainWindow._set_profile_recovery_generation

    def __init__(self, global_dir):
        self.settings = {"privateinfo": {"paired": True}}
        self.token = SESSION + ":tok"
        self.global_dir = global_dir
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.background_mode = True
        self.audits = []
        self.announced = []
        self.error_sound = types.SimpleNamespace(play=lambda: None)
        self.i18n = types.SimpleNamespace(t=lambda key: key)
        self.db = _MetadataDB()
        self.restored_with = []

    def wait_for_profile_release(self, session_name, timeout=20.0):
        return True

    def _shutdown_audit(self, msg):
        self.audits.append(msg)

    def output(self, text, interrupt=False):
        self.announced.append(text)

    def _announce_profile_beyond_repair(self):
        MainWindow._announce_profile_beyond_repair(self)

    def _announce_profile_restored(self):
        MainWindow._announce_profile_restored(self)


class _InlineThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


@pytest.fixture
def recovery(monkeypatch):
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    monkeypatch.setattr("main.threading.Thread", _InlineThread)
    monkeypatch.setattr("main.api_post", lambda *a, **kw: None)

    def _restore(global_dir, session_name, prefer_previous=False):
        _restore.calls.append(prefer_previous)
        return True
    _restore.calls = []
    monkeypatch.setattr("core.profile_recovery.restore_snapshot", _restore)
    return _restore


class TestItDoesNotRestoreTheFailure:
    def test_an_identical_snapshot_is_not_restored(self, gd, recovery):
        """The reported case: one snapshot, byte-identical to the rejected
        profile, and no previous generation to climb to."""
        payload = {"000003.log": "same"}
        _seed(gd, live=payload, snapshot=dict(payload))
        stub = _Stub(gd)

        assert MainWindow._recover_suspect_profile(stub) is False
        assert recovery.calls == []

    def test_the_user_is_told_instead_of_left_offline(self, gd, recovery):
        """Reporting a success that changed nothing is worse than silence: the
        session is unrecoverable locally and only the user can decide to pair
        again."""
        payload = {"000003.log": "same"}
        _seed(gd, live=payload, snapshot=dict(payload))
        stub = _Stub(gd)

        MainWindow._recover_suspect_profile(stub)

        assert "profile_corrupted_repair_needed" in stub.announced

    def test_it_is_audited(self, gd, recovery):
        """shutdown_audit.log is the only file surviving the next launch, and
        this verdict is what the user's next report needs."""
        payload = {"000003.log": "same"}
        _seed(gd, live=payload, snapshot=dict(payload))
        stub = _Stub(gd)

        MainWindow._recover_suspect_profile(stub)

        assert any("every snapshot matches" in line for line in stub.audits)


class TestItClimbsWithinTheSameLaunch:
    def test_a_matching_newest_falls_through_to_previous(self, gd, recovery):
        """Waiting for the next launch to climb is what left the user offline:
        the attempt was spent on a restore that provably could not help."""
        payload = {"000003.log": "same"}
        _seed(gd, live=payload, snapshot=dict(payload),
              previous={"000003.log": "an older, different state"})
        stub = _Stub(gd)

        assert MainWindow._recover_suspect_profile(stub) is True
        assert recovery.calls == [True]

    def test_a_matching_previous_too_gives_up(self, gd, recovery):
        payload = {"000003.log": "same"}
        _seed(gd, live=payload, snapshot=dict(payload), previous=dict(payload))
        stub = _Stub(gd)

        assert MainWindow._recover_suspect_profile(stub) is False
        assert recovery.calls == []


class TestTheOrdinaryPathIsUnchanged:
    def test_a_differing_snapshot_is_still_restored(self, gd, recovery):
        _seed(gd, live={"000003.log": "broken"},
              snapshot={"000003.log": "a known-good state"})
        stub = _Stub(gd)

        assert MainWindow._recover_suspect_profile(stub) is True
        assert recovery.calls == [False]

    def test_the_generation_ladder_still_climbs_across_launches(self, gd, recovery):
        """Unchanged behaviour: a restore that did not hold makes the NEXT
        launch prefer the older generation."""
        _seed(gd, live={"000003.log": "broken"},
              snapshot={"000003.log": "one"}, previous={"000003.log": "two"})
        stub = _Stub(gd)

        MainWindow._recover_suspect_profile(stub)
        stub._profile_recovery_attempted = False       # a new launch
        MainWindow._recover_suspect_profile(stub)

        assert recovery.calls == [False, True]
