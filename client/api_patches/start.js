const path = require('path');
const fs = require('fs');
const os = require('os');

// Garante que o Puppeteer saiba onde encontrar o cache do Chrome
const puppeteerCacheDir = path.join(__dirname, '.cache');
process.env.PUPPETEER_CACHE_DIR = puppeteerCacheDir;

// WinZapp runs chrome-headless-shell, never a GUI-capable Chrome. The shell is
// a smaller, faster binary with no windowing/UI layer compiled in at all, which
// is the whole point: nothing can ever pop a visible window in a blind user's
// face, and startup is measurably quicker.
//
// The search is deliberately in TWO passes rather than one loop matching both
// names. With a single pass the winner is whatever the directory walk happens
// to reach first, which is not a decision — it is an accident of layout:
//
//   .cache/chrome-headless-shell/   <- may exist but be EMPTY (a stub the
//                                      downloader leaves behind, or a wiped
//                                      install)
//   .cache/puppeteer/chrome/win64-*/chrome-win64/chrome.exe   <- full Chrome,
//                                      left over from before WinZapp switched
//
// That is exactly the state this repo was in: the shell directory existed and
// was empty, the one-pass walk fell through to the leftover full chrome.exe,
// `hasChrome` came back true, so the chrome-headless-shell install below never
// ran and WPPConnect was handed full Chrome — the switch to the shell had
// silently never taken effect on any machine that had ever run the older build.
//
// Two passes make the preference explicit and stable: the shell wins whenever a
// real shell binary exists anywhere under the cache, full Chrome is only ever a
// fallback for an install that could not fetch the shell, and an empty shell
// directory triggers the install instead of being papered over.
const HEADLESS_SHELL_NAMES = ['chrome-headless-shell.exe', 'chrome-headless-shell'];
const FULL_CHROME_NAMES = ['chrome.exe', 'chrome', 'Chromium'];

function findExecutable(dir, names, depth) {
  if (depth > 6) return null;
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch (e) {
    return null;
  }
  const subdirs = [];
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      subdirs.push(full);
    } else if (names.includes(entry.name)) {
      return full;
    }
  }
  // Files in this directory before descending, so a binary sitting right here
  // is never lost to a deeper one under a sibling folder.
  for (const sub of subdirs) {
    const found = findExecutable(sub, names, depth + 1);
    if (found) return found;
  }
  return null;
}

function findHeadlessShell() {
  return findExecutable(puppeteerCacheDir, HEADLESS_SHELL_NAMES, 0);
}

function findFullChrome() {
  const roots = [
    puppeteerCacheDir,
    path.join(os.homedir(), '.cache', 'puppeteer')
  ];
  for (const root of roots) {
    const found = findExecutable(root, FULL_CHROME_NAMES, 0);
    if (found) return found;
  }
  return null;
}

function findAnyChrome() {
  // chrome-headless-shell is a console-subsystem executable on Windows. Its
  // renderer/GPU children can each allocate a visible console (seven windows
  // were observed when opening the QR screen). Full Chrome uses the GUI
  // subsystem and remains windowless under Puppeteer's headless mode.
  return process.platform === 'win32'
    ? findFullChrome() || findHeadlessShell()
    : findHeadlessShell() || findFullChrome();
}

function findPreferredChrome() {
  return process.platform === 'win32' ? findFullChrome() : findHeadlessShell();
}

// Locate a downloaded-but-maybe-unextracted chrome-headless-shell ZIP, so a
// botched extraction can be redone by hand (see the safeguard below).
function findHeadlessShellZip() {
  const shellCacheDir = path.join(puppeteerCacheDir, 'chrome-headless-shell');
  let entries;
  try {
    entries = fs.readdirSync(shellCacheDir, { withFileTypes: true });
  } catch (e) {
    return null;
  }
  for (const entry of entries) {
    if (entry.isFile() && /chrome-headless-shell.*\.zip$/i.test(entry.name)) {
      return path.join(shellCacheDir, entry.name);
    }
  }
  return null;
}

// puppeteer lays the shell out under .cache/chrome-headless-shell/win64-<ver>/,
// and the ZIP is named "<ver>-chrome-headless-shell-win64.zip" — derive that
// destination so a manual extraction lands exactly where findHeadlessShell()
// looks. Falls back to the ZIP's own dir if the version can't be parsed.
function headlessShellDestForZip(zipPath) {
  const m = path.basename(zipPath).match(/^(\d+\.\d+\.\d+\.\d+)-/);
  const shellCacheDir = path.dirname(zipPath);
  return m ? path.join(shellCacheDir, 'win64-' + m[1]) : shellCacheDir;
}

