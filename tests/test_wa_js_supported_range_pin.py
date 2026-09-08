"""The pinned WhatsApp Web build has to be one the installed wa-js can drive.

Expiry (tests/test_wa_version_expiry_pin.py) answers "will Meta still serve
this build". It says nothing about the other half of the contract: WPPConnect
serves the pinned build and then injects @wppconnect/wa-js into it, and if that
build is one wa-js cannot drive, the page loads and *authenticates* — the phone
lists the linked device as active — but `WPP.isReady` never becomes true.
wppconnect's injectApi() then dies on

    TimeoutError: Waiting failed: 30000ms exceeded

the session never leaves INITIALIZING, WinZapp reports offline, and because
nothing was ever logged out the user is told nothing at all.

Measured on a real install on 2026-09-07: an npm install at 15:10 local pulled
a wa-version catalogue published that morning, selectServableVersion() pinned
2.3000.1046941822-alpha (released 15:44 UTC, two and a half hours before the
run), and every launch died exactly there.

wa-js states the contract itself, in its own bundle:

    t.version="4.6.0", t.supportedWhatsappWeb=">=2.3000.1038792969-alpha"

Today that is a floor only, so honouring it changes nothing on a current
catalogue — which is precisely why it needs a test: the behaviour it protects
is invisible until wa-js publishes an upper bound, and a regression here would
be silent until the next outage.

Same harness approach as the expiry test, and for the same reason: requiring
the whole of start.js runs its top-level Chrome scan.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
START_JS = ROOT / "client" / "api_patches" / "start.js"

DAY_MS = 24 * 60 * 60 * 1000
NOW_MS = 1_800_000_000_000

HARNESS = r"""
'use strict';
const scenario = JSON.parse(process.argv[2]);

const catalogue = {
  getAvailableVersions: () => scenario.versions.map((v) => v.version),
  getPageContent: (version) => {
    const entry = scenario.versions.find((v) => v.version === version);
    if (!entry || entry.htmlMissing) throw new Error('Version not available for ' + version);
    return '<html></html>';
  },
  getVersionInfo: (version) => {
    const entry = scenario.versions.find((v) => v.version === version);
    if (!entry) throw new Error('Version not available for ' + version);
    return { version, beta: false, released: entry.released, expire: entry.expire };
  },
};

__SELECTOR_SOURCE__

const selected = selectServableVersion(catalogue, scenario.now, scenario.range);
console.log('__RESULT__' + JSON.stringify({
  selected,
  compare: (scenario.compare || []).map(([a, b]) => compareWhatsappVersions(a, b)),
  satisfies: (scenario.satisfies || []).map(([v, r]) => satisfiesWhatsappRange(v, r)),
}));
"""


def _selector_source():
    """The real shipped source, sliced between two load-bearing definitions —
    the same anchors the expiry test uses, so the two cannot drift apart."""
    source = START_JS.read_text(encoding="utf-8")
    start = source.index("function versionExpiry(")
    end = source.index("function resolveWhatsappVersion(", start)
    return source[start:end]


def _run(tmp_path, versions=(), now=NOW_MS, range_=None, compare=(), satisfies=()):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    harness = tmp_path / "harness.js"
    harness.write_text(
        HARNESS.replace("__SELECTOR_SOURCE__", _selector_source()), encoding="utf-8"
    )
    scenario = {
        "versions": list(versions),
        "now": now,
        "range": range_,
        "compare": [list(pair) for pair in compare],
        "satisfies": [list(pair) for pair in satisfies],
    }
    proc = subprocess.run(
        [node, str(harness), json.dumps(scenario)],
        capture_output=True, text=True, timeout=60,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("__RESULT__"):
            return json.loads(line[len("__RESULT__"):])
    raise AssertionError(
        f"harness produced no result.\nexit={proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def _iso(ms):
    import datetime
    return datetime.datetime.fromtimestamp(
        ms / 1000, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _entry(version, expire_offset_days=30, html_missing=False):
    return {
        "version": version,
        "released": "2026-07-01T00:00:00.000Z",
        "expire": _iso(NOW_MS + expire_offset_days * DAY_MS),
        "htmlMissing": html_missing,
    }


class TestWhatsappBuildsAreOrderedAsNumbers:
    """They look like semver and are not: the third component runs into the
    billions, where string ordering is simply wrong."""

    def test_a_longer_number_is_not_automatically_larger_as_a_string(self, tmp_path):
        result = _run(tmp_path, compare=[
            ("2.3000.1046941822", "2.3000.999999999"),
        ])
        assert result["compare"] == [1]

    def test_equal_builds_compare_equal_across_channel_suffixes(self, tmp_path):
        """The suffix names a release channel, not an ordering — the
        stable-first preference in selectServableVersion() handles that."""
        result = _run(tmp_path, compare=[
            ("2.3000.1046941822-alpha", "2.3000.1046941822"),
        ])
        assert result["compare"] == [0]

    def test_ordering_is_component_by_component(self, tmp_path):
        result = _run(tmp_path, compare=[
            ("2.3000.1000000001", "2.3000.1000000002"),
            ("2.3001.1", "2.3000.9999999999"),
        ])
        assert result["compare"] == [-1, 1]


class TestTheDeclaredRangeIsRead:
    def test_a_floor_excludes_everything_below_it(self, tmp_path):
        result = _run(tmp_path, satisfies=[
            ("2.3000.1038792968-alpha", ">=2.3000.1038792969-alpha"),
            ("2.3000.1038792969-alpha", ">=2.3000.1038792969-alpha"),
            ("2.3000.1046941822-alpha", ">=2.3000.1038792969-alpha"),
        ])
        assert result["satisfies"] == [False, True, True]

    def test_a_conjunction_with_an_upper_bound_is_honoured(self, tmp_path):
        """Nothing wa-js publishes today has one. The day it does, this is the
        whole fix, with nothing to edit here."""
        result = _run(tmp_path, satisfies=[
            ("2.3000.1040000000", ">=2.3000.1038792969 <2.3000.1046000000"),
            ("2.3000.1046941822", ">=2.3000.1038792969 <2.3000.1046000000"),
        ])
        assert result["satisfies"] == [True, False]


class TestAnUnreadableRangeNeverExcludesAnything:
    """The safe direction is emphatic. A range that matched nothing would empty
    the catalogue and drop resolveWhatsappVersion() into its unpinned branch,
    where WhatsApp serves its own newest build AND the document interception is
    lost — the documented silent-send-failure disaster."""

    @pytest.mark.parametrize("range_", [None, "", "*", "^4.6.0 || banana", "latest"])
    def test_unusable_ranges_admit_every_build(self, tmp_path, range_):
        result = _run(tmp_path, satisfies=[("2.3000.1046941822-alpha", range_)])
        assert result["satisfies"] == [True]

    def test_an_install_that_cannot_read_wa_js_pins_exactly_as_before(self, tmp_path):
        """null is what readWaJsSupportedRange() answers when the bundle cannot
        be resolved — it must behave as "no opinion", never as "nothing is
        supported"."""
        result = _run(tmp_path, versions=[
            _entry("2.3000.1000000001"),
            _entry("2.3000.1000000002"),
        ], range_=None)
        assert result["selected"]["version"] == "2.3000.1000000002"
        assert result["selected"]["unsupported"] is False


