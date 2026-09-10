"""Never offer WhatsApp a profile state it has already refused.

The in-launch check (tests/test_restore_that_restores_the_failure.py) compares a
snapshot against the profile currently on disk. That is enough exactly once: the
moment a restore runs, the live profile stops being the rejected one, so on the
next launch there is nothing left to compare against. The user's log shows both
halves of that — a restore that put the rejected bytes back at 10:55:48, refused
again at 10:55:56, after which the live profile was evidence of nothing.

So the verdict is persisted, keyed by the same login-store fingerprint
`shutdown_audit.log` already records, and cleared the moment a session reaches
CONNECTED — because a state that authenticates now must not go on being refused
by a record from before it did.

What this cannot do is stop WhatsApp rejecting a state in the first place. The
key material lives inside WhatsApp Web and its server; WinZapp only chooses
which bytes to hand it. This makes that choice stop repeating itself.
"""

import os
import types

import pytest

from core import profile_recovery
from main import MainWindow

LOGIN_STORE = os.path.join(
    "Default", "IndexedDB", "https_web.whatsapp.com_0.indexeddb.leveldb")
SESSION = "c77cc915f87e4b1a371ebe2c105cee9b"
OTHER = "9de1c2bf73b3ec8b8891febfe2bc4d48"


def _store(root, payload):
    path = os.path.join(root, LOGIN_STORE)
    os.makedirs(path, exist_ok=True)
    for name, content in payload.items():
        with open(os.path.join(path, name), "w", encoding="utf-8") as f:
            f.write(content)


@pytest.fixture
def gd(tmp_path):
    return str(tmp_path)


def _seed(gd, session=SESSION, live=None, snapshot=None, previous=None):
    if live is not None:
        _store(profile_recovery.profile_dir(gd, session), live)
    if snapshot is not None:
        _store(profile_recovery.snapshot_dir(gd, session), snapshot)
    if previous is not None:
        _store(profile_recovery.previous_snapshot_dir(gd, session), previous)


class TestTheRecord:
    def test_a_verdict_survives_the_profile_being_replaced(self, gd):
        """The whole reason it is persisted: after a restore the live profile
        is no longer the rejected one."""
        _seed(gd, live={"000003.log": "rejected state"})
        fp = profile_recovery.login_store_fingerprint(gd, SESSION)
        profile_recovery.note_profile_rejected(gd, SESSION)

        _store(profile_recovery.profile_dir(gd, SESSION), {"000004.log": "moved on"})

        assert profile_recovery.profile_state_was_rejected(gd, SESSION, fp) is True

    def test_an_unknown_state_is_not_refused(self, gd):
        _seed(gd, live={"000003.log": "x"})
        assert profile_recovery.profile_state_was_rejected(
            gd, SESSION, "files=9 bytes=9 newest=9") is False

    def test_an_empty_fingerprint_is_not_refused(self, gd):
        """An unreadable profile must never be treated as a known-bad one —
        that would refuse a restore that might work."""
        assert profile_recovery.profile_state_was_rejected(gd, SESSION, None) is False
        assert profile_recovery.profile_state_was_rejected(gd, SESSION, "") is False

    def test_sessions_do_not_share_verdicts(self, gd):
        _seed(gd, live={"000003.log": "x"})
        fp = profile_recovery.login_store_fingerprint(gd, SESSION)
        profile_recovery.note_profile_rejected(gd, SESSION)
        assert profile_recovery.profile_state_was_rejected(gd, OTHER, fp) is False

    def test_the_history_is_bounded(self, gd):
        """A record that grew without bound would be a log, not a decision."""
        for i in range(profile_recovery._REJECTED_HISTORY + 5):
            profile_recovery.note_profile_rejected(
                gd, SESSION, "files=1 bytes=%d newest=1" % i)
        stored = profile_recovery._read_rejected(gd)[SESSION]
        assert len(stored) == profile_recovery._REJECTED_HISTORY
        assert stored[-1] == "files=1 bytes=%d newest=1" % (
            profile_recovery._REJECTED_HISTORY + 4)

    def test_recording_the_same_state_twice_does_not_fill_the_history(self, gd):
        for _ in range(5):
            profile_recovery.note_profile_rejected(gd, SESSION, "same")
        assert profile_recovery._read_rejected(gd)[SESSION] == ["same"]

    def test_connecting_clears_the_record(self, gd):
        """A state that authenticates now must not go on being refused by a
        verdict recorded before it did."""
        profile_recovery.note_profile_rejected(gd, SESSION, "fp")
        assert profile_recovery.clear_rejected_profiles(gd, SESSION) is True
        assert profile_recovery.profile_state_was_rejected(gd, SESSION, "fp") is False

    def test_a_corrupt_record_refuses_nothing(self, gd):
        """Fails open, like every other read here: the worst a lost verdict
        costs is one wasted restore, while a spurious one costs the account."""
        path = profile_recovery._rejected_path(gd)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        assert profile_recovery.profile_state_was_rejected(gd, SESSION, "fp") is False

    def test_recording_never_raises(self, gd):
        assert profile_recovery.note_profile_rejected(None, SESSION) is False