// Extract a ZIP the way puppeteer's own bundled unzip failed to. PowerShell's
// Expand-Archive ships with every supported Windows; unzip covers dev/Linux.
function extractZip(zipPath, destDir) {
  const { execSync } = require('child_process');
  try {
    fs.mkdirSync(destDir, { recursive: true });
  } catch (e) {}
  if (process.platform === 'win32') {
    execSync(
      'powershell -NoProfile -ExecutionPolicy Bypass -Command ' +
        `"Expand-Archive -LiteralPath '${zipPath}' -DestinationPath '${destDir}' -Force"`,
      { stdio: 'inherit', windowsHide: true }
    );
  } else {
    execSync(`unzip -o "${zipPath}" -d "${destDir}"`, { stdio: 'inherit' });
  }
}

if (!findPreferredChrome()) {
  const browserProduct = process.platform === 'win32' ? 'chrome' : 'chrome-headless-shell';
  console.log(`[chrome-install] ${browserProduct} não encontrado. Instalando automaticamente (isso pode levar alguns minutos)...`);
  try {
    const { execSync } = require('child_process');
    const nodeDir = path.dirname(process.execPath);
    const env = {
      ...process.env,
      PUPPETEER_CACHE_DIR: puppeteerCacheDir
    };
    if (process.platform === 'win32') {
      env.Path = `${nodeDir};${env.Path || ''};${env.PATH || ''}`;
    } else {
      env.PATH = `${nodeDir}:${env.PATH || ''}`;
    }
    // Prefer invoking npm's own npx-cli.js with the Node binary we are already
    // running under. WinZapp ships a portable Node in client/node/, and on a
    // machine with no system-wide Node the bare `npx` command simply does not
    // resolve — prepending nodeDir to PATH above is not enough, because there
    // is no npx.cmd shim inside the portable extraction on every layout.
    const npxCli = path.join(nodeDir, 'node_modules', 'npm', 'bin', 'npx-cli.js');
    const npxCmd = fs.existsSync(npxCli)
      ? `"${process.execPath}" "${npxCli}" puppeteer browsers install ${browserProduct}`
      : `npx puppeteer browsers install ${browserProduct}`;
    execSync(npxCmd, {
      cwd: __dirname,
      stdio: 'inherit',
      env: env,
      windowsHide: true
    });
    console.log('[chrome-install] chrome-headless-shell instalado com sucesso!');
  } catch (err) {
    console.error('[chrome-install] Falha ao instalar o chrome-headless-shell automaticamente:', err);
  }
}

// Safeguard against puppeteer's own downloader botching the extraction: on some
// Windows setups it fetches the full ZIP but leaves only ABOUT/LICENSE behind —
// no chrome-headless-shell.exe — and then, seeing the ZIP already on disk, never
// re-downloads, so the session hangs in INITIALIZING (offline) forever. If the
// binary is still missing but its ZIP is here, extract it ourselves.
if (!findHeadlessShell()) {
  const zip = findHeadlessShellZip();
  if (zip) {
    const dest = headlessShellDestForZip(zip);
    console.log(`[chrome-install] Instalação do chrome-headless-shell incompleta; extraindo o ZIP manualmente: ${zip}`);
    try {
      extractZip(zip, dest);
      if (findHeadlessShell()) {
        console.log('[chrome-install] chrome-headless-shell extraído manualmente com sucesso!');
      } else {
        console.error('[chrome-install] Extração manual não produziu o executável esperado.');
      }
    } catch (err) {
      console.error('[chrome-install] Falha ao extrair o chrome-headless-shell manualmente:', err);
    }
  }
}

const chromeExecutable = findAnyChrome();
if (chromeExecutable && !HEADLESS_SHELL_NAMES.includes(path.basename(chromeExecutable))) {
  console.warn(
    `[chrome-install] chrome-headless-shell indisponível; usando ${path.basename(chromeExecutable)} ` +
    'em modo headless como alternativa. Nenhuma janela será exibida.'
  );
} else if (chromeExecutable) {
  console.log(`[chrome-install] Usando chrome-headless-shell: ${chromeExecutable}`);
}

// Carrega a configuração padrão compilada
const distPath = path.join(__dirname, 'dist');
const configDefault = require(path.join(distPath, 'config')).default;
const { initServer } = require(path.join(distPath, 'index'));