class TestSelectionPrefersABuildWaJsCanDrive:
    def test_a_newer_build_outside_the_range_loses_to_an_older_one_inside_it(
        self, tmp_path
    ):
        """The point of the whole filter: newer is worth nothing if wa-js
        cannot initialise on it. This is the 2026-09-07 outage in one
        assertion."""
        result = _run(tmp_path, versions=[
            _entry("2.3000.1040000000"),
            _entry("2.3000.1046941822"),
        ], range_=">=2.3000.1038792969 <2.3000.1046000000")
        assert result["selected"]["version"] == "2.3000.1040000000"
        assert result["selected"]["unsupported"] is False

    def test_the_newest_supported_build_still_wins_among_supported_ones(self, tmp_path):
        result = _run(tmp_path, versions=[
            _entry("2.3000.1039000000"),
            _entry("2.3000.1040000000"),
            _entry("2.3000.1046941822"),
        ], range_=">=2.3000.1038792969 <2.3000.1046000000")
        assert result["selected"]["version"] == "2.3000.1040000000"

    def test_expiry_still_applies_inside_the_supported_set(self, tmp_path):
        result = _run(tmp_path, versions=[
            _entry("2.3000.1040000000", expire_offset_days=30),
            _entry("2.3000.1041000000", expire_offset_days=-1),
        ], range_=">=2.3000.1038792969 <2.3000.1046000000")
        assert result["selected"]["version"] == "2.3000.1040000000"

    def test_a_build_whose_html_is_missing_is_skipped_inside_the_range_too(
        self, tmp_path
    ):
        result = _run(tmp_path, versions=[
            _entry("2.3000.1040000000"),
            _entry("2.3000.1041000000", html_missing=True),
        ], range_=">=2.3000.1038792969 <2.3000.1046000000")
        assert result["selected"]["version"] == "2.3000.1040000000"


class TestAnEmptySupportedSetDegradesRatherThanFailing:
    """Returning null here would mean no pin at all, which is worse than a pin
    of uncertain compatibility — so an impossible range falls back to the old
    behaviour and says so through `unsupported`."""

    def test_nothing_in_range_still_pins_the_newest_servable_build(self, tmp_path):
        result = _run(tmp_path, versions=[
            _entry("2.3000.1000000001"),
            _entry("2.3000.1000000002"),
        ], range_=">=2.3000.9999999999")
        assert result["selected"] is not None
        assert result["selected"]["version"] == "2.3000.1000000002"

    def test_the_fallback_is_flagged_so_the_log_can_name_it(self, tmp_path):
        result = _run(tmp_path, versions=[_entry("2.3000.1000000001")],
                      range_=">=2.3000.9999999999")
        assert result["selected"]["unsupported"] is True

    def test_an_entirely_empty_catalogue_is_still_null(self, tmp_path):
        result = _run(tmp_path, versions=[], range_=">=2.3000.1")
        assert result["selected"] is None
