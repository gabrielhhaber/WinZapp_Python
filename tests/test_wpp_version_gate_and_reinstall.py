"""Two user-visible promises about the WPPConnect version, both broken.

Reported together by a user trying to move an install onto a newer
wppconnect-server:

1. **Force reinstall reinstalled the same version.** Help > "Forçar
   reinstalação da WPPConnect" documents, in two places, "always fetches
   whatever is currently the latest release, regardless of version". It called
   `_fetch_latest_tag()`, which returned the *homologated* tag whenever one was
   bundled — which is always, in a release build. Three forced reinstalls, each
   reported successful, `package.json` unchanged at 2.10.16.

2. **The outdated-version warning never appeared.** Raising
   `client/wpp_minimum_version.txt` above the installed version produced no
   prompt at all: WinZapp came up on the old server without a word.
   `ensure_wpp_version()` guarded on `api/dist/main.js`, and WPPConnect Server
   has never built a file by that name — `npm run build` produces
   `dist/server.js`. The guard was always false, so the method always returned
   on its first statement and the entire prompt has never run for anyone.

Fixing (2) exposed a third defect underneath it, which is the reason a dead
code path is worth being suspicious of rather than merely fixing: the prompt's
own "Update now" button passed the bare version ("2.10.18") as
ApiSetupDialog's forced_tag, which goes straight into
`.../archive/refs/tags/{tag}.zip`. The release is tagged "v2.10.18", so that
URL is a 404. The button could never have worked.
"""

import inspect
import re

import pytest

import main
import updater
from main import MainWindow


class TestForceReinstallGoesToTheNewestRelease:
    def test_it_asks_for_the_newest_available_tag(self):
        source = inspect.getsource(updater.WppUpdateChecker._force_reinstall_worker)
        assert "_newest_available_tag()" in source, (
            "force reinstall must resolve the newest release, not the "
            "homologated one — reinstalling the same version is the bug"
        )

    def test_the_newest_lookup_actually_reaches_github(self):
        source = inspect.getsource(updater.WppUpdateChecker._newest_available_tag)
        assert "fetch_latest_wpp_tag()" in source

    def test_it_never_downgrades_below_the_homologated_release(self, monkeypatch):
        """It must be able to move a user forward, never backward — a GitHub
        hiccup answering with something older must not silently downgrade an
        install below what WinZapp was built against."""
        monkeypatch.setattr(updater, "homologated_wpp_tag", lambda _p: "v2.10.18")
        monkeypatch.setattr(
            "ui.dialogs.api_setup.fetch_latest_wpp_tag", lambda: "v2.10.16")
        assert updater.WppUpdateChecker._newest_available_tag() == "v2.10.18"

    def test_a_newer_release_wins(self, monkeypatch):
        monkeypatch.setattr(updater, "homologated_wpp_tag", lambda _p: "v2.10.16")
        monkeypatch.setattr(
            "ui.dialogs.api_setup.fetch_latest_wpp_tag", lambda: "v2.10.18")
        assert updater.WppUpdateChecker._newest_available_tag() == "v2.10.18"

    def test_an_unreachable_github_falls_back_to_the_homologated_tag(self, monkeypatch):
        monkeypatch.setattr(updater, "homologated_wpp_tag", lambda _p: "v2.10.16")
        monkeypatch.setattr("ui.dialogs.api_setup.fetch_latest_wpp_tag", lambda: "")
        assert updater.WppUpdateChecker._newest_available_tag() == "v2.10.16"

    def test_the_periodic_check_still_compares_against_the_homologated_release(self):
        """Deliberately unchanged. Prompting every user onto every
        wppconnect-server release the day it appears is how a patch set that no
        longer matches reaches people — which is what 2.3.2 did. Raising
        wpp_minimum_version.txt stays the deliberate act."""
        source = inspect.getsource(updater.WppUpdateChecker._check_once)
        assert "_homologated_or_latest_tag()" in source
        assert "_newest_available_tag()" not in source


class TestVersionComparisonForTheFloor:
    @pytest.mark.parametrize("candidate, reference, expected", [
        ("v2.10.16", "v2.10.18", True),
        ("2.10.16", "v2.10.18", True),
        ("v2.10.18", "v2.10.18", False),
        ("v2.10.19", "v2.10.18", False),
        ("v2.11.0", "v2.10.18", False),
    ])
    def test_ordering(self, candidate, reference, expected):
        assert updater._version_is_older(candidate, reference) is expected

    @pytest.mark.parametrize("candidate, reference", [
        ("not-a-version", "v2.10.18"),
        ("v2.10.18", ""),
        ("", "v2.10.18"),
    ])
    def test_unparseable_input_never_means_downgrade(self, candidate, reference):
        """"I cannot tell" must not become "go backwards"."""
        assert updater._version_is_older(candidate, reference) is False


class TestTheStartupGateCanActuallyRun:
    @staticmethod
    def _source():
        return inspect.getsource(MainWindow.ensure_wpp_version)

    def test_it_no_longer_guards_on_a_file_that_is_never_built(self):
        """`npm run build` produces dist/server.js. dist/main.js has never
        existed, so this guard was always false and the whole method returned
        on its first statement."""
        source = self._source()
        assert '"main.js"' not in source

    def test_it_guards_on_the_built_entry_point(self):
        source = self._source()
        assert re.search(r'resource_path\(\s*"api",\s*"dist",\s*"server\.js"\s*\)',
                         source)

    def test_the_update_button_passes_a_real_git_tag(self):
        """forced_tag goes straight into .../archive/refs/tags/{tag}.zip. The
        bare version built a 404, which nobody could discover while the guard
        above kept this code unreachable."""
        source = self._source()
        assert "homologated_wpp_tag(" in source
        assert not re.search(r"forced_tag\s*=\s*minimum\s*,", source)


class TestTheGuardMatchesWhatTheInstallerBuilds:
    def test_the_setup_dialog_uses_the_same_file_as_its_built_marker(self):
        """Both answer "is the API installed and built". Two different files
        for one question is how the startup gate ended up watching a name that
        is never produced."""
        from ui.dialogs import api_setup
        source = inspect.getsource(api_setup.ApiSetupDialog._run_setup)
        assert '"dist", "server.js"' in source
