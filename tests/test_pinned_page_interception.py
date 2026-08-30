"""start.js must serve the pinned WhatsApp Web build *without* letting
WPPConnect install its blanket request interception.

Why this file exists at all: this is the single most consequential line in the
whole vendored API layer, and it has now silently reverted twice through
branch merges, each time reproducing the same user-visible bug — "conversations
only ever sync their last ~15 messages, even though the chat list shows the
right unread count".

The mechanism, recorded once so it does not have to be rediscovered a third
time (see the long comment block in api_patches/start.js for the measurements):

  * WPPConnect pins a WhatsApp Web build by calling
    page.setRequestInterception(true) and answering the document request with
    wa-version's HTML. That is a blanket Fetch.enable over every request the
    target makes, and puppeteer never answers the ones a dedicated Worker
    issues in CORS mode — they hang forever, with no error anywhere.
  * WhatsApp Web runs its entire storage/decode backend in such a worker, and
    every history-sync chunk handler awaits that worker's bridge before
    decoding. So the phone delivers history normally, WhatsApp Web stores it
    undecoded, and get-messages faithfully returns the ~2 messages per chat it
    actually managed to decode.
  * start.js therefore installs its own *narrow* interception (a raw-CDP
    Fetch.enable whose urlPattern matches only the document and check-update)
    and then passes version=undefined onward, which is the only thing that
    stops WPPConnect from adding its blanket one on top.

So the two assertions that matter are exactly: our narrow Fetch.enable went
out, and `undefined` — not the version string — reached WPPConnect.

The regression these tests were written against: the wrapper looked up
`waVersion.getPageContent(version)` while `waVersion` existed only as a local
inside resolveWhatsappVersion(). That is a ReferenceError, thrown inside a
`try { body = ... } catch { body = null }` — so it never crashed. It merely
made `body` null, which the wrapper reads as "wa-version cannot serve this
build" and which makes it hand the version straight to WPPConnect. One
undefined identifier, no stack trace, and the entire history-sync fix silently
off. A pure syntax check (`node --check`) cannot see it; only actually calling
the wrapper can, which is what these tests do.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
API = ROOT / "client" / "api"
PATCHES = ROOT / "client" / "api_patches"

# Executed with client/api as argv[2]. It stubs out everything that would boot
# a real server or a real browser, then invokes the wrapper start.js installs
# over WPPConnect's initWhatsapp and reports what it did.
HARNESS = r"""
'use strict';
const path = require('path');
const Module = require('module');
const apiDir = process.argv[2];

const out = {
  fetchEnableSent: false,
  fetchPatterns: [],
  versionPassedOnward: 'INIT_WHATSAPP_NEVER_CALLED',
  error: null,
};

// start.js boots the real server on its last line; stub the two compiled
// modules it reaches that through so requiring it has no side effects.
// (What start.js puts *into* that config — headless, useChrome, the selected
// binary — is asserted in test_headless_shell.py.)
const distConfig = path.join(apiDir, 'dist', 'config');
const distIndex = path.join(apiDir, 'dist', 'index');
const origLoad = Module._load;
Module._load = function (request) {
  if (request === distConfig) return { default: { createOptions: {}, webhook: {}, log: {} } };
  if (request === distIndex) return { initServer: () => {} };
  return origLoad.apply(this, arguments);
};

// Replace initWhatsapp BEFORE start.js patches it, so the wrapper wraps this
// spy rather than WPPConnect's real navigation code. start.js resolves the
// same module path, so it gets this same cached module object.
const wppEntry = require.resolve('@wppconnect-team/wppconnect/package.json', { paths: [apiDir] });
const browser = require(path.join(path.dirname(wppEntry), 'dist', 'controllers', 'browser'));
browser.initWhatsapp = async function (page, token, clear, version) {
  // JSON has no undefined, and undefined is the whole point of the assertion.
  out.versionPassedOnward = version === undefined ? '__UNDEFINED__' : version;
  return 'spy';
};

require(path.join(apiDir, 'start.js'));

