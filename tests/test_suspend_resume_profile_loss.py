"""Two ways a suspend cost a user their session and 21 hours of messages.

Reported after a laptop came back from 5.5 hours asleep. Nothing is killed by
suspending, so "the suspend broke my Chrome profile" reads as impossible — the
logs say otherwise, and name both steps.

**One: the restart raced Chrome's flush.** ``_restart_wpp_session()`` waited for
the session to report CLOSED and started a replacement browser 80 ms later::

    18:34:09.496  close-session  -> 200
    18:34:09.536  status-session -> CLOSED      (first gate, 40 ms)
    18:34:09.576  start-session  -> 200         (80 ms after the close)
    18:34:18      Session Unpaired -> post_logout=1&logout_reason=0

CLOSED is the first of two gates and never the second: it says WPPConnect's own
state machine finished, not that Chrome let go of ``userDataDir``.
``_stop_wpp_server()`` has always waited for both — "Neither substitutes for the
other" — and this path waited only for the first. The proof of how much that
mattered is in the same log: when the recovery finally ran the wait at 18:36:01
it reported *"still held after 20s"*. A Chrome resumed from a long suspend has a
whole session's state to write back, and 80 ms is not that.

**Two: the restore mirrored its own rollback.** The session then looked logged
out, the QR handler read that as a broken profile, and the snapshot that went
back was from 09-09 21:20 against a restore at 09-10 18:36 — WhatsApp Web
returned knowing 21 hours less than WinZapp's own database.
``_reconcile_active_conversation_with_remote()`` reads "the server has not got
this message" as "the phone deleted it", so it began deleting correctly-synced
history from the only complete copy there was.
"""

import inspect
import types

import pytest

from main import MainWindow


# ── One: the restart must wait for the profile, not just for CLOSED ─────────


class _RestartStub:
    _restart_wpp_session = MainWindow._restart_wpp_session
    _RECOVERY_CLOSE_WAIT = MainWindow._RECOVERY_CLOSE_WAIT
    _RESTART_PROFILE_RELEASE_WAIT = MainWindow._RESTART_PROFILE_RELEASE_WAIT
    _WPP_SESSION_RESTART_COOLDOWN = MainWindow._WPP_SESSION_RESTART_COOLDOWN

    def __init__(self, released=True):
        self.token = "sess123:tok"
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.calls = []
        self._released = released
        self.release_waits = []
        self._restarting_wpp_session = False
        self._last_wpp_restart_ts = 0.0
        self.offline_mode = False

    def wait_for_profile_release(self, session_name, timeout=20.0):
        self.calls.append("wait_for_profile_release")
        self.release_waits.append((session_name, timeout))
        return self._released

    def _wait_for_status(self, predicate, timeout, stop_when_connected=False):
        self.calls.append("wait_for_closed")
        return "CLOSED"

    def _set_wa_connected(self, *a, **kw):
        pass

    def _apply_offline_state(self, *a, **kw):
        pass


@pytest.fixture
def restart(monkeypatch):
    posted = []

    def _post(url, **kwargs):
        posted.append(url)
        if "close-session" in url:
            _stub = None
        return types.SimpleNamespace(status_code=200, text="{}")

    monkeypatch.setattr("main.api_post", _post)
    return posted


def _run(stub, restart):
    """Drive the real method, recording the order of its two waits."""
    original = stub.wait_for_profile_release
    MainWindow._restart_wpp_session(stub)
    return stub


