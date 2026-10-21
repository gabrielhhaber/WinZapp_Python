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

(Since 2026-10-18 Force Reinstall installs the newest release this build
supports, the pin or one approved for it (client/wpp_approved_versions.json),
never the newest upstream one: WinZapp's patches follow the server by hand.
Promise 1 above is kept for its history; TestForceReinstallGoesToThePinnedRelease
is the rule now.)
"""

import inspect
import re

import main
import updater
from main import MainWindow


class TestForceReinstallGoesToThePinnedRelease:
    """Originally Force Reinstall was meant to fetch the newest release. WinZapp
    now installs only a release it supports, so the "same version again" repair
    is the point: it rebuilds a broken install on the pin, or on a release the
    maintainers approved for that pin."""

    def test_it_asks_for_the_supported_tag(self, monkeypatch):
        confirmed = []
        monkeypatch.setattr(updater.wx, "CallAfter", lambda fn, *a: confirmed.append(a))
        checker = updater.WppUpdateChecker.__new__(updater.WppUpdateChecker)
        checker._stopped = False
        checker._reinstall_tag = lambda: "v2.10.19"
        checker._schedule_retry = lambda interval=None: None
        checker._force_reinstall_worker()
        assert confirmed == [("2.10.19", "v2.10.19")]

    def test_the_supported_tag_is_the_pin_when_nothing_is_approved(self, monkeypatch):
        monkeypatch.setattr(updater, "homologated_wpp_tag", lambda _p: "v2.10.18")
        monkeypatch.setattr(updater.WppUpdateChecker, "_approved_tag",
                            staticmethod(lambda pin: ("", False)))
        checker = updater.WppUpdateChecker.__new__(updater.WppUpdateChecker)
        assert checker._reinstall_tag() == "v2.10.18"

    def test_the_supported_tag_is_an_approved_release_when_there_is_one(self, monkeypatch):
        monkeypatch.setattr(updater, "homologated_wpp_tag", lambda _p: "v2.10.18")
        monkeypatch.setattr(updater.WppUpdateChecker, "_approved_tag",
                            staticmethod(lambda pin: ("v2.10.19", True)))
        checker = updater.WppUpdateChecker.__new__(updater.WppUpdateChecker)
        assert checker._reinstall_tag() == "v2.10.19"

    def test_the_pin_is_read_from_the_bundled_minimum(self, monkeypatch):
        seen = []
        monkeypatch.setattr(updater, "homologated_wpp_tag",
                            lambda path: seen.append(path) or "v2.10.18")
        assert updater.WppUpdateChecker._pinned_tag() == "v2.10.18"
        assert seen and seen[0].endswith("wpp_minimum_version.txt")

    def test_nothing_in_the_updater_asks_github_for_a_wpp_release(self):
        source = inspect.getsource(updater)
        assert "fetch_latest_wpp_tag" not in source
        assert "_newest_available_tag" not in source


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
        source = inspect.getsource(api_setup.ApiSetupDialog._run_setup_locked)
        assert '"dist", "server.js"' in source
