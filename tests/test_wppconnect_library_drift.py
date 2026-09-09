"""Tests for detecting that node_modules holds a different wppconnect than
api/package.json pins — core/wpp_runtime.wppconnect_library_drift().

The mismatch nothing checked, and the one that actually disconnects people.
`build.py`'s API_EXCLUDE_DIRS keeps node_modules OUT of the release ZIP, so an
update ships a new dist/server.js and a new package.json over whatever
wppconnect the machine already had. The startup gate could never see it: it
compares api/package.json's *server* version against wpp_minimum_version.txt,
and both of those files come out of the same ZIP, so after an update they
agree by construction.

What breaks is the patching. core/wppconnect_*_patch.py rewrites the library's
compiled output by searching for exact source text, so against an unexpected
version a patch does not fail — it is silently skipped. Measured on a real
install (issue #164): host.layer.js's checkQrCode and loginByCode both logged
"DID NOT MATCH ... left untouched", the pairing-code path ran unpatched, and
the session cycled CLOSED/INITIALIZING for minutes without ever reaching
CONNECTED. A second install, checked directly, held wppconnect 2.3.1 under an
api/package.json pinning 2.3.3.

Pure and stdlib-only (setup_api.py imports this module before any dependency
is installed), so these run against real files in tmp_path rather than mocks.
"""

import inspect
import json

import main
from core.wpp_runtime import (
    WPPCONNECT_PACKAGE,
    installed_wppconnect_version,
    required_wppconnect_version,
    wppconnect_library_drift,
)


def _api_dir(tmp_path, installed=None, pinned=None, pkg_extra=None):
    """A minimal api/ tree. `installed` None means node_modules is absent,
    `pinned` None means api/package.json declares no such dependency."""
    api = tmp_path / "api"
    api.mkdir(exist_ok=True)

    pkg = {"name": "@wppconnect/server", "version": "2.10.18"}
    if pinned is not None:
        pkg["dependencies"] = {WPPCONNECT_PACKAGE: pinned}
    if pkg_extra is not None:
        pkg.update(pkg_extra)
    (api / "package.json").write_text(json.dumps(pkg), encoding="utf-8")

    if installed is not None:
        lib = api / "node_modules"
        for part in WPPCONNECT_PACKAGE.split("/"):
            lib = lib / part
        lib.mkdir(parents=True, exist_ok=True)
        (lib / "package.json").write_text(
            json.dumps({"name": WPPCONNECT_PACKAGE, "version": installed}),
            encoding="utf-8",
        )
    return str(api)


class TestReadingTheTwoVersions:
    def test_the_installed_version_comes_from_node_modules(self, tmp_path):
        api = _api_dir(tmp_path, installed="2.3.1", pinned="2.3.3")
        assert installed_wppconnect_version(api) == "2.3.1"

    def test_it_is_not_api_package_jsons_own_version(self, tmp_path):
        """api/package.json's "version" is the WPPConnect *Server* number
        (2.10.18 here) — a different thing entirely, and the one the old gate
        was reading."""
        api = _api_dir(tmp_path, installed="2.3.1", pinned="2.3.3")
        assert installed_wppconnect_version(api) != "2.10.18"

    def test_the_required_version_comes_from_the_pin(self, tmp_path):
        api = _api_dir(tmp_path, installed="2.3.1", pinned="2.3.3")
        assert required_wppconnect_version(api) == "2.3.3"

    def test_a_missing_node_modules_reads_as_unknown(self, tmp_path):
        api = _api_dir(tmp_path, installed=None, pinned="2.3.3")
        assert installed_wppconnect_version(api) == ""

    def test_an_unparseable_file_reads_as_unknown(self, tmp_path):
        api = _api_dir(tmp_path, installed="2.3.1", pinned="2.3.3")
        broken = tmp_path / "api" / "package.json"
        broken.write_text("{ not json", encoding="utf-8")
        assert required_wppconnect_version(api) == ""


class TestOnlyAnExactPinIsActedOn:
    """Deciding whether an installed version satisfies a RANGE needs a semver
    resolver this module cannot have — it is stdlib-only because setup_api.py
    imports it before any dependency exists. Answering "I cannot tell" is the
    safe direction: the caller's response to drift is a multi-minute
    re-download and rebuild."""

    def test_a_caret_range_is_not_an_exact_pin(self, tmp_path):
        api = _api_dir(tmp_path, installed="2.3.1", pinned="^2.3.3")
        assert required_wppconnect_version(api) == ""
        assert wppconnect_library_drift(api) is None

    def test_a_tilde_range_is_not_an_exact_pin(self, tmp_path):
        api = _api_dir(tmp_path, installed="2.3.1", pinned="~2.3.3")
        assert wppconnect_library_drift(api) is None

    def test_a_wildcard_is_not_an_exact_pin(self, tmp_path):
        api = _api_dir(tmp_path, installed="2.3.1", pinned="*")
        assert wppconnect_library_drift(api) is None

    def test_a_git_url_is_not_an_exact_pin(self, tmp_path):
        api = _api_dir(tmp_path, installed="2.3.1",
                       pinned="github:wppconnect-team/wppconnect#main")
        assert wppconnect_library_drift(api) is None

    def test_a_prerelease_pin_is_still_exact(self, tmp_path):
        api = _api_dir(tmp_path, installed="2.4.0-rc1", pinned="2.4.0-rc2")
        assert required_wppconnect_version(api) == "2.4.0-rc2"
        assert wppconnect_library_drift(api) == ("2.4.0-rc1", "2.4.0-rc2")


