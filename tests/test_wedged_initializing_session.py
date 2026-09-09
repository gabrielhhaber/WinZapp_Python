"""A failed session start must never be able to wedge the account offline.

Measured on a real install on 2026-09-09, and the account was offline in
silence for the rest of the launch:

    09:49:38  QR for a paired install -> _recover_suspect_profile() starts;
              close-session, then wait_for_profile_release() for up to 20 s
    09:49:52  the 30 s health poll reads CLOSED and fires its own
              /start-session into a profile Chrome is still holding
    09:49:54  Error: The browser is already running for ...userDataDir\\47d16ae...
    09:49:58  Python kills the ten Chrome processes holding the lock
    09:49:59  profile restored from the snapshot -- the same bytes that had
              connected fine eight hours earlier
    09:50:22  status-session INITIALIZING ... and every 30 s after that,
              forever, with no chrome.exe in existence

The restore worked. Nothing could start a session on it. Confirmed live
against the still-running server: status-session answered INITIALIZING and
POST /start-session answered HTTP 200 while launching no browser at all.

Two independent faults, either one enough on its own, so both are pinned here:

* createSessionUtil()'s catch reset the status to CLOSED only for a
  TimeoutError. "The browser is already running" is a plain Error, so the
  status stayed INITIALIZING -- and INITIALIZING is terminal on both sides:
  createSessionUtil() returns early for any status but CLOSED, and
  check_wa_connection_http() skips /start-session for every "active" state.
* _recover_suspect_profile() owns the browser and the profile for the whole
  close/kill/restore cycle and said so to nobody, so the health poll raced it.
  _recovery_restart_active is the flag that exists for exactly this.

The TypeScript only compiles inside client/api/, which does not exist in the
test job, so the Node half is asserted against the patched source the way
tests/test_stale_browser_lock_recovery.py does for the same file.
"""

import re
import types
from pathlib import Path

import pytest

import connection_state as cs
from main import MainWindow


PATCHED_UTIL = (
    Path(__file__).resolve().parents[1]
    / "client" / "api_patches" / "src" / "util" / "createSessionUtil.ts"
)


@pytest.fixture(scope="module")
def source():
    return PATCHED_UTIL.read_text(encoding="utf-8")


def _strip_comments(text):
    """Comments explain the very failures these assertions look for, so they
    would satisfy a substring check on their own."""
    return "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith("//"))


def _catch_block(source):
    """createSessionUtil()'s own trailing catch, sliced out by structure rather
    than by a quoted copy of its body -- a copy would keep passing after the
    real one was narrowed back to TimeoutError."""
    start = source.index("  async createSessionUtil(")
    end = source.index("  async opendata(", start)
    body = source[start:end]
    catch = body.rindex("} catch (e) {")
    return _strip_comments(body[catch:])


class TestAFailedStartLeavesTheSessionStartable:
    def test_the_reset_is_no_longer_limited_to_a_timeout(self, source):
        """The exact error that wedged the reporting install was
        `Error: The browser is already running for <userDataDir>` -- name
        'Error', not 'TimeoutError'. Gating the reset on the error type means
        every other way create() can fail is permanent."""
        catch = _catch_block(source)
        assert "TimeoutError" not in catch, (
            "the status reset is gated on the error type again; a start that "
            "fails any other way leaves the session INITIALIZING forever"
        )
        assert re.search(r"\.status\s*=\s*'CLOSED'", catch), (
            "nothing in the catch resets the status, so a failed start is "
            "terminal for the whole account"
        )

    def test_it_only_touches_this_attempts_own_client(self, source):
        """A create() superseded by a newer one must never write CLOSED over
        the successor's status: the next /start-session would then launch a
        duplicate Chrome onto a profile a live session is using. That is the
        same failure killBrowserOrFallback() refuses the userDataDir scan for,
        and it cost a working session on 2026-09-08."""
        catch = _catch_block(source)
        assert "clientsArray[session] === failed" in catch, (
            "the catch no longer checks that the slot still holds this "
            "attempt's client"
        )

    def test_it_only_rewrites_a_status_nothing_else_promoted(self, source):
        """The try covers everything past this.start() too -- the webhook
        wirings. A throw from one of those comes from a session that already
        reached a real state, and reporting it CLOSED hands it the same
        duplicate launch."""
        catch = _catch_block(source)
        assert "failed.status === 'INITIALIZING'" in catch

    def test_the_client_is_captured_where_ownership_is_taken(self, source):
        """`client` is declared inside the try and is out of scope in the
        catch, which is why the old code had to re-resolve it through
        getClient() -- and why it could not tell a superseded attempt apart.
        The capture has to happen after the CLOSED guard, or it names a client
        this attempt never owned."""
        start = source.index("  async createSessionUtil(")
        body = source[start:source.index("  async opendata(", start)]
        assert body.index("ownClient = client") > body.index(
            "if (client.status != null && client.status !== 'CLOSED')")
        assert body.index("ownClient = client") < body.index(
            "client.status = 'INITIALIZING'")

    def test_the_silent_early_return_is_logged(self, source):
        """startSession() answers HTTP 200 either way, so WinZapp logged
        'Sent auto-start session command' every 30 s for a session no code path
        was ever going to start. The silence is most of why this took a live
        probe to find."""
        start = source.index("  async createSessionUtil(")
        body = source[start:source.index("  async opendata(", start)]
        guard = body[:body.index("client.status = 'INITIALIZING'")]
        assert "start-session ignored" in guard