class TestSnapshotsAreCheckedAgainstIt:
    def test_a_snapshot_holding_a_refused_state_is_recognised(self, gd):
        payload = {"000003.log": "rejected"}
        _seed(gd, live=payload, snapshot=dict(payload))
        profile_recovery.note_profile_rejected(gd, SESSION)
        _store(profile_recovery.profile_dir(gd, SESSION), {"000009.log": "different now"})

        assert profile_recovery.snapshot_was_rejected(gd, SESSION) is True

    def test_an_untried_snapshot_is_not(self, gd):
        _seed(gd, live={"000003.log": "rejected"},
              snapshot={"000003.log": "never offered"})
        profile_recovery.note_profile_rejected(gd, SESSION)

        assert profile_recovery.snapshot_was_rejected(gd, SESSION) is False

    def test_the_previous_generation_is_checked_too(self, gd):
        payload = {"000003.log": "rejected"}
        _seed(gd, live=payload, snapshot={"000003.log": "other"},
              previous=dict(payload))
        profile_recovery.note_profile_rejected(gd, SESSION)

        assert profile_recovery.snapshot_was_rejected(
            gd, SESSION, prefer_previous=True) is True


# ── the policy ──────────────────────────────────────────────────────────────


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

    def wait_for_profile_release(self, session_name, timeout=20.0):
        return True

    def find_incomplete_browser(self):
        """A healthy browser by default. _recover_suspect_profile() refuses
        outright when the bundled Chromium cannot start, because a failed
        browser launch produces exactly the "died without connecting" the
        tracker counts — see tests/test_broken_browser_payload.py."""
        return None, None

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


class TestRecoveryRecordsAndRespectsTheVerdict:
    def test_the_rejected_profile_is_recorded_before_anything_moves_it(
            self, gd, recovery):
        _seed(gd, live={"000003.log": "rejected"},
              snapshot={"000003.log": "a different state"})
        fp = profile_recovery.login_store_fingerprint(gd, SESSION)

        MainWindow._recover_suspect_profile(_Stub(gd))

        assert profile_recovery.profile_state_was_rejected(gd, SESSION, fp) is True

    def test_a_snapshot_refused_on_an_earlier_launch_is_not_offered_again(
            self, gd, recovery):
        """The case the in-launch comparison cannot see: the live profile has
        already been replaced, so only the persisted verdict still knows."""
        payload = {"000003.log": "refused last time"}
        _seed(gd, live={"000009.log": "something else now"}, snapshot=dict(payload))
        _store(os.path.join(gd, "scratch"), payload)
        profile_recovery.note_profile_rejected(
            gd, SESSION,
            profile_recovery._fingerprint_login_store(os.path.join(gd, "scratch")))

        assert MainWindow._recover_suspect_profile(_Stub(gd)) is False
        assert recovery.calls == []

    def test_it_climbs_to_a_generation_that_was_never_refused(self, gd, recovery):
        payload = {"000003.log": "refused"}
        _seed(gd, live=dict(payload), snapshot=dict(payload),
              previous={"000003.log": "never tried"})

        assert MainWindow._recover_suspect_profile(_Stub(gd)) is True
        assert recovery.calls == [True]

    def test_an_untouched_snapshot_is_still_restored(self, gd, recovery):
        _seed(gd, live={"000003.log": "broken"},
              snapshot={"000003.log": "known good"})

        assert MainWindow._recover_suspect_profile(_Stub(gd)) is True
        assert recovery.calls == [False]

    def test_the_user_is_told_when_nothing_is_left(self, gd, recovery):
        payload = {"000003.log": "refused"}
        _seed(gd, live=dict(payload), snapshot=dict(payload))
        stub = _Stub(gd)

        MainWindow._recover_suspect_profile(stub)

        assert "profile_corrupted_repair_needed" in stub.announced
        assert any("already refused" in line for line in stub.audits)
