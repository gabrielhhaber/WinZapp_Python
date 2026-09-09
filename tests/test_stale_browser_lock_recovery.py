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


class TestASupersededCreateMustNotKillItsSuccessor:
    """The userDataDir kill is a fallback, and it is not always safe.

    `killBrowserOrFallback()` tries a precise process-tree kill through
    `wppClient?.page` first, but `wppClient` is only assigned once create()
    returns — so during create() itself it is in the temporal dead zone and the
    fallback always runs. That fallback kills whatever browser currently holds
    the profile, which after a takeover is somebody else's.

    Measured live on 2026-09-09:

        03:55:50  Connected / inChat        (restored profile, syncing)
        03:56:01  Auth probe has failed for 30s straight — giving up
        03:56:02  shouldClose detected in statusFind. Force-killing browser.
        03:56:02  browserClose

    The 30 s auth-probe bound belonged to a create() started 45 s earlier
    against the *broken* profile. While it counted, WinZapp restored the
    profile and the health poll started a second session that connected and
    began syncing. The old create() then timed out and killed the new one's
    browser by directory. WhatsApp logged that session out on the next load,
    and the once-per-launch profile recovery had already been spent.
    """

    def _kill_helper(self, source):
        start = source.index("const killBrowserOrFallback = () => {")
        return source[start:source.index("};", start)]

    def test_a_precise_kill_is_still_unconditional(self, source):
        """It can only reach this create()'s own page, so it cannot touch a
        successor and must not be gated on ownership."""
        body = self._kill_helper(source)
        assert body.index("forceKillBrowserProcess") < body.index("clientsArray[session]")

    def test_the_directory_scan_is_gated_on_still_owning_the_session(self, source):
        body = self._kill_helper(source)
        gate = body.index("current !== client")
        assert gate < body.index("forceKillByUserDataDir")

    def test_a_superseded_create_returns_without_killing(self, source):
        body = self._kill_helper(source)
        superseded = body[body.index("current !== client"):]
        assert "return;" in superseded[:superseded.index("forceKillByUserDataDir")]

    def test_the_refusal_is_logged(self, source):
        """Silently not killing is as hard to diagnose as wrongly killing."""
        body = self._kill_helper(source)
        assert "superseded" in body

    def test_the_session_slot_is_only_cleared_when_it_is_ours(self, source):
        """Clearing it would drop a successor's client, leaving the session
        unreachable while its browser keeps running."""
        helper = source[source.index("const clearSessionSlotIfStillOurs = () => {"):]
        helper = helper[:helper.index("};")]
        assert "clientsArray[session] === client" in helper

    @pytest.mark.parametrize("branch", ["catchLinkCode", "catchQR", "statusFind", "poller"])
    def test_every_shouldClose_branch_uses_the_guarded_clear(self, source, branch):
        marker = f"shouldClose detected in {branch}." if branch != "poller" \
            else "shouldClose detected by poller."
        # rindex: the same sentence appears in killBrowserOrFallback()'s own
        # comment, which quotes the log line this was diagnosed from.
        tail = source[source.rindex(marker):]
        end = tail.index("}, 2000);") if branch == "poller" else 400
        window = tail[:end]
        assert "clearSessionSlotIfStillOurs()" in window
        assert "clientsArray[session] = undefined" not in window


class TestEveryKillAsksFirst:
    """Force-killing a Chrome that is mid-write is the leading explanation for
    the profile losses this file's recovery keeps having to repair.

    What that Chrome is writing is WhatsApp Web's IndexedDB — the sole carrier
    of the login, since WPPConnect's token store is empty on a real install.
    The resulting failure is not a corrupt database, which is what made it hard
    to see: measured across several losses, the profile comes back
    structurally perfect, differing from a working snapshot only by ordinary
    LevelDB compaction, with a shutdown fingerprint identical to the one the
    next launch reads — and WhatsApp Web still answers `post_logout=1` seven
    seconds in while an older copy of the same profile authenticates. Nothing
    on disk is broken; the state in it stopped matching the server's, which is
    what killing a browser part-way through a key rotation would produce.

    So every kill site asks first.
    """

    def _helper(self, source, name):
        start = source.index(f"async function {name}(")
        return source[start:source.index("\n}\n", start)]

    def test_the_graceful_close_waits_for_the_process_to_be_gone(self, source):
        """The call returning is not the process exiting, and a caller about to
        relaunch against this profile must not race a Chrome still flushing."""
        body = self._helper(source, "closeBrowserGracefully")
        assert "exitCode" in body and "deadline" in body

    def test_it_never_throws(self, source):
        """A failure here just means the caller force-kills, which is what it
        did unconditionally before."""
        body = self._helper(source, "closeBrowserGracefully")
        assert "catch" in body

    def test_it_reports_whether_the_browser_actually_went(self, source):
        body = self._helper(source, "closeBrowserGracefully")
        assert "return true;" in body and "return false;" in body

    def test_the_stale_lock_recovery_asks_before_killing(self, source):
        body = self._helper(source, "launchWithStaleBrowserRecovery")
        assert body.index("closeBrowserGracefully") < body.index("forceKillByUserDataDir")

    def test_force_kill_session_asks_before_killing(self, source):
        start = source.index("async forceKillSession(")
        body = source[start:source.index("\n  }\n", start)]
        assert body.index("closeBrowserGracefully") < body.index("forceKillBrowserProcess")

    def test_it_can_be_told_not_to_ask_twice(self, source):
        """Only where a close has already been tried and failed — asking again
        would just spend the caller's budget a second time."""
        start = source.index("async forceKillSession(")
        body = source[start:source.index("\n  }\n", start)]
        assert "graceful = true" in body