// Carrega as configurações personalizadas de config.json
let customConfig = {};
const customConfigPath = path.join(__dirname, 'config.json');
if (fs.existsSync(customConfigPath)) {
  try {
    customConfig = JSON.parse(fs.readFileSync(customConfigPath, 'utf8'));
  } catch (e) {
    console.error('Erro ao ler config.json:', e);
  }
}

// Sobrescreve com variáveis de ambiente do processo se fornecidas
if (process.env.PORT) {
  customConfig.port = process.env.PORT;
}
if (process.env.AUTHENTICATION_API_KEY) {
  customConfig.secretKey = process.env.AUTHENTICATION_API_KEY;
}

// Optimized browser arguments to limit Puppeteer/Chromium CPU and Memory usage
//
// NOTE on what was deliberately left OUT of this list: an earlier version
// included '--js-flags="--max-old-space-size=350"', capping the WhatsApp
// Web renderer's own V8 JS heap at 350MB. Every other flag here reduces
// memory by disabling an optional feature (cache, GPU, extensions, ...) —
// a safe degradation. --max-old-space-size is categorically different: it's
// a hard ceiling, and WhatsApp Web's own JS heap (message store, media
// metadata, many open/cached chats — exactly the "muitas conversas
// chegando" scenario) can legitimately need more than 350MB under real
// load. Hitting the ceiling doesn't slow anything down gracefully — V8
// throws "JavaScript heap out of memory" and the renderer process crashes
// outright, which is what a WhatsApp Web page dying and needing WPPConnect
// to resync would look like from WinZapp's side. Removed so V8 falls back
// to its own default (auto-scaled off available system memory, normally
// well over 1GB) — a real, if higher, ceiling instead of an artificial low
// one that trades a memory-usage improvement for occasional hard crashes.
//
// NOTE on '--disable-software-rasterizer'/'--disable-3d-apis'/
// '--disable-webgl' (also removed, same reasoning): every video send
// (message attachment AND status) failed with "MediaUnsupportedError:
// video loaded with duration but no dims" — WhatsApp Web's own upload flow
// reads a video's container metadata fine (duration needs no decode) but
// never got past that, because reading videoWidth/videoHeight off an
// HTMLVideoElement needs at least one frame actually decoded and
// composited. '--disable-gpu' alone is a normal, well-supported way to run
// headless Chrome (it falls back to SwiftShare/SwiftShader, a bundled
// software rasterizer) — but stacking '--disable-software-rasterizer' on
// TOP of '--disable-gpu' removes that fallback too, leaving no
// rasterization backend at all; '--disable-3d-apis'/'--disable-webgl'
// block the same GPU/compositor surface from a different angle. Images
// were never affected (a static image decode/thumbnail doesn't go through
// this compositor pipeline the same way), which is exactly the "photos
// work, videos don't, even tiny ones" split that was reported.
// '--disable-gpu' itself is kept — it's the one that actually saves
// CPU/memory in a windowless session, and on its own still leaves the
// software rasterizer fallback available.
const optimizedBrowserArgs = [
  '--disable-renderer-accessibility',
  '--disable-web-security',
  '--no-sandbox',
  '--disable-background-networking',
  '--disable-default-apps',
  '--disable-extensions',
  '--disable-sync',
  '--disable-dev-shm-usage',
  '--disable-gpu',
  '--disable-translate',
  '--hide-scrollbars',
  '--metrics-recording-only',
  '--mute-audio',
  '--no-first-run',
  '--safebrowsing-disable-auto-update',
  '--ignore-certificate-errors',
  '--ignore-ssl-errors',
  '--ignore-certificate-errors-spki-list',
  '--no-zygote',
  '--disable-component-update',
  '--disable-speech-api',
  '--disable-voice-input',
  '--disable-renderer-backgrounding',
  '--disable-backgrounding-occluded-windows',
  '--disable-features=OptimizationGuideOnDeviceModel,PromptAPIForGeminiNano,AISummarization,HelpMeWrite,OptimizationGuide,OptimizationHints,OptimizationTargetPrediction',
  '--disable-ipc-flooding-protection',
  '--disable-breakpad',
  '--password-store=basic',
  '--use-mock-keychain',
  '--no-pings',
  '--disable-client-side-phishing-detection',
];

