"""WinZapp must launch the browser it chose, and history sync must survive it.

Two separate guarantees, because they failed in two different ways.

1. The intended binary has to actually be the one that gets launched.

   start.js originally switched to chrome-headless-shell unconditionally
   (smaller, faster, and with no windowing layer compiled in at all). But the
   switch silently never took effect on any machine that had run an older
   build, and the reason is worth keeping:

     * The search walked client/api/.cache once, matching the shell's and full
       Chrome's filenames in the same loop, and returned whatever the directory
       walk reached first.
     * A machine upgrading from an older WinZapp has
       .cache/puppeteer/chrome/win64-*/chrome-win64/chrome.exe sitting there
       from before, and an EMPTY .cache/chrome-headless-shell/ directory.
     * So the walk found full Chrome, `hasChrome` came back true, and the
       "install chrome-headless-shell" branch never ran. Forever: the empty
       directory was never the thing being checked.

   The fix is a two-pass search — one candidate list at a time, so the winner
   is decided by preference rather than by directory layout — plus keying the
   installer off the preferred binary specifically rather than off "any
   Chrome-ish binary".

   The preference itself is now PLATFORM-DEPENDENT, and that inversion is
   deliberate: chrome-headless-shell is a console-subsystem executable on
   Windows, and its renderer/GPU children can each allocate a visible console
   (seven windows were observed when opening the QR screen — the exact thing
   the shell was adopted to avoid). Full Chrome uses the GUI subsystem and
   stays windowless under Puppeteer's headless mode, so on Windows it is the
   preferred binary and the shell is the fallback. Everywhere else the
   original order stands.

   What these tests pin down is that the ordering, the installer's trigger,
   and the product each installer actually downloads all keep agreeing with
   each other. They drifted apart once already: start.js's policy was inverted
   while this file's assertions were softened into tautologies that could no
   longer tell either policy from the other.

2. The shell must not cost anything history sync depends on.

   chrome-headless-shell is Chrome's *old* headless implementation, a separate
   binary rather than a mode. That is exactly the kind of substitution that
   quietly removes an API a page depends on, and WhatsApp Web's history
   pipeline depends on several: it runs its storage/decode backend in workers,
   keeps everything in IndexedDB, and needs a persistent storage bucket or the
   browser may evict that database. Chrome's auto-grant heuristic for
   persistent storage also keys off the notifications permission, so a build
   without the Notification API can never get a persistent bucket.

   TestTheShellKeepsWhatWhatsAppWebNeeds launches the real shell with
   start.js's real flag list and probes each of those directly, rather than
   trusting that "headless is headless".
"""

import json
import pathlib
import shutil
import subprocess
import sys

import pytest
from tests.god_modules import main_window_source

ROOT = pathlib.Path(__file__).resolve().parents[1]
API = ROOT / "client" / "api"
PATCHES = ROOT / "client" / "api_patches"
CACHE = API / ".cache"
# start.js's findFullChrome() searches the user-level puppeteer cache too, so a
# test that wants to know whether the preferred binary is present has to look
# in the same places the code does — otherwise it skips itself on exactly the
# machines where it would have something to say.
HOME_CACHE = pathlib.Path.home() / ".cache" / "puppeteer"

SHELL_NAMES = ("chrome-headless-shell.exe", "chrome-headless-shell")
FULL_CHROME_NAMES = ("chrome.exe", "chrome")

ON_WINDOWS = sys.platform == "win32"
# Mirrors start.js's findPreferredChrome(). See this module's docstring for
# why Windows inverts the order.
PREFERRED_NAMES = FULL_CHROME_NAMES if ON_WINDOWS else SHELL_NAMES
PREFERRED_PRODUCT = "chrome" if ON_WINDOWS else "chrome-headless-shell"


def _find(names, roots=(CACHE,)):
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.name in names:
                return path
    return None


def _find_preferred():
    """The binary start.js would pick here, searched where start.js searches."""
    if ON_WINDOWS:
        return _find(FULL_CHROME_NAMES, (CACHE, HOME_CACHE))
    return _find(SHELL_NAMES)


def _start_js():
    return (PATCHES / "start.js").read_text(encoding="utf-8")


