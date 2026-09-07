"""A dead Chrome must not be able to hold a session offline forever.

Whenever a session dies *after* its browser launched, the browser stays alive
holding `userDataDir/<session>` while `client.status` goes to CLOSED. From
there WinZapp could not recover on its own, and the loop is entirely mechanical:

    status-session -> CLOSED
    POST /start-session -> 200
    Error: The browser is already running for ...\\userDataDir\\db02b6c4...
           Use a different `userDataDir` or stop the running browser first.
    (30s later, identically, forever)

Nothing in that loop ever touches the process holding the lock. Measured on a
real install on 2026-09-07 across two consecutive launches, both times only
broken by the user giving up and disconnecting by hand. What put the session
there was a wa-js injection timeout (see
tests/test_wa_js_supported_range_pin.py), but the recovery has to work for any
cause — the lock is the same whatever killed the session.

createSessionUtil.ts is TypeScript that only compiles inside client/api/, which
does not exist in the test job, so this asserts against the patched source the
way tests/test_shutdown_closing_state.py does for the same file. The one piece
that is real behaviour rather than structure — the discriminator deciding
whether an error means "stale profile lock" — is extracted and exercised
directly, because a wrong answer there is the difference between recovering and
killing a browser that was doing its job.
"""

import re
from pathlib import Path

import pytest


PATCHED_UTIL = (
    Path(__file__).resolve().parents[1]
    / "client" / "api_patches" / "src" / "util" / "createSessionUtil.ts"
)


@pytest.fixture(scope="module")
def source():
    return PATCHED_UTIL.read_text(encoding="utf-8")


def _discriminator_pattern(source):
    """The regex literal shipped in isStaleBrowserLockError(), read out of the
    source rather than restated here — a copy would keep passing after the real
    one was narrowed or widened."""
    body = source[source.index("function isStaleBrowserLockError("):]
    match = re.search(r"return\s+/(.+?)/i\.test\(message\)", body)
    assert match, "isStaleBrowserLockError no longer tests a case-insensitive regex"
    return re.compile(match.group(1), re.IGNORECASE)


class TestTheDiscriminatorRecognisesOnlyAStaleProfileLock:
    def test_it_matches_puppeteers_real_message(self, source):
        """Verbatim from the failing install's wppconnect.log."""
        assert _discriminator_pattern(source).search(
            "The browser is already running for "
            "C:\\winzapp\\dist\\WinZapp\\data\\global\\api\\userDataDir\\db02b6c4e7. "
            "Use a different `userDataDir` or stop the running browser first."
        )

    @pytest.mark.parametrize("message", [
        "Session not found",
        "Chat not found",
        "Waiting failed: 30000ms exceeded",
        "Protocol error (Target.createTarget): Target closed",
        "net::ERR_INTERNET_DISCONNECTED",
        "",
    ])
    def test_it_matches_nothing_else(self, source, message):
        """Deliberately narrow, for the same reason `chat not found` is matched
        by its exact phrase: killing a browser on an unrelated failure is a far
        worse bug than the one being fixed."""
        assert not _discriminator_pattern(source).search(message)


class TestTheRecoveryIsWiredIntoTheLaunch:
    def test_create_is_reached_through_the_recovery_wrapper(self, source):
        """A bare `await create(...)` is the bug — it is what could not
        recover."""
        assert "launchWithStaleBrowserRecovery(" in source
        assert re.search(
            r"const wppClient = await launchWithStaleBrowserRecovery\(", source
        ), "create() is no longer launched through the recovery wrapper"
        assert not re.search(r"const wppClient = await create\(", source)

    def test_the_kill_is_scoped_to_this_sessions_profile(self, source):
        """Never "kill Chrome": the profile is per session, so the only process
        this may touch is the one holding this session's own userDataDir."""
        call = re.search(
            r"await launchWithStaleBrowserRecovery\(\s*launchWppClient,\s*"
            r"`userDataDir/\$\{session\}`",
            source,
        )
        assert call, "the recovery is no longer scoped to userDataDir/<session>"


@pytest.fixture(scope="module")
def recovery(source):
    start = source.index("async function launchWithStaleBrowserRecovery(")
    return source[start:source.index("\n}", start)]


@pytest.fixture(scope="module")
def kill(source):
    start = source.index("function forceKillByUserDataDir(")
    return source[start:source.index("\n}", source.index("new Promise", start))]


class TestTheRetryIsBoundedAndOrdered:
    def test_an_unrelated_failure_is_rethrown_untouched(self, recovery):
        assert "if (!isStaleBrowserLockError(error)) throw error;" in recovery

    def test_the_kill_is_awaited_before_relaunching(self, recovery):
        """Stop-Process returns before Windows has released the profile's file
        handles; relaunching without waiting hits the identical error and burns
        the one retry for nothing."""
        killed_at = recovery.index("await forceKillByUserDataDir(")
        settled_at = recovery.index("STALE_BROWSER_RELEASE_MS")
        relaunched_at = recovery.index("await launch()", killed_at)
        assert killed_at < settled_at < relaunched_at

    def test_there_is_exactly_one_retry(self, recovery):
        """A loop would spin Chrome launches forever against a profile held by
        something we cannot kill — another Windows user, a debugger, an
        antivirus."""
        assert recovery.count("launch()") == 2
        assert not re.search(r"\b(for|while)\s*\(", recovery)

    def test_a_failed_retry_still_propagates(self, recovery):
        assert "throw retryError;" in recovery


class TestTheKillCanActuallyBeAwaited:
    """Awaiting a function that returns undefined is a no-op that reads like a
    fix — the recovery would relaunch into the same lock every time."""

    def test_it_returns_a_promise(self, kill):
        assert "): Promise<void> {" in kill
        assert "return new Promise<void>((resolve) => {" in kill

    def test_every_path_out_of_it_settles(self, kill):
        """Including the empty-argument guard, which returns before the promise
        body ever runs."""
        assert "if (!userDataDir) return Promise.resolve();" in kill
        # One for the Windows PowerShell path, one for the POSIX pkill path.
        assert kill.count("resolve()") >= 2
