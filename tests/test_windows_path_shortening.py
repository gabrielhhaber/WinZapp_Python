"""shorten_windows_path() — the Chrome profile root must stay under MAX_PATH.

Why this exists: Chrome's deepest per-profile paths are its Service Worker
CacheStorage entries, roughly 127 characters below the profile root. Add a
32-character session name and an install that lives a few directories down,
and the total crosses Windows' 260-character MAX_PATH. Chrome then cannot
create those entries, WhatsApp Web never gets its persistent storage bucket,
and it logs ITSELF out — the page navigates to `?post_logout=1&logout_reason=0`,
taking wa-js with it, and loops until WPPConnect force-kills the session. No
log anywhere says "path too long"; the navigation is the only symptom.

Two installs that differed in nothing else produced 262 vs 219 characters,
and only the 219 one could ever show a QR.
"""

import os

import pytest

from main import shorten_windows_path


# The real shape, measured on the install where this was found: two hashed
# 32-character directories plus Chrome's own index naming.
_CHROME_SW_SUFFIX = (
    os.sep.join(["", "Default", "Service Worker", "CacheStorage",
                 "b" * 32, "c" * 32, "index-dir", "the-real-index"])
)
MAX_PATH = 260


class TestItNeverMakesThingsWorse:
    """Best-effort by design: 8.3 generation can be disabled per volume, and
    the caller must be no worse off than before when it is."""

    def test_it_never_returns_a_longer_path(self, tmp_path):
        result = shorten_windows_path(str(tmp_path))
        assert len(result) <= len(str(tmp_path))

    def test_it_never_returns_empty_for_a_real_directory(self, tmp_path):
        assert shorten_windows_path(str(tmp_path))

    def test_an_empty_path_is_returned_unchanged(self):
        assert shorten_windows_path("") == ""

    def test_a_missing_directory_is_returned_unchanged(self, tmp_path):
        """The Win32 call resolves a real directory entry, so a path that is
        not there yet cannot be shortened — the caller creates the directory
        first precisely because of this."""
        missing = str(tmp_path / "not-created-yet")
        assert shorten_windows_path(missing) == missing

    def test_it_still_points_at_the_same_directory(self, tmp_path):
        """8.3 is an alias, not a copy. Everything else in main.py checks
        these directories through the long form, so the two must resolve to
        one directory or those checks would silently stop finding anything."""
        marker = tmp_path / "marker.txt"
        marker.write_text("x", encoding="utf-8")
        short = shorten_windows_path(str(tmp_path))
        assert os.path.isfile(os.path.join(short, "marker.txt"))
        assert os.path.samefile(short, str(tmp_path))


@pytest.mark.skipif(os.name != "nt", reason="8.3 short names are Windows-only")
class TestItActuallyShortensAPathWithSpaces:
    """The install this was found on lives under a directory name containing
    a space, which is exactly when Windows keeps an 8.3 alias around."""

    def test_a_spaced_directory_name_gets_shortened(self, tmp_path):
        spaced = tmp_path / "code troopers with spaces"
        spaced.mkdir()
        short = shorten_windows_path(str(spaced))
        if short == str(spaced):
            pytest.skip("8.3 generation appears disabled on this volume")
        assert len(short) < len(str(spaced))
        assert os.path.samefile(short, str(spaced))


class TestTheBudgetThisProtects:
    """Documents the arithmetic, so a future change that lengthens the
    profile root fails here rather than in the field as a pairing that never
    produces a QR."""

    def test_chromes_deepest_known_suffix_is_what_blows_the_budget(self):
        # Anything at or under this leaves no room for a 32-char session name.
        assert len(_CHROME_SW_SUFFIX) > 120

    def test_a_deep_profile_root_crosses_max_path(self):
        root = (r"c:\inatel\code troopers\gabrielhhaber-winzapp_python"
                r"\winzapp_python\client\data\global\api\userDataDir")
        full = os.path.join(root, "a" * 32) + _CHROME_SW_SUFFIX
        assert len(full) > MAX_PATH

    def test_the_shortened_equivalent_fits(self):
        root = r"c:\inatel\CODETR~1\GABRIE~1\WINZAP~1\client\api\userDataDir"
        full = os.path.join(root, "a" * 32) + _CHROME_SW_SUFFIX
        assert len(full) < MAX_PATH


class TestTheLiteralUserDataDirSurvivesShortening:
    """The regression guard for the one thing that must NOT be shortened.

    `GetShortPathNameW` rewrites every component longer than 8 characters,
    and `userDataDir` is 11 — so shortening the WHOLE path turns it into
    `USERDA~1`. Two separate places identify a session's Chrome by matching
    that literal in a command line, and both fail by matching NOTHING: a
    stale Chrome holding the profile lock is then never killed and the
    session hangs in INITIALIZING. Shortening only the ancestor costs 3 of
    the 43 characters saved.

    This ties the two modules together deliberately: the env var is built in
    main.py, and the matcher lives in connection_state.py, so neither file's
    own tests would notice the other changing.
    """

    SESSION = "a" * 32

    def _env_value(self, ancestor):
        """Mirrors what _start_wpp_background() puts in WINZAPP_USER_DATA_DIR."""
        return os.path.join(shorten_windows_path(str(ancestor)), "userDataDir") + os.sep

    def test_the_matcher_still_recognises_the_shortened_path(self, tmp_path):
        from connection_state import chrome_cmdline_owns_session
        ancestor = tmp_path / "code troopers with spaces" / "api"
        ancestor.mkdir(parents=True)
        cmdline = (r"chrome.exe --user-data-dir="
                   + self._env_value(ancestor) + self.SESSION)
        assert chrome_cmdline_owns_session(cmdline, self.SESSION) is True

    def test_shortening_the_whole_path_would_break_that_matcher(self, tmp_path):
        """States the bug that was nearly shipped, so the guard above cannot
        be 'simplified' back into it without a red test."""
        from connection_state import chrome_cmdline_owns_session
        udd = tmp_path / "code troopers with spaces" / "api" / "userDataDir"
        udd.mkdir(parents=True)
        whole = shorten_windows_path(str(udd))
        if "userdatadir" in whole.lower():
            pytest.skip("8.3 generation appears disabled on this volume")
        cmdline = rf"chrome.exe --user-data-dir={whole}\{self.SESSION}"
        assert chrome_cmdline_owns_session(cmdline, self.SESSION) is False
