"""Tests for detecting that node_modules holds a different wppconnect or
wa-js than api/package.json pins — core/wpp_runtime.library_drifts() and the
per-package wppconnect_library_drift() / wa_js_library_drift() under it.

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

wa-js is checked for the same reason as wppconnect, and it needs its own
comparison: wppconnect 2.3.3 -> 2.3.4 changed no compiled file and moved only
its wa-js range (^4.6.0 -> ^4.6.1), so after a wa-js-only bump the wppconnect
numbers agree and nothing else would notice that the bundle running in the
page is not the one WinZapp was validated against.

Pure and stdlib-only (setup_api.py imports this module before any dependency
is installed), so these run against real files in tmp_path rather than mocks.
"""

import inspect
import json

import main
from core.wpp_runtime import (
    WA_JS_PACKAGE,
    WPPCONNECT_PACKAGE,
    describe_library_drifts,
    installed_wppconnect_version,
    library_drifts,
    required_wppconnect_version,
    wa_js_library_drift,
    wppconnect_library_drift,
)
from tests.god_modules import patch_main_global


def _api_dir(tmp_path, installed=None, pinned=None, pkg_extra=None,
             wa_installed=None, wa_pinned=None):
    """A minimal api/ tree. `installed` None means node_modules is absent,
    `pinned` None means api/package.json declares no such dependency; the
    wa_* pair is the same for @wppconnect/wa-js."""
    api = tmp_path / "api"
    api.mkdir(exist_ok=True)

    pkg = {"name": "@wppconnect/server", "version": "2.10.18"}
    deps = {}
    if pinned is not None:
        deps[WPPCONNECT_PACKAGE] = pinned
    if wa_pinned is not None:
        deps[WA_JS_PACKAGE] = wa_pinned
    if deps:
        pkg["dependencies"] = deps
    if pkg_extra is not None:
        pkg.update(pkg_extra)
    (api / "package.json").write_text(json.dumps(pkg), encoding="utf-8")

    for package, version in ((WPPCONNECT_PACKAGE, installed),
                             (WA_JS_PACKAGE, wa_installed)):
        if version is None:
            continue
        lib = api / "node_modules"
        for part in package.split("/"):
            lib = lib / part
        lib.mkdir(parents=True, exist_ok=True)
        (lib / "package.json").write_text(
            json.dumps({"name": package, "version": version}),
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


class TestWaJsDrift:
    """The bundle injected into WhatsApp Web, compared exactly like wppconnect:
    exact pin only, anything unreadable is "no drift claim"."""

    def test_a_wa_js_only_drift_is_detected(self, tmp_path):
        """The 2.3.3 -> 2.3.4 shape: wppconnect agrees, wa-js does not.
        wppconnect_library_drift() alone would have answered None."""
        api = _api_dir(tmp_path, installed="2.3.4", pinned="2.3.4",
                       wa_installed="4.6.0", wa_pinned="4.6.1")
        assert wppconnect_library_drift(api) is None
        assert wa_js_library_drift(api) == ("4.6.0", "4.6.1")
        assert library_drifts(api) == [(WA_JS_PACKAGE, "4.6.0", "4.6.1")]

    def test_a_wppconnect_only_drift_is_detected(self, tmp_path):
        api = _api_dir(tmp_path, installed="2.3.3", pinned="2.3.4",
                       wa_installed="4.6.1", wa_pinned="4.6.1")
        assert wa_js_library_drift(api) is None
        assert library_drifts(api) == [(WPPCONNECT_PACKAGE, "2.3.3", "2.3.4")]

    def test_both_drifting_are_both_reported_in_a_stable_order(self, tmp_path):
        api = _api_dir(tmp_path, installed="2.3.3", pinned="2.3.4",
                       wa_installed="4.6.0", wa_pinned="4.6.1")
        assert library_drifts(api) == [
            (WPPCONNECT_PACKAGE, "2.3.3", "2.3.4"),
            (WA_JS_PACKAGE, "4.6.0", "4.6.1"),
        ]

    def test_agreeing_libraries_are_not_drift(self, tmp_path):
        api = _api_dir(tmp_path, installed="2.3.4", pinned="2.3.4",
                       wa_installed="4.6.1", wa_pinned="4.6.1")
        assert library_drifts(api) == []

    def test_a_newer_wa_js_is_drift_too(self, tmp_path):
        api = _api_dir(tmp_path, installed="2.3.4", pinned="2.3.4",
                       wa_installed="4.7.0", wa_pinned="4.6.1")
        assert wa_js_library_drift(api) == ("4.7.0", "4.6.1")

    def test_whitespace_does_not_invent_wa_js_drift(self, tmp_path):
        api = _api_dir(tmp_path, wa_installed=" 4.6.1 ", wa_pinned=" 4.6.1 ")
        assert wa_js_library_drift(api) is None

    def test_a_wa_js_range_makes_no_drift_claim(self, tmp_path):
        for pin in ("^4.6.1", "~4.6.1", "*", "latest",
                    "github:wppconnect-team/wa-js#main"):
            api = _api_dir(tmp_path, wa_installed="4.6.0", wa_pinned=pin)
            assert wa_js_library_drift(api) is None, pin
            assert library_drifts(api) == [], pin

    def test_an_unreadable_wa_js_is_never_reported_as_drift(self, tmp_path):
        # no node_modules copy
        assert library_drifts(_api_dir(tmp_path, wa_pinned="4.6.1")) == []
        # no pin
        assert library_drifts(_api_dir(tmp_path, wa_installed="4.6.0")) == []
        # broken installed package.json
        api = _api_dir(tmp_path, wa_installed="4.6.0", wa_pinned="4.6.1")
        broken = (tmp_path / "api" / "node_modules" / "@wppconnect" / "wa-js"
                  / "package.json")
        broken.write_text("{ not json", encoding="utf-8")
        assert library_drifts(api) == []
        # broken api/package.json
        (tmp_path / "api" / "package.json").write_text("[]", encoding="utf-8")
        assert library_drifts(api) == []
        # nothing at all
        assert library_drifts(str(tmp_path / "nope")) == []

    def test_one_unreadable_library_does_not_hide_the_other(self, tmp_path):
        api = _api_dir(tmp_path, installed=None, pinned="2.3.4",
                       wa_installed="4.6.0", wa_pinned="4.6.1")
        assert library_drifts(api) == [(WA_JS_PACKAGE, "4.6.0", "4.6.1")]

    def test_an_unscoped_lookalike_is_not_mistaken_for_wa_js(self, tmp_path):
        """@wppconnect/wa-version, the unpinned catalogue, lives next to
        wa-js in node_modules and must never be read as it."""
        api = _api_dir(tmp_path, wa_installed="4.6.1", wa_pinned="4.6.1")
        other = (tmp_path / "api" / "node_modules" / "@wppconnect"
                 / "wa-version")
        other.mkdir(parents=True)
        (other / "package.json").write_text(
            json.dumps({"version": "1.5.4943"}), encoding="utf-8")
        assert library_drifts(api) == []


class TestTheDriftIsNamedInThePrompt:
    """The reinstall prompt has two slots, current and new. Each names its
    library, so "4.6.0 -> 4.6.1" cannot be read as a server version."""

    def test_one_drift_names_its_library(self):
        drifts = [(WA_JS_PACKAGE, "4.6.0", "4.6.1")]
        assert describe_library_drifts(drifts) == (
            "@wppconnect/wa-js 4.6.0", "@wppconnect/wa-js 4.6.1")

    def test_several_drifts_are_joined_in_order(self):
        drifts = [(WPPCONNECT_PACKAGE, "2.3.3", "2.3.4"),
                  (WA_JS_PACKAGE, "4.6.0", "4.6.1")]
        installed, required = describe_library_drifts(drifts)
        assert installed == (
            "@wppconnect-team/wppconnect 2.3.3, @wppconnect/wa-js 4.6.0")
        assert required == (
            "@wppconnect-team/wppconnect 2.3.4, @wppconnect/wa-js 4.6.1")


# ── The gate that consumes it ────────────────────────────────────────────────


class _Gate:
    """MainWindow's two version questions, unbound against a stub.

    MainWindow is a wx.Frame and cannot be built without a running wx.App;
    these two methods touch nothing but the helpers below them.
    """

    _server_version_below_minimum = main.MainWindow._server_version_below_minimum
    _library_drifts               = main.MainWindow._library_drifts
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
        patch_main_global(monkeypatch, "library_drifts", _boom)
        patch_main_global(monkeypatch, "resource_path", lambda *p: "/nope")

        assert _Gate()._library_drifts() == []

    def test_drifts_are_passed_through(self, monkeypatch):
        drifts = [(WPPCONNECT_PACKAGE, "2.3.1", "2.3.3"),
                  (WA_JS_PACKAGE, "4.6.0", "4.6.1")]
        patch_main_global(monkeypatch, "library_drifts", lambda _api_dir: drifts)
        patch_main_global(monkeypatch, "resource_path", lambda *p: "/api")

        assert _Gate()._library_drifts() == drifts


class TestTheReinstallTagIsNeverBuiltFromALibraryVersion:
    """The library branch's version is a wppconnect/wa-js number ("2.3.4",
    "4.6.1"), and no wppconnect-SERVER release is tagged v2.3.4 — feeding it to
    ApiSetupDialog's forced_tag would build a 404 archive URL, the same
    failure the bare-version bug had. Only the server branch may fall back to
    its own number; the library branch passes no tag and lets the dialog
    resolve the latest release itself."""

    def test_the_fallback_is_gated_on_the_server_branch(self):
        src = inspect.getsource(main.MainWindow.ensure_wpp_version)
        assert "if not minimum_tag and outdated:" in src
        # and the tag is handed over as-is, never re-derived at the call site
        assert "forced_tag=minimum_tag," in src