def _function_body(src, name):
    """The source of `function <name>()`, up to its closing brace at column 0."""
    marker = f"function {name}()"
    start = src.index(marker)
    end = src.index("\n}", start)
    return src[start:end]


def _node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    return node


def _run_node(script, tmp_path, timeout=300):
    harness = tmp_path / "probe.js"
    harness.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        [_node(), str(harness), str(API)], capture_output=True, text=True, timeout=timeout
    )
    for line in proc.stdout.splitlines():
        if line.startswith("__RESULT__"):
            return json.loads(line[len("__RESULT__"):])
    raise AssertionError(
        f"probe produced no result.\nexit={proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


class TestTheSelectionIsPlatformAware:
    """Source-level, so these run on a bare checkout too."""

    def test_the_two_candidate_sets_stay_separate(self):
        src = _start_js()
        assert "HEADLESS_SHELL_NAMES" in src and "FULL_CHROME_NAMES" in src, (
            "the two candidate sets must be separate lists, or the search "
            "collapses back into one pass whose winner is decided by directory "
            "layout rather than by preference — see this module's docstring"
        )

    def test_the_search_order_flips_on_windows_and_only_there(self):
        """Both orderings must be present, each behind the right branch. An
        assertion that accepts either one on its own (as a previous version of
        this test did) cannot tell the current policy from its opposite."""
        body = _function_body(_start_js(), "findAnyChrome")
        assert "process.platform === 'win32'" in body, (
            "findAnyChrome() must branch on the platform — the Windows console "
            "problem and the non-Windows preference are different answers"
        )
        win_branch, _, other_branch = body.partition(":")
        assert "findFullChrome() || findHeadlessShell()" in win_branch, (
            "on Windows full Chrome must be tried first: the shell is a "
            "console-subsystem binary whose children flash console windows"
        )
        assert "findHeadlessShell() || findFullChrome()" in other_branch, (
            "off Windows the shell must still win — it is smaller, faster and "
            "has no windowing layer compiled in at all"
        )

    def test_the_preferred_binary_agrees_with_the_search_order(self):
        """findPreferredChrome() decides what gets *installed*; findAnyChrome()
        decides what gets *launched*. If they disagree, start.js downloads one
        browser on every boot and then launches the other."""
        src = _start_js()
        preferred = _function_body(src, "findPreferredChrome")
        any_chrome = _function_body(src, "findAnyChrome")
        win_pref, _, other_pref = preferred.partition(":")
        win_any, _, other_any = any_chrome.partition(":")
        assert "findFullChrome()" in win_pref and "findFullChrome()" in win_any, (
            "the Windows branches of findPreferredChrome() and findAnyChrome() "
            "must name the same binary"
        )
        assert "findHeadlessShell()" in other_pref and "findHeadlessShell()" in other_any, (
            "the non-Windows branches of findPreferredChrome() and "
            "findAnyChrome() must name the same binary"
        )

    def test_the_installer_is_keyed_off_the_preferred_binary(self):
        """`if (!hasChrome)` was the original bug: a leftover full Chrome made
        it false and the intended binary was never fetched. The trigger has to
        name the binary actually wanted here, not "anything Chrome-ish"."""
        src = _start_js()
        assert "if (!findPreferredChrome())" in src, (
            "the auto-install must trigger when the *preferred* binary is "
            "missing, not when any Chrome-like binary is missing"
        )
        # The old flag's name survives in the comment explaining the bug, so
        # only real code lines count.
        code = [
            line for line in src.splitlines()
            if not line.lstrip().startswith(("//", "*"))
        ]
        assert not [line for line in code if "hasChrome" in line], (
            "the old any-Chrome-will-do flag must be gone from the code"
        )

    def test_the_product_installed_is_the_product_preferred(self):
        """start.js must fetch whatever findPreferredChrome() looks for. A
        hardcoded product here is how a machine ends up permanently
        re-downloading a browser it will never launch."""
        src = _start_js()
        assert (
            "const browserProduct = process.platform === 'win32' "
            "? 'chrome' : 'chrome-headless-shell';"
        ) in src, "the product to install must be derived from the platform"
        assert "browsers install ${browserProduct}" in src, (
            "the npx invocation must use browserProduct, not a literal product"
        )
        assert "browsers install chrome-headless-shell" not in src
        assert "browsers install chrome'" not in src

    def test_the_in_app_installer_downloads_the_same_product(self):
        """ApiSetupDialog is the flow every end user goes through just by
        running WinZapp. If it fetches a different product than start.js
        prefers, the first launch downloads a second browser on top of it."""
        src = (ROOT / "client" / "ui" / "dialogs" / "api_setup.py").read_text(encoding="utf-8")
        # Collapsed so the assertion survives reformatting of a line that is
        # long enough to get wrapped.
        flat = " ".join(src.split())
        assert (
            '"chrome" if sys.platform == "win32" else "chrome-headless-shell"'
        ) in flat, "the in-app installer must pick the product per platform too"
        assert '"browsers", "install", browser_product' in flat, (
            "the install command must use browser_product, not a literal"
        )
        assert '"install", "chrome-headless-shell"' not in flat
        assert '"install", "chrome"' not in flat

    def test_every_install_path_uses_the_cache_dir_start_js_searches(self):
        """start.js searches (and exports as PUPPETEER_CACHE_DIR)
        client/api/.cache. An installer writing into .cache/puppeteer instead
        downloads a browser into a tree the server is not the one looking in."""
        for rel in ("client/ui/dialogs/api_setup.py", "client/main.py"):
            src = (main_window_source() if rel == "client/main.py"
                   else (ROOT / rel).read_text(encoding="utf-8"))
            offenders = [
                line.strip() for line in src.splitlines()
                if 'resource_path("api", ".cache", "puppeteer")' in line
            ]
            assert not offenders, f"{rel} still points at the .cache/puppeteer subtree: {offenders}"
        start = (PATCHES / "start.js").read_text(encoding="utf-8")
        assert "const puppeteerCacheDir = path.join(__dirname, '.cache');" in start

    def test_a_gui_browser_can_never_be_launched(self):
        """headless and useChrome are both pinned in start.js rather than left
        to upstream defaults. useChrome in particular is not cosmetic: when
        true, WPPConnect's initBrowser() locates the *system* Chrome and
        overwrites puppeteerOptions.executablePath with it, discarding the
        shell we selected — with no log line saying so."""
        src = (PATCHES / "start.js").read_text(encoding="utf-8")
        assert "headless: true," in src
        assert "useChrome: false," in src


class TestTheLaunchConfigStartJsBuilds:
    """Runs the real start.js and inspects the config it hands initServer()."""

    HARNESS = r"""
'use strict';
const path = require('path');
const Module = require('module');
const apiDir = process.argv[2];
const out = { launch: null };

const distConfig = path.join(apiDir, 'dist', 'config');
const distIndex = path.join(apiDir, 'dist', 'index');
const origLoad = Module._load;
Module._load = function (request) {
  if (request === distConfig) return { default: { createOptions: {}, webhook: {}, log: {} } };
  if (request === distIndex) {
    return { initServer: (cfg) => {
      const co = (cfg && cfg.createOptions) || {};
      out.launch = {
        headless: co.headless,
        useChrome: co.useChrome,
        executablePath: (co.puppeteerOptions || {}).executablePath || null,
        browserArgs: co.browserArgs || [],
      };
    } };
  }
  return origLoad.apply(this, arguments);
};

require(path.join(apiDir, 'start.js'));
console.log('__RESULT__' + JSON.stringify(out));
"""

    @pytest.fixture(scope="class")
    @classmethod
    def launch(cls, tmp_path_factory):
        if not (API / "start.js").exists():
            pytest.skip("client/api/ not set up here")
        if not (API / "node_modules" / "@wppconnect-team" / "wppconnect").exists():
            pytest.skip("client/api/node_modules not installed here")
        if _find_preferred() is None:
            # Requiring start.js with the preferred binary absent triggers a
            # ~90-170MB download; never do that from a test.
            pytest.skip(f"{PREFERRED_PRODUCT} not installed in a cache start.js searches")
        result = _run_node(cls.HARNESS, tmp_path_factory.mktemp("launch"))
        assert result["launch"] is not None, "start.js never called initServer()"
        return result["launch"]

    def test_the_selected_binary_matches_the_platform_preference(self, launch):
        exe = launch["executablePath"]
        assert exe, "no executablePath was pinned — puppeteer would pick its own browser"
        name = pathlib.Path(exe).name
        why = (
            "on Windows the shell is a console-subsystem binary whose renderer "
            "and GPU children each allocate a visible console window"
            if ON_WINDOWS else
            "off Windows the shell has no windowing layer compiled in at all"
        )
        assert name in PREFERRED_NAMES, (
            f"start.js selected {exe!r}, but this platform's preferred binary is "
            f"{PREFERRED_PRODUCT} — {why}. Note the preferred binary IS present "
            f"here ({_find_preferred()}), so this is a selection bug, not a "
            f"missing download."
        )

    def test_headless_and_use_chrome_are_pinned(self, launch):
        assert launch["headless"] is True
        assert launch["useChrome"] is False, (
            "useChrome=true makes WPPConnect replace our executablePath with the "
            "system-installed Chrome"
        )

    def test_no_flag_removes_part_of_the_worker_or_notification_surface(self, launch):
        """WhatsApp Web runs its storage/decode backend in workers, and the
        persistent-storage auto-grant keys off the notifications permission."""
        for flag in ("--disable-shared-workers", "--disable-workers", "--disable-notifications"):
            assert flag not in launch["browserArgs"], f"{flag} must not be launched with"


class TestTheShellKeepsWhatWhatsAppWebNeeds:
    """Launches the real chrome-headless-shell with start.js's real flag list.

    Every probe here corresponds to something the history pipeline actually
    uses; a shell build missing any of them would lose messages rather than
    fail loudly.
    """

    HARNESS = r"""
'use strict';
const path = require('path');
const http = require('http');
const fs = require('fs');
const apiDir = process.argv[2];
const puppeteer = require(require.resolve('puppeteer', { paths: [apiDir] }));

const SHELL = ['chrome-headless-shell.exe', 'chrome-headless-shell'];
function find(dir, names, d) {
  if (d > 6) return null;
  let e; try { e = fs.readdirSync(dir, { withFileTypes: true }); } catch (x) { return null; }
  const subs = [];
  for (const en of e) { const f = path.join(dir, en.name);
    if (en.isDirectory()) subs.push(f); else if (names.includes(en.name)) return f; }
  for (const s of subs) { const f = find(s, names, d + 1); if (f) return f; }
  return null;
}

// Reuse start.js's real flag list rather than a copy of it.
const startJs = fs.readFileSync(path.join(apiDir, 'start.js'), 'utf8');
const bs = startJs.indexOf('const optimizedBrowserArgs = [');
const args = [...startJs.slice(bs, startJs.indexOf('];', bs)).matchAll(/'(--[^']+)'/g)].map((m) => m[1]);

const out = { caps: null, worker: null, error: null };

// Two ports = two origins, so the cross-origin path is exercised offline.
function servers() {
  return new Promise((resolve) => {
    const b = http.createServer((req, res) => {
      res.writeHead(200, { 'Content-Type': 'application/javascript', 'Access-Control-Allow-Origin': '*' });
      res.end('export const value = 42;');
    });
    b.listen(0, '127.0.0.1', () => {
      const portB = b.address().port;
      const a = http.createServer((req, res) => {
        if (req.url === '/sw.js') {
          res.writeHead(200, { 'Content-Type': 'application/javascript' });
          res.end('self.addEventListener("install",()=>{});');
          return;
        }
        if (req.url === '/worker.js') {
          res.writeHead(200, { 'Content-Type': 'application/javascript' });
          // What WAWebBackendWorker does at boot: a module worker importing
          // its bundles cross-origin, then a CORS fetch.
          res.end(`self.onmessage = async () => { const log = [];
            try { const m = await import('http://127.0.0.1:${portB}/mod.js'); log.push('import:' + m.value); }
            catch (e) { log.push('import-err:' + e.message); }
            try { const r = await fetch('http://127.0.0.1:${portB}/mod.js', {mode:'cors'}); log.push('fetch:' + r.status); }
            catch (e) { log.push('fetch-err:' + e.message); }
            self.postMessage(log.join('|')); };`);
          return;
        }
        res.writeHead(200, { 'Content-Type': 'text/html' });
        res.end(`<!doctype html><title>caps</title><script>
          const w = new Worker('/worker.js', { type: 'module' });
          window.__w = new Promise((res) => { w.onmessage = (e) => res(e.data); });
          w.postMessage('go');
        </script>`);
      });
      a.listen(0, '127.0.0.1', () => resolve({ a, b, portA: a.address().port }));
    });
  });
}

(async () => {
  const { a, b, portA } = await servers();
  let browser = null;
  try {
    const executable = find(path.join(apiDir, '.cache'), SHELL, 0);
    if (!executable) throw new Error('chrome-headless-shell not installed');
    browser = await puppeteer.launch({ headless: true, executablePath: executable, args });
    const page = await browser.newPage();
    const origin = 'http://127.0.0.1:' + portA;
    // The same grant createSessionUtil.ts performs, over a CDP session kept alive.
    const cdp = await page.createCDPSession();
    await cdp.send('Browser.grantPermissions', { origin, permissions: ['durableStorage', 'notifications'] });
    await page.goto(origin + '/', { waitUntil: 'domcontentloaded', timeout: 30000 });
    out.caps = await page.evaluate(async () => {
      const o = {};
      o.notificationApi = typeof Notification;
      try { o.persistedBefore = await navigator.storage.persisted();
            if (!o.persistedBefore) await navigator.storage.persist();
            o.persisted = await navigator.storage.persisted(); }
      catch (e) { o.persisted = 'err:' + e.message; }
      try { await new Promise((res, rej) => { const r = indexedDB.open('probe', 1);
              r.onsuccess = () => res(); r.onerror = () => rej(r.error); });
            o.indexedDB = 'ok'; } catch (e) { o.indexedDB = 'err:' + e.message; }
      o.worker = typeof Worker;
      o.sharedWorker = typeof SharedWorker;
      try { await navigator.serviceWorker.register('/sw.js'); o.serviceWorker = 'ok'; }
      catch (e) { o.serviceWorker = 'err:' + e.message; }
      o.wasm = typeof WebAssembly;
      o.subtleCrypto = typeof crypto.subtle;
      return o;
    });
    out.worker = await page.evaluate((ms) => Promise.race([
      window.__w, new Promise((r) => setTimeout(() => r('HANG'), ms)),
    ]), 15000);
  } catch (e) { out.error = String((e && e.stack) || e); }
  finally {
    if (browser) { try { await browser.close(); } catch (e) {} }
    a.close(); b.close();
    console.log('__RESULT__' + JSON.stringify(out));
    process.exit(0);
  }
})();
"""

    @pytest.fixture(scope="class")
    @classmethod
    def probe(cls, tmp_path_factory):
        if _find(SHELL_NAMES) is None:
            pytest.skip("chrome-headless-shell not installed in client/api/.cache")
        if not (API / "node_modules" / "puppeteer").exists():
            pytest.skip("puppeteer not installed in client/api/node_modules")
        result = _run_node(cls.HARNESS, tmp_path_factory.mktemp("caps"))
        assert result["error"] is None, result["error"]
        return result

    def test_a_module_worker_can_import_and_fetch_cross_origin(self, probe):
        """The single most important one. WhatsApp Web boots its entire
        storage/decode backend in a module worker that imports its bundles from
        another origin, and every history-sync chunk handler waits on that
        worker's bridge before decoding. If this hangs, chats show only their
        newest messages and nothing anywhere reports an error."""
        assert probe["worker"] == "import:42|fetch:200", (
            f"module worker cross-origin path degraded under chrome-headless-shell: "
            f"{probe['worker']!r}"
        )

    def test_persistent_storage_is_granted(self, probe):
        """Without a persistent bucket WhatsApp Web's IndexedDB is evictable,
        and the decoded history can be thrown away between runs."""
        caps = probe["caps"]
        assert caps["notificationApi"] == "function", (
            "Chrome's auto-grant heuristic for persistent storage keys off the "
            "notifications permission — with the API gone, persist() can only "
            "ever return false"
        )
        assert caps["persisted"] is True

    def test_the_storage_and_worker_surface_is_intact(self, probe):
        caps = probe["caps"]
        assert caps["indexedDB"] == "ok"
        assert caps["worker"] == "function"
        assert caps["sharedWorker"] == "function"
        assert caps["serviceWorker"] == "ok"
        assert caps["wasm"] == "object"
        assert caps["subtleCrypto"] == "object"
