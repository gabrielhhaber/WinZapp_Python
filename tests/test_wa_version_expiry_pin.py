"""The pinned WhatsApp Web build has to be one Meta still serves.

start.js pins a build from @wppconnect/wa-version's catalogue, and it used to
take `available[available.length - 1]` — the newest entry — validating it with
getPageContent(). That only proves the HTML can be assembled from files on
disk. Meta expires a build on its own side (the catalogue records an ~2-month
window per entry in `released`/`expire`), and getPageContent() keeps succeeding
long after that, so a stale install pins a build WhatsApp no longer serves and
nothing in the log says so.

Seen live: a catalogue of 390 entries pinned 2.3000.1046208945 while WhatsApp
was serving 2.3000.1046553522; reinstalling the API (which updated the package)
pinned 2.3000.1046540740 and everything worked. Until then the only mechanism
protecting the user was "reinstall the API", which nobody knows to do.

The selection is pure logic in JS, so it is exercised as such: the two
functions are sliced out of the real api_patches/start.js source and run under
node against a fake catalogue. Requiring the whole of start.js is not an
option — its top-level code scans for Chrome and will happily start
downloading one.
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

const pageContentAsked = [];
const catalogue = {
  getAvailableVersions: () => scenario.versions.map((v) => v.version),
  getPageContent: (version) => {
    pageContentAsked.push(version);
    const entry = scenario.versions.find((v) => v.version === version);
    if (!entry || entry.htmlMissing) throw new Error('Version not available for ' + version);
    return '<html></html>';
  },
};
if (!scenario.withoutMetadata) {
  catalogue.getVersionInfo = (version) => {
    const entry = scenario.versions.find((v) => v.version === version);
    if (!entry) throw new Error('Version not available for ' + version);
    return { version, beta: false, released: entry.released, expire: entry.expire };
  };
}

__SELECTOR_SOURCE__

const selected = selectServableVersion(catalogue, scenario.now);
console.log('__RESULT__' + JSON.stringify({ selected, pageContentAsked }));
"""


def _selector_source():
    """The real shipped source of versionExpiry()/selectServableVersion().

    Sliced between two anchors that are load-bearing code rather than markers
    left for the test: the first function's definition, and the definition of
    the caller that follows it.
    """
    source = START_JS.read_text(encoding="utf-8")
    start = source.index("function versionExpiry(")
    end = source.index("function resolveWhatsappVersion(", start)
    return source[start:end]


def _select(tmp_path, versions, now=NOW_MS, without_metadata=False):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    harness = tmp_path / "harness.js"
    harness.write_text(
        HARNESS.replace("__SELECTOR_SOURCE__", _selector_source()), encoding="utf-8")
    scenario = {"versions": versions, "now": now, "withoutMetadata": without_metadata}
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


def _entry(version, expire_offset_days, html_missing=False):
    return {
        "version": version,
        "released": "2026-07-01T00:00:00.000Z",
        "expire": None if expire_offset_days is None
        else _iso(NOW_MS + expire_offset_days * DAY_MS),
        "htmlMissing": html_missing,
    }


def _iso(ms):
    import datetime
    return datetime.datetime.fromtimestamp(
        ms / 1000, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


class TestTheNewestStillValidBuildIsPinned:
    def test_an_expired_newest_entry_is_skipped(self, tmp_path):
        """The whole point: the last entry in the catalogue is not necessarily
        one Meta still serves."""
        result = _select(tmp_path, [
            _entry("2.3000.1000000001", 10),
            _entry("2.3000.1000000002", -1),
            _entry("2.3000.1000000003", -5),
        ])
        assert result["selected"]["version"] == "2.3000.1000000001"
        assert result["selected"]["expired"] is False

    def test_a_valid_newest_entry_is_taken_as_is(self, tmp_path):
        result = _select(tmp_path, [
            _entry("2.3000.1000000001", 10),
            _entry("2.3000.1000000002", 40),
        ])
        assert result["selected"]["version"] == "2.3000.1000000002"

    def test_expiry_is_checked_before_the_html_is_read(self, tmp_path):
        """Reading every HTML file down a 400-entry catalogue is minutes of
        I/O on a machine that is trying to start up."""
        result = _select(tmp_path, [
            _entry("2.3000.1000000001", 10),
            _entry("2.3000.1000000002", -1),
        ])
        assert result["pageContentAsked"] == ["2.3000.1000000001"]

    def test_a_build_this_install_cannot_assemble_is_skipped(self, tmp_path):
        """Unchanged from before: getPageContent() throwing means the HTML is
        not on disk here, so pinning it would land in the silent fallback."""
        result = _select(tmp_path, [
            _entry("2.3000.1000000001", 10),
            _entry("2.3000.1000000002", 40, html_missing=True),
        ])
        assert result["selected"]["version"] == "2.3000.1000000001"


class TestACatalogueWithNothingValidLeft:
    """The state a user who has not rebuilt the API in months ends up in."""

    def test_the_newest_build_is_still_pinned(self, tmp_path):
        """Running unpinned has a measured cost — WhatsApp Web serves a build
        the bundled wa-js may not support, and sending to an individual
        contact then fails silently. An expired pin fails visibly."""
        result = _select(tmp_path, [
            _entry("2.3000.1000000001", -30),
            _entry("2.3000.1000000002", -2),
        ])
        assert result["selected"]["version"] == "2.3000.1000000002"

    def test_and_it_is_reported_as_expired_so_the_log_can_say_so(self, tmp_path):
        result = _select(tmp_path, [_entry("2.3000.1000000001", -2)])
        assert result["selected"]["expired"] is True

    def test_an_empty_catalogue_pins_nothing(self, tmp_path):
        assert _select(tmp_path, [])["selected"] is None


class TestACatalogueWithoutDates:
    """An older wa-version, or an entry with no metadata: unknown expiry must
    read as "cannot prove it is dead", never as expired — otherwise the fix
    would break installs it was never about."""

    def test_the_newest_entry_is_pinned_unchanged(self, tmp_path):
        result = _select(tmp_path, [
            _entry("2.3000.1000000001", 10),
            _entry("2.3000.1000000002", 40),
        ], without_metadata=True)
        assert result["selected"]["version"] == "2.3000.1000000002"
        assert result["selected"]["expire"] is None
        assert result["selected"]["expired"] is False

    def test_a_single_entry_with_a_null_expire_is_still_usable(self, tmp_path):
        result = _select(tmp_path, [_entry("2.3000.1000000001", None)])
        assert result["selected"]["version"] == "2.3000.1000000001"
        assert result["selected"]["expired"] is False


class TestTheSourceSaysWhatToDoAboutIt:
    """Cheap source checks: the point of the change is that the next user log
    names the cause instead of nobody knowing, so the message itself is part
    of the contract."""

    def test_the_resolver_selects_by_validity_not_by_position(self):
        source = START_JS.read_text(encoding="utf-8")
        assert "selectServableVersion(waVersion, Date.now())" in source
        assert "available[available.length - 1]" not in source

    def test_an_expired_catalogue_is_logged_with_the_command_that_fixes_it(self):
        source = START_JS.read_text(encoding="utf-8")
        assert "EXPIRED" in source
        assert source.count("npm update @wppconnect/wa-version") >= 3
