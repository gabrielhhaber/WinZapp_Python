"""A Chrome profile that restores its tabs starves the page WPPConnect drives.

WPPConnect drives exactly one page. When the session's Chrome profile reopens
the tabs it had last time, that page gets company — and the company is actively
harmful rather than merely untidy, because Chrome opens those tabs itself:

  * they never pass through start.js's document-only interception, so they load
    whatever build Meta is serving right now instead of the pinned one;
  * they never receive wppconnect's user-agent override, so WhatsApp answers
    them with the unsupported-browser screen and no wa-js runs in them at all;
  * they still share the profile's IndexedDB and its single WAWebBackendWorker
    with the page that matters.

Measured live on 2026-09-10 by attaching to the wedged browser over its own
DevTools port. Three ``web.whatsapp.com`` targets in one profile::

    ua=Chrome/102.0.5005.63        title="(30) WhatsApp"   WPP.isReady=true
    ua=HeadlessChrome/148.0.0.0    title=""                WPP undefined
    ua=HeadlessChrome/148.0.0.0    title=""                WPP undefined

The two extras were created 0.6 s *before* puppeteer's own page — they are a
session restore, not something the app opened. The page that mattered did reach
``WPP.isReady``, but only after ``injectApi()``'s 30 s bound had already
expired, so the session was reported dead. Nothing was logged out; it was
starved. On a fresh profile with the same Chrome, the same pinned build and the
same wa-js, ``WAPI && Store && WPP.isReady`` came back true in 3.1 s.

The loop is self-feeding, which is why it never recovered on its own: the
timeout leaves Chrome alive, the recovery force-kills it, a force-killed Chrome
records no clean exit, and the next launch therefore restores the tabs of the
run before — one more each time. Fifteen orphan Chromes had accumulated behind
the profile by the time it was looked at.

createSessionUtil.ts is TypeScript that only compiles inside client/api/, which
does not exist in the test job, so this asserts against the patched source the
way tests/test_stale_browser_lock_recovery.py does for the same file. The one
piece that is real behaviour rather than structure — which filenames the sweep
selects — is extracted and exercised directly, because deleting the wrong file
in a Chrome profile is a much worse bug than the one being fixed.
"""

import re
from pathlib import Path

import pytest


PATCHES = Path(__file__).resolve().parents[1] / "client" / "api_patches"
PATCHED_UTIL = PATCHES / "src" / "util" / "createSessionUtil.ts"


@pytest.fixture(scope="module")
def source():
    return PATCHED_UTIL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def sweep(source):
    start = source.index("function clearRestorableSession(")
    return source[start : source.index("\n}", start)]


def _session_file_pattern(sweep):
    """The regex literal shipped in clearRestorableSession(), read out of the
    source rather than restated here — a copy would keep passing after the real
    one was widened."""
    match = re.search(r"if \(!/(.+?)/\.test\(name\)\) continue;", sweep)
    assert match, "the sweep no longer filters Sessions/ entries by name"
    return re.compile(match.group(1))


class TestTheSweepSelectsOnlyTabState:
    @pytest.mark.parametrize(
        "name",
        [
            # Verbatim from the wedged install's profile.
            "Session_13433519014419047",
            "Tabs_13433519014575974",
            "Session_13433302293117688",
            "Tabs_13433302293291382",
        ],
    )
    def test_it_matches_chromes_saved_tab_records(self, sweep, name):
        assert _session_file_pattern(sweep).search(name)

    @pytest.mark.parametrize(
        "name",
        [
            # Nothing else in a profile may ever be removed by this — the
            # WhatsApp login lives in the profile and there is no second copy
            # of it anywhere.
            "Preferences",
            "Secure Preferences",
            "Local Storage",
            "IndexedDB",
            "Cookies",
            "Login Data",
            "History",
            "my Session_backup",
            "TabsBackup",
            "",
        ],
    )
    def test_it_matches_nothing_else(self, sweep, name):
        assert not _session_file_pattern(sweep).search(name)


class TestBothHalvesOfTheFixAreThere:
    """Either half alone leaves a way back in: files left behind are a restore
    record, and an exit that was never recorded as clean is an invitation to
    restore whatever is left."""

    def test_the_loose_restore_files_are_removed_too(self, source):
        for name in ("Last Session", "Last Tabs", "Current Session", "Current Tabs"):
            assert f"'{name}'" in source

    def test_a_clean_exit_is_recorded(self, sweep):
        assert "prefs.profile.exit_type = 'Normal';" in sweep
        assert "prefs.profile.exited_cleanly = true;" in sweep

    def test_preferences_is_merged_and_never_rewritten(self, sweep):
        """Preferences carries far more than the exit record. Replacing the file
        drops every setting the profile has accumulated."""
        read_at = sweep.index("JSON.parse(fs.readFileSync(prefsPath")
        write_at = sweep.index("fs.writeFileSync(prefsPath")
        assert read_at < write_at

    def test_an_unreadable_preferences_is_left_alone(self, sweep):
        """Not defaulting to `{}` on a read failure, for the same reason the
        token store must not: writing an empty object makes a transient error
        permanent."""
        block = sweep[sweep.index("const prefsPath") :]
        assert "JSON.parse(fs.readFileSync" in block
        assert re.search(r"\} catch \(e\) \{\}", block)
        assert "= {}" not in block.split("prefs.profile = prefs.profile")[0]


class TestTheSweepCanNeverBlockAStart:
    """It runs on the startup path of every session. A profile that cannot be
    tidied is not a reason to refuse to start one."""

    def test_it_returns_nothing_and_swallows_every_failure(self, source, sweep):
        assert "function clearRestorableSession(profileDir: string, logger?: any): void {" in source
        assert "if (!profileDir) return;" in sweep
        # An outer catch around the whole body, plus per-operation ones so a
        # single undeletable file does not abandon the rest of the sweep.
        assert sweep.count("catch") >= 4
        assert "throw" not in sweep


class TestChromeIsAlsoToldNotToRestore:
    """Belt to the sweep's braces, for a restore record that appears anyway —
    a crash mid-session, or a profile restored from a snapshot taken elsewhere.

    Both argument lists have to carry them. merge-deep UNIONS config.ts's
    browserArgs with start.js's rather than replacing them, so a flag present
    in only one still reaches Chrome — but a flag deleted from one and left in
    the other is exactly the trap that kept the rasterizer flags alive through
    the video-send fix (see tests/test_browser_close_timers_and_flags.py).
    Pinning both keeps the two lists honest.
    """

    @pytest.mark.parametrize(
        "flag", ["--hide-crash-restore-bubble", "--disable-session-crashed-bubble"]
    )
    @pytest.mark.parametrize("path", ["src/config.ts", "start.js"])
    def test_the_crash_restore_flags_are_in_both_lists(self, path, flag):
        text = (PATCHES / path).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("//")
        )
        assert f"'{flag}'" in code


class TestTheSweepRunsBeforeEveryLaunch:
    def test_the_launch_path_calls_it(self, source):
        assert "clearRestorableSession(profileDir, logger)" in source

    def test_it_is_given_the_resolved_profile_directory(self, source):
        """`userDataDir/<session>` is the relative form puppeteer resolves for
        itself; the sweep touches the filesystem directly and needs the real
        path, which is customUserDataDir + session when one is configured."""
        call = re.search(
            r"path\.resolve\(\s*req\.serverOptions\.customUserDataDir\s*\n?\s*\?"
            r" req\.serverOptions\.customUserDataDir \+ session",
            source,
        )
        assert call, "the recovery is no longer handed the resolved profile dir"