class _MetadataDB:
    def __init__(self):
        self.values = {}

    def get_metadata_json(self, key, default=None):
        return self.values.get(key, default)

    def set_metadata_json(self, key, value):
        self.values[key] = value


class _SyncThread:
    """Runs the restore body on .start(), so a test can observe the flag both
    during the cycle (recorded from inside) and after it."""

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


class _Stub:
    """Carries only what _recover_suspect_profile() actually touches."""

    def __init__(self):
        self.settings = {"privateinfo": {"paired": True}}
        self.token = "sess123:tok"
        self.global_dir = "/g"
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.audits = []
        self.announced = []
        self.db = _MetadataDB()
        self.error_sound = types.SimpleNamespace(play=lambda: None)
        self.i18n = types.SimpleNamespace(t=lambda key: key)
        self._recovery_restart_active = False
        self.flag_while_waiting = None

    _PROFILE_RECOVERY_GENERATION_KEY = MainWindow._PROFILE_RECOVERY_GENERATION_KEY
    _profile_recovery_generation = MainWindow._profile_recovery_generation
    _set_profile_recovery_generation = MainWindow._set_profile_recovery_generation

    def _shutdown_audit(self, msg):
        self.audits.append(msg)

    def output(self, text, interrupt=False):
        self.announced.append(text)

    def wait_for_profile_release(self, session_name, timeout=None):
        # The 20 s window the health poll landed in on the reporting install.
        self.flag_while_waiting = self._recovery_restart_active
        return True

    def _announce_profile_restored(self):
        MainWindow._announce_profile_restored(self)

    def _announce_profile_beyond_repair(self):
        MainWindow._announce_profile_beyond_repair(self)


@pytest.fixture
def stub(monkeypatch):
    s = _Stub()
    monkeypatch.setattr("main.threading.Thread", _SyncThread)
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    monkeypatch.setattr("main.api_post", lambda *a, **kw: None)
    monkeypatch.setattr("core.profile_recovery.has_snapshot", lambda *a, **kw: True)
    return s


class TestTheRestoreOwnsTheBrowserWhileItRuns:
    def test_the_health_poll_is_blocked_for_the_whole_cycle(self, stub, monkeypatch):
        monkeypatch.setattr("core.profile_recovery.restore_snapshot",
                            lambda *a, **kw: True)
        assert MainWindow._recover_suspect_profile(stub) is True
        assert stub.flag_while_waiting is True, (
            "the profile restore ran without claiming the browser, so a "
            "competing /start-session can fire into a still-locked profile"
        )

    def test_the_flag_is_the_one_the_health_check_consults(self, stub, monkeypatch):
        """Pinned against connection_state rather than restated: a flag nothing
        reads would satisfy the test above and change nothing."""
        monkeypatch.setattr("core.profile_recovery.restore_snapshot",
                            lambda *a, **kw: True)
        MainWindow._recover_suspect_profile(stub)
        assert cs.auto_start_block_reason(
            pairing_dialog_active=False, qr_flood_halted=False,
            recovery_restart_active=True, self_inflicted_teardown=False,
        ) == cs.AUTO_START_BLOCKED_RECOVERY_RESTART

    def test_it_is_released_once_the_profile_is_back(self, stub, monkeypatch):
        """Held any longer and the very poll that should start a session on the
        restored profile is the one being blocked."""
        monkeypatch.setattr("core.profile_recovery.restore_snapshot",
                            lambda *a, **kw: True)
        MainWindow._recover_suspect_profile(stub)
        assert stub._recovery_restart_active is False

    def test_a_restore_that_fails_releases_it_too(self, stub, monkeypatch):
        monkeypatch.setattr("core.profile_recovery.restore_snapshot",
                            lambda *a, **kw: False)
        MainWindow._recover_suspect_profile(stub)
        assert stub._recovery_restart_active is False

    def test_a_restore_that_raises_releases_it_too(self, stub, monkeypatch):
        """A flag nobody clears blocks every auto-start for the life of the
        process -- strictly worse than the race it guards against."""
        def boom(*a, **kw):
            raise RuntimeError("disk went away")

        monkeypatch.setattr("core.profile_recovery.restore_snapshot", boom)
        MainWindow._recover_suspect_profile(stub)
        assert stub._recovery_restart_active is False
        assert "profile_corrupted_repair_needed" in stub.announced