// WPPConnect pins the WhatsApp Web version (default '2.3000.10305x') by serving
// that build's HTML from the @wppconnect/wa-version package. When the pinned
// version is not in that package it does NOT fail — it logs
//   "Version not available for <v>, using latest as fallback"
// and lets WhatsApp Web serve its newest build, which can be more recent than
// the bundled wa-js supports. That is how sending to individual contacts started
// failing silently: every usync query hung, WhatsApp Web flagged the message
// isSendFailure with ack 0, and the REST call still answered 200. Groups kept
// working because they use sender keys and need no usync.
//
// Rather than hardcoding a version — which rots as soon as WhatsApp removes the
// old build's assets (HTTP 410) — ask wa-version itself for the newest build it
// can serve. `npm update @wppconnect/wa-version` is then enough to keep up with
// WhatsApp Web.
//
// wa-version is resolved through WPPConnect's own dependency tree, never as a
// direct dependency of ours. WPPConnect's setWhatsappVersion() serves the HTML
// from the copy *it* resolved, so reading any other copy risks picking a build
// that its catalogue does not have — which lands right back in the silent
// "using latest as fallback" path. Declaring our own `@wppconnect/wa-version`
// range would do exactly that the moment the two ranges stop overlapping (say
// WPPConnect moves to ^2 while ours still says ^1): npm then installs two
// copies, and the version we pin would be looked up in the wrong one.
function requireWaVersion() {
  const path = require('path');
  try {
    const wppEntry = require.resolve('@wppconnect-team/wppconnect/package.json');
    return require(require.resolve('@wppconnect/wa-version', {
      paths: [path.dirname(wppEntry)],
    }));
  } catch (e) {
    // Hoisted layout / older npm: fall back to plain resolution.
    return require('@wppconnect/wa-version');
  }
}

// Resolved once, at module scope, and used by BOTH callers below.
//
// This used to be a `const waVersion` local to resolveWhatsappVersion(), while
// the initWhatsapp wrapper further down reached for a bare `waVersion`
// identifier that existed nowhere in its scope. That is a ReferenceError, and
// it was thrown inside the wrapper's `try { body = ... } catch { body = null }`
// — so it never surfaced as a crash. It surfaced as `body === null`, which the
// wrapper reads as "wa-version cannot serve this build", which makes it hand
// the version straight to WPPConnect and let WPPConnect install the blanket
// request interception this whole file exists to avoid. One undefined
// identifier silently disabled the entire history-sync fix; keeping the module
// on one shared binding is what makes that unrepresentable.
const waVersion = (() => {
  try {
    return requireWaVersion();
  } catch (e) {
    return null;
  }
})();

// How close to its own expiry a pinned build may be before every launch says
// so. The catalogue's entries carry an ~2-month window, so two weeks still
// leaves `npm update @wppconnect/wa-version` something newer to find, while
// not crying wolf on a build that has weeks left.
const VERSION_EXPIRY_WARNING_MS = 14 * 24 * 60 * 60 * 1000;

// Expiry timestamp of one catalogue entry, or null when it cannot be known.
//
// Every entry in wa-version's versions.json carries `released` and `expire` —
// Meta stops serving a build's assets around that date, and nothing local can
// tell: getPageContent() only proves the HTML can be assembled from disk, and
// it keeps succeeding happily for a build WhatsApp abandoned weeks ago. That
// is what made "the pin works fine" and "sending silently fails" coexist.
//
// null (no metadata, unparseable date, an older wa-version without
// getVersionInfo) means "cannot prove it is dead", never "expired": an install
// whose catalogue carries no dates keeps exactly the previous behaviour.
function versionExpiry(catalogue, version) {
  try {
    if (typeof catalogue.getVersionInfo !== 'function') return null;
    const info = catalogue.getVersionInfo(version);
    if (!info || !info.expire) return null;
    const expire = Date.parse(info.expire);
    return Number.isNaN(expire) ? null : expire;
  } catch (e) {
    return null;
  }
}

// The newest build this install can serve AND that Meta has not expired yet.
//
// Walks the catalogue newest-first and stops at the first entry that is both
// unexpired and actually assemblable — getPageContent() throws when the HTML
// is missing from this install, so a pin is only ever a build known to exist
// locally. The expiry check comes first because it is the cheap one; reading
// every HTML file down the list would be minutes of I/O.
//
// Returns { version, expire, expired, total }, or null when the catalogue is
// empty/unusable. `expired: true` means nothing valid was left and this is the
// newest servable build regardless — see the call site for why that is still
// pinned rather than dropped.
function selectServableVersion(catalogue, now) {
  const available = catalogue.getAvailableVersions();
  if (!Array.isArray(available) || available.length === 0) return null;
  const canServe = (version) => {
    try {
      catalogue.getPageContent(version);
      return true;
    } catch (e) {
      return false;
    }
  };
  for (let i = available.length - 1; i >= 0; i--) {
    const version = available[i];
    const expire = versionExpiry(catalogue, version);
    if (expire !== null && expire <= now) continue;
    if (!canServe(version)) continue;
    return { version, expire, expired: false, total: available.length };
  }
  for (let i = available.length - 1; i >= 0; i--) {
    const version = available[i];
    if (canServe(version)) {
      return {
        version,
        expire: versionExpiry(catalogue, version),
        expired: true,
        total: available.length,
      };
    }
  }
  return null;
}