const cdp = {
  send: async (method, params) => {
    if (method === 'Fetch.enable') {
      out.fetchEnableSent = true;
      out.fetchPatterns = (params && params.patterns) || [];
    }
  },
  on: () => {},
};
// A real puppeteer Page is an EventEmitter that also exposes mainFrame();
// start.js subscribes to 'framenavigated' to record reloads, so a stub
// without these makes the interception install throw and silently fall back
// to WPPConnect's blanket one — which is exactly what these tests exist to
// catch, so the stub has to be faithful enough not to trigger it spuriously.
const page = {
  createCDPSession: async () => cdp,
  on: () => {},
  mainFrame: () => ({ url: () => 'https://web.whatsapp.com/' }),
};

const waVersion = require(require.resolve('@wppconnect/wa-version', { paths: [path.dirname(wppEntry)] }));
const versions = waVersion.getAvailableVersions();
const pinned = versions[versions.length - 1];

browser.initWhatsapp(page, 'token', false, pinned, null, () => {})
  .catch((e) => { out.error = String((e && e.message) || e); })
  .then(() => { console.log('__RESULT__' + JSON.stringify(out)); });
"""


def _run_harness(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    if not (API / "start.js").exists():
        pytest.skip("client/api/ not set up here (run setup_api.py)")
    if not (API / "node_modules" / "@wppconnect-team" / "wppconnect").exists():
        pytest.skip("client/api/node_modules not installed here")

    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [node, str(harness), str(API)],
        capture_output=True, text=True, timeout=180,
    )
    marker = "__RESULT__"
    for line in proc.stdout.splitlines():
        if line.startswith(marker):
            return json.loads(line[len(marker):])
    raise AssertionError(
        "harness produced no result.\n"
        f"exit={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


class TestTheNarrowInterceptionIsActuallyInstalled:
    """These run the real start.js, so they fail on the real regression rather
    than on a copy of the logic restated in the test."""

    def test_our_own_document_only_interception_goes_out(self, tmp_path):
        result = _run_harness(tmp_path)
        assert result["error"] is None, result["error"]
        assert result["fetchEnableSent"], (
            "start.js did not install its own CDP Fetch.enable. The pinned HTML is "
            "then served by WPPConnect's blanket setRequestInterception(true), which "
            "hangs WhatsApp Web's backend worker — chats will only ever show their "
            "newest messages. See this module's docstring."
        )

    def test_wppconnect_is_handed_undefined_so_it_adds_no_blanket_interception(self, tmp_path):
        """Passing the version string onward is exactly how the bug manifests:
        WPPConnect sees a version to pin and installs the blanket interception,
        undoing the narrow one installed a moment earlier."""
        result = _run_harness(tmp_path)
        assert result["error"] is None, result["error"]
        assert result["versionPassedOnward"] == "__UNDEFINED__", (
            f"start.js passed version={result['versionPassedOnward']!r} on to "
            f"WPPConnect instead of undefined, so WPPConnect will install its "
            f"blanket request interception on top of ours and history sync dies."
        )


class TestTheSourceItself:
    """Cheap checks that need neither node nor node_modules, so they still run
    on a bare checkout and in CI before setup_api.py."""

    def test_the_page_content_lookup_does_not_depend_on_a_function_local(self):
        """The exact regression: the initWhatsapp wrapper referenced a
        `waVersion` binding that only existed inside resolveWhatsappVersion().
        Requiring the binding to be module-scope makes that unrepresentable."""
        src = (PATCHES / "start.js").read_text(encoding="utf-8")
        assert "\nconst waVersion = " in src, (
            "`waVersion` must be resolved once at module scope. When it was a "
            "function-local, the initWhatsapp wrapper's own `waVersion.getPageContent()` "
            "threw ReferenceError into a catch that turned it into 'wa-version cannot "
            "serve this build' — silently restoring WPPConnect's blanket interception."
        )
        assert "    const waVersion = requireWaVersion();" not in src, (
            "resolveWhatsappVersion() must use the module-scope binding, not shadow "
            "it with a local of the same name"
        )

    def test_the_wrapper_consumes_the_version(self):
        """`version = undefined` right after installing our interception is the
        line that keeps WPPConnect from adding its own."""
        src = (PATCHES / "start.js").read_text(encoding="utf-8")
        assert "version = undefined;" in src

    def test_the_blanket_interception_is_never_re_enabled_directly(self):
        """The comment block explaining the bug names the call, so only actual
        code lines count."""
        src = (PATCHES / "start.js").read_text(encoding="utf-8")
        offenders = [
            line for line in src.splitlines()
            if "setRequestInterception" in line and not line.lstrip().startswith(("//", "*"))
        ]
        assert not offenders, (
            f"start.js must never call page.setRequestInterception() itself — "
            f"that is the blanket interception that kills the backend worker: {offenders}"
        )

    def test_worker_disabling_flags_stay_out_of_the_browser_args(self):
        """WhatsApp Web runs its storage/decode backend inside workers, and its
        persistent-storage grant keys off the notifications permission — both
        flags were removed deliberately and must not come back with the next
        round of 'performance' flags."""
        src = (PATCHES / "start.js").read_text(encoding="utf-8")
        start = src.index("const optimizedBrowserArgs = [")
        args = src[start:src.index("];", start)]
        for flag in ("--disable-shared-workers", "--disable-workers", "--disable-notifications"):
            assert flag not in args, f"{flag} must not be in optimizedBrowserArgs"

    def test_the_two_copies_of_start_js_match(self):
        """api_patches/ is what setup_api.py restores from — a fix applied only
        to client/api/start.js is undone by the next setup run."""
        if not (API / "start.js").exists():
            pytest.skip("client/api/ not set up here")
        assert (API / "start.js").read_bytes() == (PATCHES / "start.js").read_bytes()


class TestTheDocumentPatternStaysAnExactMatch:
    """Widening this pattern is a trap that has already been walked into once.

    WhatsApp Web navigates itself to
    `https://web.whatsapp.com/?post_logout=1&logout_reason=0` on a fresh
    unpaired profile, which the exact-match pattern does not cover. Making it
    cover that (an origin-wide glob scoped to `resourceType: 'Document'`) looks
    like the obvious fix and breaks pairing outright: the page is force-fed the
    pinned document on the very navigation it is using to restart itself, so it
    loops every ~10s until WPPConnect force-kills the session at notLogged, and
    neither the QR nor the pairing code ever appears. With the exact match, the
    QR arrives about 12s after start-session."""

    def test_the_document_pattern_is_an_exact_match(self, tmp_path):
        result = _run_harness(tmp_path)
        assert result["error"] is None, result["error"]
        doc_patterns = [
            p for p in result["fetchPatterns"]
            if "web.whatsapp.com" in p.get("urlPattern", "")
            and "check-update" not in p.get("urlPattern", "")
        ]
        assert doc_patterns, f"no document pattern at all: {result['fetchPatterns']}"
        for pattern in doc_patterns:
            assert not pattern["urlPattern"].endswith("*"), (
                f"urlPattern {pattern['urlPattern']!r} is a glob. That re-serves the "
                "pinned document on WhatsApp Web's own ?post_logout navigation and "
                "puts the session into the reload loop. See this class's docstring."
            )

    def test_the_post_logout_url_is_deliberately_not_matched(self, tmp_path):
        """Asserted against the real URL taken from a live log, so the intent
        is unmistakable to whoever reads this next: that navigation is meant
        to reach the network untouched."""
        import fnmatch
        result = _run_harness(tmp_path)
        assert result["error"] is None, result["error"]
        post_logout = "https://web.whatsapp.com/?post_logout=1&logout_reason=0"
        for pattern in result["fetchPatterns"]:
            if "check-update" in pattern.get("urlPattern", ""):
                continue
            assert not fnmatch.fnmatchcase(post_logout, pattern["urlPattern"]), (
                f"{post_logout!r} must NOT match {pattern['urlPattern']!r}"
            )