class TestTheDrift:
    def test_the_real_reported_mismatch_is_detected(self, tmp_path):
        """The exact pair found on a live install."""
        api = _api_dir(tmp_path, installed="2.3.1", pinned="2.3.3")
        assert wppconnect_library_drift(api) == ("2.3.1", "2.3.3")

    def test_agreeing_versions_are_not_drift(self, tmp_path):
        api = _api_dir(tmp_path, installed="2.3.3", pinned="2.3.3")
        assert wppconnect_library_drift(api) is None

    def test_a_newer_library_is_drift_too(self, tmp_path):
        """Not a "below the minimum" test: the patches search for one exact
        version's source text, so a newer library misses them just as an
        older one does."""
        api = _api_dir(tmp_path, installed="2.4.0", pinned="2.3.3")
        assert wppconnect_library_drift(api) == ("2.4.0", "2.3.3")

    def test_whitespace_around_a_version_does_not_invent_drift(self, tmp_path):
        api = _api_dir(tmp_path, installed=" 2.3.3 ", pinned=" 2.3.3 ")
        assert wppconnect_library_drift(api) is None

    def test_nothing_readable_is_never_reported_as_drift(self, tmp_path):
        """Every unknown answers None: the response to drift is a full
        rebuild, far too expensive to trigger on a file that could not be
        parsed."""
        assert wppconnect_library_drift(str(tmp_path / "nope")) is None
        assert wppconnect_library_drift(
            _api_dir(tmp_path, installed=None, pinned="2.3.3")) is None
        assert wppconnect_library_drift(
            _api_dir(tmp_path, installed="2.3.1", pinned=None)) is None

    def test_a_non_dict_dependencies_block_is_survived(self, tmp_path):
        api = _api_dir(tmp_path, installed="2.3.1", pinned=None,
                       pkg_extra={"dependencies": "not-an-object"})
        assert wppconnect_library_drift(api) is None


# ── The gate that consumes it ────────────────────────────────────────────────


class _Gate:
    """MainWindow's two version questions, unbound against a stub.

    MainWindow is a wx.Frame and cannot be built without a running wx.App;
    these two methods touch nothing but the helpers below them.
    """

    _server_version_below_minimum = main.MainWindow._server_version_below_minimum
    _wppconnect_library_drift     = main.MainWindow._wppconnect_library_drift
    # staticmethod(), or binding it as a plain class attribute would make it
    # an instance method and pass `self` as the first version.
    _version_is_below = staticmethod(main.MainWindow._version_is_below)

    def __init__(self, installed="2.10.18", minimum="2.10.18", drift=None):
        self._installed = installed
        self._minimum = minimum
        self._drift = drift

    def _read_wpp_minimum_version(self):
        return self._minimum

    def _get_installed_wpp_version(self):
        return self._installed


class TestTheServerCheckCannotSeeAnUpdateIntroducedDrift:
    """Why the library check had to be added rather than the server one
    tightened: both of the server check's inputs — api/package.json's own
    "version" and wpp_minimum_version.txt — ship inside the release ZIP, so
    an update rewrites them together and they agree afterwards no matter what
    happened to node_modules."""

    def test_matching_versions_report_nothing(self):
        assert _Gate(installed="2.10.18", minimum="2.10.18")._server_version_below_minimum() is None

    def test_an_older_server_is_still_caught(self):
        """The case it CAN see: an install never updated at all."""
        gate = _Gate(installed="2.10.16", minimum="2.10.18")
        assert gate._server_version_below_minimum() == ("2.10.16", "2.10.18")

    def test_a_newer_server_is_not_a_problem(self):
        assert _Gate(installed="2.11.0", minimum="2.10.18")._server_version_below_minimum() is None

    def test_unreadable_inputs_report_nothing(self):
        assert _Gate(installed="", minimum="2.10.18")._server_version_below_minimum() is None
        assert _Gate(installed="2.10.16", minimum="")._server_version_below_minimum() is None


class TestTheLibraryCheckNeverBlocksStartup:
    def test_an_exception_is_swallowed_rather_than_raised(self, monkeypatch):
        """It runs on the startup path, before the Node server is brought up
        — an exception escaping here would leave the app open with no server
        behind it."""
        def _boom(_api_dir):
            raise RuntimeError("unreadable")
        monkeypatch.setattr(main, "wppconnect_library_drift", _boom)
        monkeypatch.setattr(main, "resource_path", lambda *p: "/nope")

        assert _Gate()._wppconnect_library_drift() is None

    def test_a_drift_is_passed_through(self, monkeypatch):
        monkeypatch.setattr(main, "wppconnect_library_drift",
                            lambda _api_dir: ("2.3.1", "2.3.3"))
        monkeypatch.setattr(main, "resource_path", lambda *p: "/api")

        assert _Gate()._wppconnect_library_drift() == ("2.3.1", "2.3.3")


class TestTheReinstallTagIsNeverBuiltFromALibraryVersion:
    """The library branch's version is a wppconnect number ("2.3.3"), and no
    wppconnect-SERVER release is tagged v2.3.3 — feeding it to
    ApiSetupDialog's forced_tag would build a 404 archive URL, the same
    failure the bare-version bug had. Only the server branch may fall back to
    its own number; the library branch passes no tag and lets the dialog
    resolve the latest release itself."""

    def test_the_fallback_is_gated_on_the_server_branch(self):
        src = inspect.getsource(main.MainWindow.ensure_wpp_version)
        assert "if not minimum_tag and outdated:" in src
        # and the tag is handed over as-is, never re-derived at the call site
        assert "forced_tag=minimum_tag," in src
