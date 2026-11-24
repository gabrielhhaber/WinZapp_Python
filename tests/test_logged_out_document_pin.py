"""A catalogue build whose document is WhatsApp Web's login page is not pinned
while a build with the full app document is left.

Measured 2026-11-24: WhatsApp Web answers `/` with a 302 to `/login/` for a
browser without a login (the WAWebLoggedOutSeparationGating experiment), and
that ~185 KB document (WAWebLoggedOutBootstrap) defines the QR screen only.
The wa-version crawler follows the redirect, so every build from
2.3000.1055857453 on held that document under `/`; selectServableVersion()
pinned the newest one and the interception served it for every `/` load: no
QR code on a new link (wa-js never reached WPP.isReady), and no app for a
linked profile.

Same approach as tests/test_wa_js_supported_range_pin.py: the real
selectServableVersion() is sliced out of start.js and driven under Node
against a synthetic catalogue; requiring the whole of start.js would run its
top-level Chrome scan.
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

FULL_DOCUMENT = '<html><script>__d("WAWebCommsHandleLoggedInStanza")</script></html>'
LOGIN_DOCUMENT = '<html><script>{"__dr":"WAWebLoggedOutBootstrap"}</script></html>'

HARNESS = r"""
'use strict';
const scenario = JSON.parse(process.argv[2]);
const catalogue = {
  getAvailableVersions: () => scenario.versions.map((v) => v.version),
  getPageContent: (version) => {
    const entry = scenario.versions.find((v) => v.version === version);
    if (!entry || entry.htmlMissing) throw new Error('Version not available for ' + version);
    return entry.html;
  },
  getVersionInfo: (version) => {
    const entry = scenario.versions.find((v) => v.version === version);
    if (!entry) throw new Error('Version not available for ' + version);
    return { version, beta: false, released: entry.released, expire: entry.expire };
  },
};

__SELECTOR_SOURCE__

console.log('__RESULT__' + JSON.stringify({
  selected: selectServableVersion(catalogue, scenario.now, scenario.range),
  loggedOut: (scenario.documents || []).map((html) => isLoggedOutOnlyDocument(html)),
}));
"""


def _node():
    bundled = ROOT / "client" / "node" / "node.exe"
    node = str(bundled) if bundled.exists() else shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    return node


def _selector_source():
    source = START_JS.read_text(encoding="utf-8")
    start = source.index("function versionExpiry(")
    end = source.index("function resolveWhatsappVersion(", start)
    return source[start:end]


def _run(tmp_path, versions=(), documents=(), range_=None):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS.replace("__SELECTOR_SOURCE__", _selector_source()),
                       encoding="utf-8")
    scenario = {"versions": list(versions), "documents": list(documents),
                "now": NOW_MS, "range": range_}
    proc = subprocess.run([_node(), str(harness), json.dumps(scenario)],
                          capture_output=True, text=True, timeout=60)
    for line in proc.stdout.splitlines():
        if line.startswith("__RESULT__"):
            return json.loads(line[len("__RESULT__"):])
    raise AssertionError(f"no result\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")


def _iso(ms):
    import datetime
    return datetime.datetime.fromtimestamp(
        ms / 1000, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _entry(version, login_only=False, expire_offset_days=30):
    return {
        "version": version,
        "released": "2026-11-01T00:00:00.000Z",
        "expire": _iso(NOW_MS + expire_offset_days * DAY_MS),
        "html": LOGIN_DOCUMENT if login_only else FULL_DOCUMENT,
    }


def test_the_login_document_is_recognised(tmp_path):
    result = _run(tmp_path, documents=[LOGIN_DOCUMENT, FULL_DOCUMENT, ""])
    assert result["loggedOut"] == [True, False, False]


def test_the_newest_full_document_wins_over_newer_login_documents(tmp_path):
    """The catalogue of 2026-11-24: 1055837054 full, everything after it
    login-only. Before, 1056039834 was pinned and no QR code ever came."""
    result = _run(tmp_path, versions=[
        _entry("2.3000.1055357543-alpha"),
        _entry("2.3000.1055837054-alpha"),
        _entry("2.3000.1055857453-alpha", login_only=True),
        _entry("2.3000.1056039834-alpha", login_only=True),
    ])
    selected = result["selected"]
    assert selected["version"] == "2.3000.1055837054-alpha"
    assert selected["loggedOutOnly"] is False and selected["expired"] is False


def test_a_catalogue_of_login_documents_only_still_pins_the_newest(tmp_path):
    """Never unpinned (docs/traps/whatsapp-web-version-pin.md): the newest is
    still pinned, flagged so start.js says why."""
    result = _run(tmp_path, versions=[
        _entry("2.3000.1055857453-alpha", login_only=True),
        _entry("2.3000.1056039834-alpha", login_only=True),
    ])
    assert result["selected"]["version"] == "2.3000.1056039834-alpha"
    assert result["selected"]["loggedOutOnly"] is True


def test_an_unexpired_login_document_beats_an_expired_full_one(tmp_path):
    """Expiry stays the stronger rule: Meta may refuse an expired build's
    assets outright, while a login document still shows the QR code."""
    result = _run(tmp_path, versions=[
        _entry("2.3000.1055837054-alpha", expire_offset_days=-1),
        _entry("2.3000.1056039834-alpha", login_only=True),
    ])
    assert result["selected"]["version"] == "2.3000.1056039834-alpha"
    assert result["selected"]["expired"] is False


def test_a_full_catalogue_is_chosen_exactly_as_before(tmp_path):
    result = _run(tmp_path, versions=[
        _entry("2.3000.1055357543-alpha"),
        _entry("2.3000.1055837054-alpha"),
    ])
    assert result["selected"]["version"] == "2.3000.1055837054-alpha"
    assert result["selected"]["loggedOutOnly"] is False
