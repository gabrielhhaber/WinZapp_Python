"""The interception never answers `/` with WhatsApp Web's login-only document.

Since 2.3000.1055857453 the @wppconnect/wa-version catalogue stores, for every
new build, the page Meta gives a browser WITHOUT a login (a 302 from `/` to
`/login/`, `WAWebLoggedOutBootstrap`, ~185 KB): the QR screen and nothing else.
start.js pins the newest build and its document-only interception answered
every `/` load with that page. A profile that was signed in then came back
logged out on its next start (2026-11-25: three times on one Mac on
2.3000.1056179073, while the same profile reached inChat on the full
documents of 1048711051 and 1055837054), and a phone that had just linked was
sent back to `/` and got the login page again (2026-11-24: "linked, then a new
QR code, then Phone not connected").

pinnedDocumentForRoot() refuses that document, and the interception then lets
WhatsApp answer `/` itself: the full app for a signed-in browser, the 302 to
`/login/` for a signed-out one. A full document is still served as before.

Exercised as pure JS sliced out of the real api_patches/start.js and run under
node against a fake page and CDP session, as tests/test_wa_version_expiry_pin.py
does: requiring all of start.js would scan for (and download) Chrome.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
START_JS = ROOT / "client" / "api_patches" / "start.js"

LOGIN_ONLY = '<html><script>__d("WAWebLoggedOutBootstrap",[],function(){})</script></html>'
FULL_APP = '<html><script>__d("WAWebMain",[],function(){})</script></html>'

HARNESS = r"""
'use strict';
const scenario = JSON.parse(process.argv[2]);
const warnings = [];
const quiet = { warn: (m) => warnings.push(m), log: () => {}, error: () => {} };
console.log = () => {};
console.warn = () => {};

__SOURCE__

(async () => {
  const sent = [];
  const handlers = {};
  const cdp = {
    send: async (method, params) => { sent.push({ method, params }); return {}; },
    on: (event, handler) => { handlers[event] = handler; },
  };
  const page = {
    createCDPSession: async () => cdp,
    on: () => {},
    mainFrame: () => null,
  };
  const root = pinnedDocumentForRoot(scenario.body, '2.3000.1056179073-alpha', quiet);
  await installPinnedPageInterception(page, root, null);
  for (const [i, url] of scenario.urls.entries()) {
    await handlers['Fetch.requestPaused']({ requestId: 'r' + i, request: { url } });
  }
  const answers = sent
    .filter((c) => c.method !== 'Fetch.enable')
    .map((c) => ({
      method: c.method,
      body: c.params.body ? Buffer.from(c.params.body, 'base64').toString() : null,
    }));
  const enable = sent.find((c) => c.method === 'Fetch.enable');
  process.stdout.write('__RESULT__' + JSON.stringify({
    rootIsNull: root === null, answers, warnings,
    patterns: enable ? enable.params.patterns.map((p) => p.urlPattern) : [],
  }));
})();
"""


def _source():
    """The shipped code from the login-only check through the interception,
    sliced between two real definitions rather than test markers."""
    source = START_JS.read_text(encoding="utf-8")
    start = source.index("const LOGIN_ONLY_DOCUMENT_MARKER")
    end = source.index("function patchWppconnectVersionPinning(", start)
    return source[start:end]


def _run(tmp_path, body, urls):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = tmp_path / "harness.js"
    script.write_text(HARNESS.replace("__SOURCE__", _source()), encoding="utf-8")
    out = subprocess.run(
        [node, str(script), json.dumps({"body": body, "urls": urls})],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    return json.loads(out.split("__RESULT__", 1)[1])


ROOT_URL = "https://web.whatsapp.com/"
CHECK_UPDATE = "https://web.whatsapp.com/check-update?version=1&platform=web"


class TestTheLoginOnlyDocument:
    def test_is_never_served_at_the_root(self, tmp_path):
        result = _run(tmp_path, LOGIN_ONLY, [ROOT_URL])
        assert result["rootIsNull"] is True
        assert result["answers"] == [{"method": "Fetch.continueRequest", "body": None}]

    def test_every_reload_is_left_to_whatsapp_too(self, tmp_path):
        """The reload after a phone links is the second `/` load: it must get
        WhatsApp's full app, not the login page again."""
        result = _run(tmp_path, LOGIN_ONLY, [ROOT_URL, ROOT_URL, ROOT_URL])
        assert [a["method"] for a in result["answers"]] == ["Fetch.continueRequest"] * 3

    def test_says_so_in_the_log(self, tmp_path):
        result = _run(tmp_path, LOGIN_ONLY, [ROOT_URL])
        assert any("login-only" in w and "2.3000.1056179073-alpha" in w
                   for w in result["warnings"])

    def test_check_update_is_still_aborted(self, tmp_path):
        result = _run(tmp_path, LOGIN_ONLY, [CHECK_UPDATE])
        assert result["answers"] == [{"method": "Fetch.failRequest", "body": None}]

    def test_the_interception_stays_narrow(self, tmp_path):
        """Still only `/` and check-update: the worker traffic the narrow
        pattern exists for is never paused."""
        result = _run(tmp_path, LOGIN_ONLY, [])
        assert result["patterns"] == [ROOT_URL, "https://web.whatsapp.com/check-update*"]


class TestAFullDocument:
    def test_is_served_as_before(self, tmp_path):
        result = _run(tmp_path, FULL_APP, [ROOT_URL, ROOT_URL])
        assert result["rootIsNull"] is False
        assert result["answers"] == [
            {"method": "Fetch.fulfillRequest", "body": FULL_APP},
            {"method": "Fetch.fulfillRequest", "body": FULL_APP},
        ]
        assert result["warnings"] == []


class TestTheWrapperUsesIt:
    def test_initwhatsapp_passes_the_checked_document(self):
        """The decision only matters if the wrapper hands its result, not the
        raw catalogue body, to the interception."""
        source = START_JS.read_text(encoding="utf-8")
        wrapper = source[source.index("function patchWppconnectVersionPinning("):]
        assert "pinnedDocumentForRoot(body, version)" in wrapper
        assert "installPinnedPageInterception(page, rootBody, log)" in wrapper
        assert "installPinnedPageInterception(page, body, log)" not in wrapper