function resolveWhatsappVersion() {
  try {
    if (!waVersion) throw new Error('@wppconnect/wa-version could not be resolved');
    // Deliberately the newest build the installed catalogue can still serve,
    // never a hardcoded one. A fixed version here rots the moment WhatsApp
    // removes that build's assets (HTTP 410), and it only ever gets refreshed
    // when WinZapp itself ships — the same "stale from the day it is set"
    // failure mode that removed the @wppconnect-team/wppconnect dependency pin
    // (see setup_api.py's _PATCHED_DEPENDENCY_KEYS comment block and
    // tests/test_wpp_dependency_not_pinned.py). Keeping up is
    // `npm update @wppconnect/wa-version`, not editing this file.
    const selected = selectServableVersion(waVersion, Date.now());
    if (!selected) return undefined;
    pinnedCatalogueExpired = Boolean(selected.expired);
    if (selected.expired) {
      // Every entry in the catalogue is past its expiry date, so whatever we
      // pin here may already be refused by Meta. We pin anyway, loudly.
      //
      // Not pinning has a known, measured cost and this does not: unpinned,
      // WPPConnect logs "using latest as fallback", WhatsApp Web serves its
      // newest build, and the bundled wa-js may not support it — which showed
      // up as sending to an individual contact failing IN SILENCE (usync
      // queries hanging, isSendFailure with ack 0, REST still answering 200,
      // groups unaffected because they use sender keys). An expired pin fails
      // visibly and recoverably; silent send failure does not. So the choice
      // is: keep pinning, and make the log say exactly what happened and what
      // fixes it, so the next user log names the cause instead of nobody
      // knowing.
      console.error(
        '[WinZapp] ATTENTION: every WhatsApp Web build in @wppconnect/wa-version has ' +
        `EXPIRED (catalogue holds ${selected.total}; newest is ${selected.version}). ` +
        'Meta may already refuse to serve it, and this install cannot know a newer ' +
        'one exists. Pinning it anyway, because running unpinned makes sending to ' +
        'individual contacts fail silently. FIX: npm update @wppconnect/wa-version ' +
        '(or reinstall the API from WinZapp) and restart.'
      );
    } else if (selected.expire !== null
        && selected.expire - Date.now() <= VERSION_EXPIRY_WARNING_MS) {
      console.warn(
        `[WinZapp] The newest usable WhatsApp Web build (${selected.version}) expires on ` +
        `${new Date(selected.expire).toISOString()} and this catalogue has nothing newer. ` +
        'Run: npm update @wppconnect/wa-version (or reinstall the API from WinZapp).'
      );
    }
    const expiryNote = selected.expire === null
      ? 'no expiry recorded'
      : `expires ${new Date(selected.expire).toISOString()}`;
    console.log(
      `[WinZapp] Pinning WhatsApp Web to ${selected.version} ` +
      `(of ${selected.total} available, ${expiryNote})`
    );
    return selected.version;
  } catch (e) {
    console.error(
      '[WinZapp] Could not resolve a WhatsApp Web version via @wppconnect/wa-version ' +
      `(${e && e.message}). Continuing unpinned — WhatsApp Web will serve its newest ` +
      'build, which the bundled wa-js may not support. Run: npm update @wppconnect/wa-version'
    );
    return undefined;
  }
}

// Set by resolveWhatsappVersion() when NOTHING in the catalogue is still valid.
// Read once, by the interception wrapper below, to decide whether to try Meta's
// own current document before falling back to the expired local copy.
let pinnedCatalogueExpired = false;

// How long to wait for Meta's live document before giving up and using the
// expired local build. This runs inside session startup, so it is a budget, not
// a best effort: a user on a dead network must not have pairing held hostage by
// a hanging fetch.
const LIVE_DOCUMENT_FETCH_TIMEOUT_MS = 10000;