class TestTheRestartWaitsForChromeToLetGo:
    def test_it_waits_for_the_profile_release(self, restart):
        stub = _RestartStub()
        MainWindow._restart_wpp_session(stub)
        assert "wait_for_profile_release" in stub.calls, (
            "the replacement browser opened the login database while the "
            "outgoing Chrome was still flushing it"
        )

    def test_the_wait_happens_after_closed_and_before_the_start(self, restart):
        """Order is the whole fix. Waiting before the close would watch a
        browser nobody has asked to stop; waiting after the start is too
        late."""
        stub = _RestartStub()
        MainWindow._restart_wpp_session(stub)

        assert stub.calls.index("wait_for_closed") < stub.calls.index(
            "wait_for_profile_release")
        start_index = next(i for i, u in enumerate(restart) if "start-session" in u)
        close_index = next(i for i, u in enumerate(restart) if "close-session" in u)
        assert close_index < start_index

    def test_a_browser_that_never_lets_go_still_starts(self, restart):
        """Refusing to start would leave the account offline for good over a
        profile nothing may ever release. Starting anyway is what this did
        before the wait existed, and createSessionUtil's stale-lock recovery
        is the net under it."""
        stub = _RestartStub(released=False)
        MainWindow._restart_wpp_session(stub)
        assert any("start-session" in u for u in restart)

    def test_the_wait_is_sized_for_a_resumed_browser(self):
        """Measured worst case on this path was 20 s — a Chrome resumed from a
        long suspend. A budget at or under that reintroduces the race."""
        assert MainWindow._RESTART_PROFILE_RELEASE_WAIT > 20.0

    def test_it_never_starts_a_browser_the_close_could_not_confirm(self, restart):
        """Pre-existing behaviour, pinned because the new wait sits right next
        to it: a session that never reached CLOSED must not get a replacement
        browser stacked on top."""
        stub = _RestartStub()
        stub._wait_for_status = lambda *a, **kw: "INITIALIZING"
        MainWindow._restart_wpp_session(stub)
        assert not any("start-session" in u for u in restart)


# ── Two: a rolled-back store is not evidence of a deletion ──────────────────


class _ReconcileStub:
    _reconcile_active_conversation_with_remote = (
        MainWindow._reconcile_active_conversation_with_remote)
    _normalize_jid = staticmethod(MainWindow._normalize_jid)

    _REMOTE_CLEAR_CONFIRM_STRIKES = MainWindow._REMOTE_CLEAR_CONFIRM_STRIKES

    def __init__(self, untrusted=False):
        self._remote_deletions_untrusted = untrusted
        self._remote_clear_strikes = {}
        self.messages_set_completed = True
        self.settings = {"user_interface": {"messages_page_size": 200}}
        self.chats = {}
        self.fetched = []
        jid = "5511900000001@s.whatsapp.net"
        self.conversations_panel = types.SimpleNamespace(
            conversation={"remoteJid": jid})
        self.chats[jid] = {
            "messages": {"messages": {"records": [
                {"key": {"id": "a"}, "messageTimestamp": 1},
                {"key": {"id": "b"}, "messageTimestamp": 2},
                {"key": {"id": "c"}, "messageTimestamp": 3},
            ]}}
        }

    def _fetch_remote_message_ids(self, remote_jid):
        self.fetched.append(remote_jid)
        return set()          # the rolled-back store knows nothing


class TestARolledBackStoreIsNotADeletion:
    def test_a_restored_profile_stops_the_mirroring(self):
        """The data loss: every locally-stored message looks deleted to a
        store that was rolled back behind us, and mirroring destroys the only
        complete copy."""
        stub = _ReconcileStub(untrusted=True)
        MainWindow._reconcile_active_conversation_with_remote(stub)
        assert stub.fetched == [], (
            "it asked the rolled-back server what it has — the next step is "
            "deleting everything the answer leaves out"
        )

    def test_an_ordinary_launch_still_reconciles(self):
        stub = _ReconcileStub(untrusted=False)
        MainWindow._reconcile_active_conversation_with_remote(stub)
        assert stub.fetched, "phone-side deletions must still be mirrored"

    def test_the_restore_sets_the_flag(self):
        """Wired at the one place a rollback can happen — the successful
        restore — rather than inferred later from a stale-looking server."""
        src = inspect.getsource(MainWindow._recover_suspect_profile)
        restored = src.index('"profile restored from snapshot"')
        assert "_remote_deletions_untrusted = True" in src[restored:], (
            "a successful restore must mark the server's view untrustworthy"
        )

    def test_the_flag_defaults_to_trusting_the_server(self):
        """Read with getattr(..., False): an install that never restored
        anything must behave exactly as before."""
        stub = _ReconcileStub()
        del stub._remote_deletions_untrusted
        MainWindow._reconcile_active_conversation_with_remote(stub)
        assert stub.fetched