// The current WhatsApp Web document, fetched from Meta at session start.
//
// Only ever used when the whole local catalogue has expired. In that state the
// two options used to be: pin a build Meta may already refuse, or run unpinned
// — and unpinned is the one with the measured silent failure (usync hanging,
// isSendFailure with ack 0, REST answering 200; see resolveWhatsappVersion()).
// Fetching the live document is strictly better than both: the page is still
// substituted through our own document-only interception, so WPPConnect never
// installs its blanket one and the backend worker is never starved, and the
// build being served is by definition one Meta still serves.
//
// It is NOT used while the catalogue is healthy. The live build can be ahead of
// what the bundled wa-js supports, which is the same silent-send failure by
// another door; an unexpired pinned build is the known-good pairing. So this is
// the expired branch's fallback, never the default.
//
// wa-version does the request itself (fetchLatestAlpha sends the user-agent,
// language and cache headers Meta expects, which a bare fetch here would get
// wrong). Older copies of the package may not export it — hence the typeof
// guard rather than a call that would throw at startup.
async function fetchLiveWhatsappDocument() {
  if (!waVersion || typeof waVersion.fetchLatestAlpha !== 'function') return null;
  let timer = null;
  try {
    const timeout = new Promise((resolve) => {
      timer = setTimeout(() => resolve(null), LIVE_DOCUMENT_FETCH_TIMEOUT_MS);
    });
    const html = await Promise.race([waVersion.fetchLatestAlpha(), timeout]);
    // Anything can answer an HTTP request — a captive portal, a proxy error
    // page, a consent interstitial. Serving one of those AS the WhatsApp Web
    // document would look like a WhatsApp bug rather than a network one, so
    // require it to actually be the app shell before trusting it.
    if (typeof html !== 'string' || html.length < 1000) return null;
    if (!/web\.whatsapp\.com|WhatsApp/i.test(html)) return null;
    return html;
  } catch (e) {
    return null;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

const whatsappVersion = resolveWhatsappVersion();

// ── Serving the pinned HTML without breaking WhatsApp Web's workers ─────────
//
// WPPConnect serves the pinned build by calling page.setRequestInterception(true)
// (controllers/browser.js, setWhatsappVersion) and answering the one request for
// https://web.whatsapp.com/ with wa-version's HTML. That single call is a blanket
// Fetch.enable over *every* request the target makes — and puppeteer never
// answers the ones a dedicated Worker issues in CORS mode. They do not fail;
// they hang forever, with no error anywhere.
//
// Measured directly, with plain puppeteer and no WhatsApp involved (a blob
// Worker doing one cross-origin fetch):
//
//   setRequestInterception(false):  worker cors ok 200 (560ms)
//   setRequestInterception(true):   worker cors HANG (>10s), no-cors and
//                                   page-issued requests unaffected
//
// What that broke: WhatsApp Web boots a dedicated module worker
// (WAWebBackendWorker) whose init script imports its bundles from
// https://static.whatsapp.net — cross-origin, CORS mode. Those imports hung, so
// the worker never posted ww-init-complete, so startBackendWorker() never
// resolved, so setBackendWorkerBridge() was never called and
// isBackendWorkerBridgeReady() stayed false for the whole session. Because it
// hangs rather than throws, WhatsApp Web's own retry path (3 attempts, keyed off
// the init promise rejecting) never fired either — one silent stall, forever.
//
// And every history-sync chunk handler awaits getBackendWorkerBridge() before
// decoding. So the phone delivered history normally and WhatsApp Web stored it
// unread: chunks parked at 'notification_stored', its own message store holding
// ~2 messages per chat. get-messages was returning everything WhatsApp Web had;
// WhatsApp Web just had almost nothing. That is the "only the last ~15 messages
// ever load" report, all the way down.
//
// Fix: keep the substitution, drop the blanket. A raw-CDP Fetch.enable carrying
// a urlPattern matching only the document (and check-update, which WPPConnect
// aborts) pauses those two URLs and nothing else, so worker traffic is never
// intercepted in the first place. Verified with the same harness: document still
// substituted, worker cors back to ok 200 (606ms).
//
// This is installed by wrapping the controller's exported initWhatsapp — the
// call site in host.layer.js reads the property off the module namespace at call
// time, so replacing it here takes effect. Passing version=undefined onward is
// what keeps WPPConnect from installing its own interception on top of ours.
//
// If you ever see "[WinZapp] wa-version cannot serve <v>" in wppconnect.log
// while "[WinZapp] Pinning WhatsApp Web to <v>" appeared for the same <v> at
// startup, this wrapper is broken and the blanket interception is back: the
// pin resolved fine seconds earlier, so the same lookup cannot legitimately
// fail here.
const WA_WEB_URL = 'https://web.whatsapp.com/';
const WA_CHECK_UPDATE = 'https://web.whatsapp.com/check-update';

async function installPinnedPageInterception(page, body, log) {
  const cdp = await page.createCDPSession();
  // Breadcrumbs for the reload loop reported live (both pairing routes show
  // nothing on screen, wppconnect.log shows "Execution context was destroyed,
  // most likely because of a navigation" every ~10s until the session is force
  // killed at notLogged). Without these there is no way to tell from a user's
  // log whether a reload was re-served the pinned document or escaped the
  // interception entirely and got WhatsApp's current build — which decides
  // whether the bug is in this pattern or upstream of it.
  let documentsServed = 0;
  page.on('framenavigated', (frame) => {
    if (frame === page.mainFrame()) {
      console.log(`[WinZapp] main frame navigated -> ${frame.url()}`);
    }
  });
  // The exact-match urlPattern is deliberate — do NOT widen it.
  //
  // On a fresh unpaired profile WhatsApp Web navigates itself to
  // `https://web.whatsapp.com/?post_logout=1&logout_reason=0`, which this
  // pattern does not match. That looks like a bug (the pin covers the first
  // load and nothing after) and was "fixed" once by matching every Document
  // navigation on the origin — `urlPattern: WA_WEB_URL + '*'` with
  // `resourceType: 'Document'`. That made pairing WORSE, not better: forcing
  // the pinned document onto the post_logout navigation too means the page can
  // never complete the logout/fresh-start cycle it is asking for, so it loops
  // roughly every 10s until WPPConnect force-kills the session at notLogged,
  // and neither the QR nor the pairing code is ever produced.
  //
  // Letting that one navigation through is what allows WhatsApp Web to settle;
  // measured on the real app, the QR then arrives about 12s after
  // start-session. The pin's job is to decide which build BOOTS, not to hold
  // the page hostage to it.
  await cdp.send('Fetch.enable', {
    patterns: [
      { urlPattern: WA_WEB_URL, requestStage: 'Request' },
      { urlPattern: WA_CHECK_UPDATE + '*', requestStage: 'Request' },
    ],
  });
  cdp.on('Fetch.requestPaused', async (event) => {
    const { requestId, request } = event;
    try {
      if (request.url.startsWith(WA_CHECK_UPDATE)) {
        console.log('[WinZapp] check-update aborted by the interception.');
        await cdp.send('Fetch.failRequest', { requestId, errorReason: 'Aborted' });
      } else if (request.url === WA_WEB_URL) {
        documentsServed += 1;
        console.log(
          `[WinZapp] pinned document served (#${documentsServed}) for ${request.url}`
        );
        await cdp.send('Fetch.fulfillRequest', {
          requestId,
          responseCode: 200,
          responseHeaders: [{ name: 'Content-Type', value: 'text/html' }],
          body: Buffer.from(body).toString('base64'),
        });
      } else {
        // Cannot happen with the patterns above, but a paused request that is
        // never answered is exactly the failure mode this whole block exists to
        // remove — so never leave one hanging.
        await cdp.send('Fetch.continueRequest', { requestId });
      }
    } catch (e) {
      // The target can go away mid-flight (navigation, session close); the
      // request dies with it and there is nothing left to answer.
      log?.('verbose', `[WinZapp] Fetch.requestPaused handling failed: ${e && e.message}`);
    }
  });
}

function patchWppconnectVersionPinning() {
  let browserController;
  try {
    const wppEntry = require.resolve('@wppconnect-team/wppconnect/package.json');
    browserController = require(path.join(
      path.dirname(wppEntry), 'dist', 'controllers', 'browser'
    ));
  } catch (e) {
    console.error(
      '[WinZapp] Could not load WPPConnect\'s browser controller to install the ' +
      `narrow request interception (${e && e.message}). WhatsApp Web's backend ` +
      'worker will stall and chats will only ever show their newest messages.'
    );
    return;
  }
  const original = browserController.initWhatsapp;
  if (typeof original !== 'function') return;

  browserController.initWhatsapp = async function (page, token, clear, version, proxy, log) {
    if (version) {
      let body = null;
      let bodyError = null;
      let source = `pinned ${version}`;
      // Expired catalogue only: ask Meta for the document it is serving right
      // now, and pin THAT for the session. See fetchLiveWhatsappDocument() for
      // why this is not the default and why it beats both alternatives here.
      // Awaited before the local read so the fallback below is reached with
      // `body` still null — a failed fetch must cost nothing but the timeout.
      if (pinnedCatalogueExpired) {
        const live = await fetchLiveWhatsappDocument();
        if (live) {
          body = live;
          source = 'live from Meta (local catalogue fully expired)';
          console.warn(
            '[WinZapp] Every build in the local catalogue has expired; serving the ' +
            'document Meta is currently returning instead. This keeps the page pinned ' +
            'and the worker unblocked, but the bundled wa-js may lag that build — ' +
            'run: npm update @wppconnect/wa-version (or reinstall the API from WinZapp).'
          );
        } else {
          console.warn(
            '[WinZapp] Could not fetch the live WhatsApp Web document (offline, blocked, ' +
            `or slower than ${LIVE_DOCUMENT_FETCH_TIMEOUT_MS}ms); falling back to the ` +
            'expired local build, which Meta may already refuse.'
          );
        }
      }
      if (!body) {
        try {
          body = waVersion ? waVersion.getPageContent(version) : null;
          if (!waVersion) bodyError = '@wppconnect/wa-version could not be resolved';
        } catch (e) {
          body = null;
          bodyError = (e && e.message) || String(e);
        }
      }
      if (body) {
        try {
          await installPinnedPageInterception(page, body, log);
          console.log(
            `[WinZapp] Serving WhatsApp Web (${source}) via a document-only ` +
            'interception (worker requests left alone).'
          );
          // Consumed here — WPPConnect must not add its blanket interception.
          version = undefined;
        } catch (e) {
          console.error(
            '[WinZapp] Failed to install the document-only interception ' +
            `(${e && e.message}); falling back to WPPConnect's blanket one. ` +
            'History sync will not work in this session.'
          );
        }
      } else {
        console.error(
          `[WinZapp] wa-version cannot serve ${version} (${bodyError}); leaving the pin ` +
          "to WPPConnect, which installs its blanket request interception. WhatsApp Web's " +
          'backend worker will stall and chats will only ever show their newest messages.'
        );
      }
    }
    return original.call(this, page, token, clear, version, proxy, log);
  };
}

patchWppconnectVersionPinning();

// Mesclagem simples recursiva para webhooks e outros objetos aninhados
const finalConfig = {
  ...configDefault,
  ...customConfig,
  webhook: {
    ...configDefault.webhook,
    ...customConfig.webhook
  },
  log: {
    ...configDefault.log,
    ...customConfig.log
  },
  createOptions: {
    ...(configDefault.createOptions || {}),
    ...(customConfig.createOptions || {}),
    browserArgs: optimizedBrowserArgs,
    // Both of the next three are pinned here on purpose rather than left to
    // whatever upstream's defaults happen to be, because all three decide
    // whether a *visible* browser can ever appear — and every one of them is
    // currently only right by accident:
    //
    //   headless   — wppconnect-server's own config.ts does not set it at all,
    //                so it falls through to createConfig()'s default. That
    //                default is true today; nothing but an upstream bump stands
    //                between that and a Chrome window opening on a blind user's
    //                screen.
    //
    //   useChrome  — false in upstream's config today, and it must stay false.
    //                When true, initBrowser() calls getChrome() to locate the
    //                *system-installed* Chrome and then overwrites
    //                puppeteerOptions.executablePath with it (browser.js ~252),
    //                throwing away the chrome-headless-shell path chosen above.
    //                That substitutes a full GUI-capable Chrome for the shell
    //                with no log line saying so.
    //
    //   puppeteerOptions.executablePath — this is the one that actually
    //                reaches puppeteer. initBrowser() launches with
    //                `{ headless, devtools, args, ...options.puppeteerOptions }`,
    //                so only the nested copy is read; the top-level
    //                createOptions.executablePath below is inert and kept only
    //                because other WPPConnect code paths read it.
    headless: true,
    useChrome: false,
    executablePath: chromeExecutable || undefined,
    puppeteerOptions: {
      ...(configDefault.createOptions?.puppeteerOptions || {}),
      ...(customConfig.createOptions?.puppeteerOptions || {}),
      protocolTimeout: 300000,
      executablePath: chromeExecutable || undefined,
    },
    disableSpins: true,  // Disables command line spinners (saves CPU)
    updatesLog: false,   // Disables checking for updates on startup
    // undefined => WPPConnect pins nothing and uses the live build (see
    // resolveWhatsappVersion above). Set explicitly here because WPPConnect's
    // own default points at a version wa-version no longer ships.
    whatsappVersion,
  }
};

// Inicializa o servidor
initServer(finalConfig);
