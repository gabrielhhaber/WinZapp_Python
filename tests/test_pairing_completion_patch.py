"""A pairing the phone accepted ends connected, or with an error the pairing
dialog hears — never a silent wait, and never a new code after acceptance.

macOS report, 2026-11-24: the phone listed the new linked device, wppconnect.log
said "Login with success", "Checking phone is connected...", WhatsApp Web
reloaded, "Pairing code already accepted; not requesting another on reload."
— and then nothing for twenty minutes while the dialog said "Enter the
pairing code".

Most of these tests run wppconnect 2.4.6's own waitForLogin() and the methods
it drives (tests/fixtures/wppconnect_2_4_6_host_layer_login.js, verbatim) with
WinZapp's patches applied, under Node, against a fake page on a virtual clock.
They assert the contract, not the patch text, so a later rework of the reload
guard, the scan wait or the in-chat wait that strands the dialog again fails
here. Each scenario also runs against the build the user had (the v1 patches)
as a control: a test that cannot fail there proves nothing.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

from core import wppconnect_host_layer_patch as host  # noqa: E402
from core import wppconnect_pairing_completion_patch as completion  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "wppconnect_2_4_6_host_layer_login.js"
UPSTREAM = FIXTURE.read_text(encoding="utf-8")


def _patched() -> str:
    notes = []
    content = host._patch_qr_reads(UPSTREAM, notes)
    content = host._patch_post_link_reload(content, notes)
    alarms = [n for n in notes if "DID NOT MATCH" in n and not n.startswith("getQrCode")]
    assert alarms == [], alarms
    return content


def _shipped_5944() -> str:
    """What builds 5944 ran (PRs #886 + #893): today's patches minus the QR
    path's — checkQrCode() as upstream ships it and #893's waitForInChat()."""
    content = _patched().replace(
        completion.PATCHED_WAIT_FOR_IN_CHAT, completion.V1_WAIT_FOR_IN_CHAT, 1
    ).replace(
        completion.PATCHED_CHECK_QR_CODE_AFTER_PROBE,
        completion.ORIGINAL_CHECK_QR_CODE_AFTER_PROBE, 1,
    )
    assert completion.V1_WAIT_FOR_IN_CHAT in content
    assert completion.PATCHED_CHECK_QR_CODE_AFTER_PROBE not in content
    return content


def _shipped_v1() -> str:
    """What build 5928 ran: the scan wait bound and the 2026-11-23 texts."""
    content = host._patch_qr_reads(UPSTREAM, [])
    content = content.replace(host.ORIGINAL_POST_SCAN_AUTH_CHECK, host.PATCHED_POST_SCAN_AUTH_CHECK, 1)
    content = content.replace(host.ORIGINAL_RELOAD_LOGIN_BY_CODE, host.PATCHED_RELOAD_LOGIN_BY_CODE, 1)
    assert host.PATCHED_RELOAD_LOGIN_BY_CODE in content
    return content


# --- the harness -------------------------------------------------------------

_HARNESS = r"""
const scenario = JSON.parse(process.env.WZ_SCENARIO);
let now = 0;
let seq = 0;
const timers = [];
Date.now = () => now;
const sleep = (ms) => new Promise((resolve) => timers.push({ at: now + ms, seq: seq++, resolve }));
const sleep_1 = { sleep };

const world = {
  registered: false, latch: false, stream: 'QR', socket: 'CONNECTED',
  reloading: false, browserGone: false, throwing: false, ref: 1,
  // WhatsApp Web without the Socket/Stream modules (the login-only document,
  // measured 2026-11-25): wa-js's export is undefined. BackendEventBus keeps
  // the socket state there (busSocket; null when it has none).
  noSocket: false, noStream: false, busSocket: null,
};
// Page-side listeners; a reload (a new document) drops them.
let handlers = {};
globalThis.window = {};
globalThis.WPP = {
  on(name, fn) { (handlers[name] = handlers[name] || []).push(fn); },
  conn: {
    isRegistered: () => world.registered,
    isMainReady: () => world.latch,
    // wa-js: null once registered, else the QR payload for the current ref.
    getAuthCode: async () => (world.registered ? null : { fullCode: `2@ref${world.ref}`, type: 'multidevice' }),
  },
  whatsapp: {
    get Stream() { return world.noStream ? undefined : { get mode() { return world.stream; } }; },
    get Socket() { return world.noSocket ? undefined : { get state() { return world.socket; } }; },
    BackendEventBus: { get socketState() { return world.busSocket; } },
  },
};

const page = {
  isClosed: () => false,  // puppeteer kept answering false after the kill
  browser: () => ({ isConnected: () => !world.browserGone }),
  evaluate: async (fn, arg) => {
    await null;
    if (world.browserGone) throw new Error("Attempted to use detached Frame 'F'.");
    if (world.throwing) throw new Error("Attempted to use detached Frame 'F'.");
    if (world.reloading) throw new Error('Execution context was destroyed, most likely because of a navigation.');
    return fn(arg);
  },
  reload: async () => { reload('page.reload()'); },
};
const helpers_1 = { evaluateAndReturn: (p, fn, arg) => p.evaluate(fn, arg) };
const auth_1 = {
  isAuthenticated: (p) => p.evaluate(() => WPP.conn.isRegistered()),
  needsToScan: async (p) => !(await p.evaluate(() => WPP.conn.isRegistered())),
  isInsideChat: (p) => p.evaluate(() => WPP.conn.isRegistered() && WPP.conn.isMainReady()),
  asciiQr: async (code) => `ascii:${code}`,
};
const browser_1 = { InjectionTimeoutError: class extends Error {} };

const out = { statusFind: [], loginByCode: [], reports: [], logs: [], reloads: [], qrs: [] };

class Host {
  constructor() {
    this.isStarted = true;
    this.isLogged = false;
    this.isInChat = false;
    this.isInjected = false;
    this.session = 's';
    this.page = page;
    this.lastLinkCode = null;
    this.attempt = 0;  // wppconnect's own initial values
    this.urlCode = '';
    this.options = {
      phoneNumber: '15550000000',
      deviceSyncTimeout: 0,
      autoClose: 0,
      catchLinkCodeError: (failure) => out.reports.push({ at: now, ...failure }),
    };
    this.statusFind = (status) => out.statusFind.push({ at: now, status });
    // The QR the client shows: wppconnect's create() hands catchQR in.
    this.catchQR = (base64, ascii, attempt, urlCode) => out.qrs.push({ at: now, attempt, urlCode });
    if (scenario.method === 'qr') delete this.options.phoneNumber;
  }
  log(level, message) { out.logs.push(`${now} ${level} ${message && message.message || message}`); }
  getWAVersion() { return Promise.resolve('2.3000.0'); }
  getWAJSVersion() { return Promise.resolve('4.8.2'); }
  startAutoClose() {}
  cancelAutoClose() {}
  tryAutoClose() {}
  isInjectionExpired() { return false; }
  async loginByCode() {
    out.loginByCode.push(now);
    this.lastLinkCode = 'CODE' + out.loginByCode.length;
  }
UPSTREAM_METHODS
}

const client = new Host();
window.checkQrCode = () => client.checkQrCode();

function reload(why) {
  out.reloads.push({ at: now, why });
  world.reloading = true;
  world.latch = false;  // a new document: wa-js starts over
  world.ref += 1;       // and WhatsApp Web mints a new QR ref for it
  handlers = {};
  client.isInjected = false;
  const load = (async () => {
    await sleep(400);
    world.reloading = false;
    client.isInjected = true;
    if (world.registered && world.stream === 'MAIN' && !scenario.latchMissed) world.latch = true;
    client.afterPageScriptInjected().catch((e) => client.log('error', e));
  })();
  client.pageLoadPromise = load;
}

for (const [at, action, value] of scenario.events) {
  timers.push({ at, seq: seq++, resolve: () => {
    if (action === 'reload') reload('WhatsApp Web');
    else if (action === 'rotate') {
      // WhatsApp Web rotates the QR ref; wa-js emits conn.auth_code_change.
      world.ref += 1;
      for (const fn of handlers['conn.auth_code_change'] || []) Promise.resolve().then(() => fn && fn());
    }
    else {
      Object.assign(world, value);
      if (world.registered && world.stream === 'MAIN' && !scenario.latchMissed && !world.reloading) world.latch = true;
    }
  } });
}

// The first page: loaded and injected, the pairing code requested once.
client.isInjected = true;
client.pageLoadPromise = Promise.resolve();
client.afterPageScriptInjected().catch((e) => client.log('error', e));

let result = { state: 'pending' };
client.waitForLogin().then(
  (value) => { result = { state: 'resolved', value, at: now }; },
  (error) => { result = { state: 'rejected', error: String(error && error.message || error), at: now }; },
);

(async () => {
  const settle = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };
  while (result.state === 'pending') {
    await settle();
    if (result.state !== 'pending' || !timers.length) break;
    timers.sort((a, b) => a.at - b.at || a.seq - b.seq);
    const timer = timers.shift();
    if (timer.at > scenario.until) break;
    now = Math.max(now, timer.at);
    timer.resolve();
  }
  await settle();
  console.log(JSON.stringify({ ...out, result, now, isLogged: client.isLogged, isInChat: client.isInChat }));
})();
"""


def _node():
    bundled = ROOT / "client" / "node" / "node.exe"
    if bundled.exists():
        return str(bundled)
    return shutil.which("node")


def _run(source: str, events, until=1_200_000, latch_missed=False, method="code") -> dict:
    node = _node()
    if not node:
        pytest.skip("node not available")
    script = _HARNESS.replace("UPSTREAM_METHODS", source)
    env = dict(os.environ, WZ_SCENARIO=json.dumps(
        {"events": events, "until": until, "latchMissed": latch_missed, "method": method}))
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True,
                          timeout=120, env=env)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _statuses(run):
    return [s["status"] for s in run["statusFind"]]


#: The reported run: code on screen, the phone accepts it 15 s in, WhatsApp
#: Web reloads to finish the login, and the new page's stream reaches MAIN
#: without wa-js latching isMainReady() (the latch is what never came).
ACCEPTED_THEN_RELOAD = [
    [15_000, "set", {"registered": True}],
    [15_500, "reload", None],
    [25_000, "set", {"stream": "MAIN"}],
]


class TestTheReportedPairing:
    def test_shipped_build_waits_forever(self):
        """Control: the build the user had never ends the wait."""
        run = _run(_shipped_v1(), ACCEPTED_THEN_RELOAD, latch_missed=True)
        assert run["result"]["state"] == "pending"
        assert "qrReadSuccess" in _statuses(run)
        assert "inChat" not in _statuses(run)
        assert run["reports"] == []

    def test_patched_reaches_the_chats(self):
        run = _run(_patched(), ACCEPTED_THEN_RELOAD, latch_missed=True)
        assert run["result"] == {"state": "resolved", "value": True, "at": run["result"]["at"]}
        assert run["result"]["at"] < 30_000
        assert _statuses(run)[-2:] == ["qrReadSuccess", "inChat"]
        assert run["reports"] == []

    def test_no_new_code_after_acceptance(self):
        run = _run(_patched(), ACCEPTED_THEN_RELOAD, latch_missed=True)
        assert run["loginByCode"] == [0]


class TestAPageThatNeverOpensTheChats:
    """Accepted and reloaded, but the stream never reaches MAIN."""

    EVENTS = [
        [15_000, "set", {"registered": True}],
        [15_500, "reload", None],
        [16_000, "set", {"stream": "SYNCING"}],
    ]

    def test_shipped_build_waits_forever(self):
        run = _run(_shipped_v1(), self.EVENTS)
        assert run["result"]["state"] == "pending"
        assert run["reports"] == []

    def test_reloads_once_then_carries_on_while_the_socket_is_connected(self):
        run = _run(_patched(), self.EVENTS)
        assert run["result"]["state"] == "resolved" and run["result"]["value"] is True
        assert [r["why"] for r in run["reloads"]] == ["WhatsApp Web", "page.reload()"]
        assert run["result"]["at"] <= 15_000 + completion.PAIRING_GIVE_UP_AFTER_MS + 5_000
        assert run["loginByCode"] == [0]
        assert run["reports"] == []

    def test_reports_to_the_dialog_when_the_socket_is_not_connected(self):
        events = self.EVENTS + [[16_000, "set", {"socket": "OPENING"}]]
        run = _run(_patched(), events)
        assert run["result"]["state"] == "rejected"
        assert run["result"]["error"] == "Phone not connected"
        assert [r["name"] for r in run["reports"]] == ["PairingNotCompleted"]
        assert "phoneNotConnected" in _statuses(run)
        assert run["loginByCode"] == [0]

    def test_a_page_that_recovers_on_the_reload_connects(self):
        events = self.EVENTS + [[15_000 + completion.PAIRING_RELOAD_AFTER_MS + 1_000, "set", {"stream": "MAIN"}]]
        run = _run(_patched(), events)
        assert run["result"]["state"] == "resolved"
        assert "inChat" in _statuses(run)
        assert run["reports"] == []


def _shipped_v2() -> str:
    """What main and 2.1.3.0 ran before #912: the 240 s check read Socket and
    Stream in one evaluate."""
    content = _patched().replace(
        completion.PATCHED_WAIT_FOR_IN_CHAT, completion.V2_WAIT_FOR_IN_CHAT, 1)
    assert completion.V2_WAIT_FOR_IN_CHAT in content
    return content


class TestWithoutTheSocketModule:
    """Issue #912: WhatsApp Web without a Socket module (and, on the login-only
    document, without Stream either). The 240 s check read both in one
    evaluate, so one missing module turned both into "unknown"."""

    EVENTS = TestAPageThatNeverOpensTheChats.EVENTS

    def _events(self, world):
        return self.EVENTS + [[16_000, "set", world]]

    def test_shipped_reports_a_connected_page_as_not_completed(self):
        run = _run(_shipped_v2(), self._events({"noSocket": True, "busSocket": "CONNECTED"}))
        assert [r["name"] for r in run["reports"]] == ["PairingNotCompleted"]
        assert "socket unknown, stream unknown" in run["reports"][0]["message"]

    def test_the_socket_state_comes_from_backend_event_bus(self):
        run = _run(_patched(), self._events({"noSocket": True, "busSocket": "CONNECTED"}))
        assert run["result"]["state"] == "resolved" and run["result"]["value"] is True
        assert run["reports"] == []

    def test_a_syncing_stream_counts_when_no_socket_state_is_readable(self):
        run = _run(_patched(), self._events({"noSocket": True}))
        assert run["result"]["state"] == "resolved" and run["result"]["value"] is True
        assert run["reports"] == []

    def test_a_bus_that_is_not_connected_still_reaches_the_dialog(self):
        run = _run(_patched(), self._events({"noSocket": True, "noStream": True, "busSocket": "OPENING"}))
        assert run["result"]["state"] == "rejected"
        assert [r["name"] for r in run["reports"]] == ["PairingNotCompleted"]
        assert "socket OPENING, stream unknown" in run["reports"][0]["message"]

    def test_nothing_readable_still_reaches_the_dialog(self):
        run = _run(_patched(), self._events({"noSocket": True, "noStream": True}))
        assert run["result"]["state"] == "rejected"
        assert [r["name"] for r in run["reports"]] == ["PairingNotCompleted"]

    def test_a_connected_login_only_page_without_the_login_reaches_the_dialog(self):
        # The login-only document: no Socket, no Stream, its socket CONNECTED
        # for the link exchange, and no stored login. Stuck, not connected.
        run = _run(_patched(), self._events(
            {"noSocket": True, "noStream": True, "busSocket": "CONNECTED", "registered": False}))
        assert run["result"]["state"] == "rejected"
        assert [r["name"] for r in run["reports"]] == ["PairingNotCompleted"]
        assert "socket CONNECTED, stream unknown, registered false" in run["reports"][0]["message"]

    def test_a_syncing_stream_without_the_login_reaches_the_dialog(self):
        run = _run(_patched(), self._events({"noSocket": True, "registered": False}))
        assert run["result"]["state"] == "rejected"
        assert [r["name"] for r in run["reports"]] == ["PairingNotCompleted"]

    def test_the_socket_modules_own_state_needs_no_login_proof(self):
        # Only the fallbacks ask for the login: a page with Socket is the full app.
        run = _run(_patched(), self._events({"registered": False}))
        assert run["result"]["state"] == "resolved" and run["result"]["value"] is True
        assert run["reports"] == []

    def test_a_bus_state_never_overrides_the_socket_modules_own(self):
        run = _run(_patched(), self._events({"socket": "OPENING", "busSocket": "CONNECTED"}))
        assert run["result"]["state"] == "rejected"


class TestDetachedFrameDuringThePairing:
    def test_a_long_probe_outage_with_the_browser_alive_still_logs_in(self):
        """The scan wait gives up after 30 s of failed probes; the login that
        arrives during the outage is still carried on to the chats."""
        events = [
            [10_000, "set", {"throwing": True}],
            [50_000, "set", {"registered": True, "stream": "MAIN"}],
            [70_000, "set", {"throwing": False}],
        ]
        run = _run(_patched(), events)
        assert any("giving up on the scan wait" in line for line in run["logs"])
        assert run["result"]["state"] == "resolved"
        assert "inChat" in _statuses(run)
        assert run["loginByCode"] == [0]

    def test_a_page_that_never_answers_again_reaches_the_dialog(self):
        events = [[10_000, "set", {"throwing": True}]]
        shipped = _run(_shipped_v1(), events)
        assert shipped["reports"] == []  # control: the shipped build said nothing
        run = _run(_patched(), events)
        assert run["result"]["state"] == "rejected"
        assert [r["name"] for r in run["reports"]] == ["PairingNotCompleted"]
        assert "qrReadError" in _statuses(run)

    def test_a_closed_session_stops_at_once(self):
        """The old QR session in the same report, force-closed by the client,
        probed a dead target for 30 s before failing."""
        events = [[10_000, "set", {"browserGone": True}]]
        shipped = _run(_shipped_v1(), events)
        assert shipped["result"]["at"] >= 40_000  # control
        run = _run(_patched(), events)
        assert run["result"]["state"] == "rejected"
        assert run["result"]["at"] < 12_000
        assert run["reports"] == []  # the client closed it; nothing to tell


class TestReloadWithoutAcceptance:
    def test_still_asks_for_a_new_code_when_the_login_never_comes(self):
        """The guard waits for the login, and only a login that never comes
        asks for another code (the reload had another cause)."""
        run = _run(_patched(), [[20_000, "reload", None]], until=200_000)
        assert run["result"]["state"] == "pending"
        assert len(run["loginByCode"]) == 2
        assert run["loginByCode"][1] >= 20_000 + 60_000


# --- the post-link reload, for BOTH pairing methods --------------------------
#
# QR report, build 5944 (2026-11-24), wppconnect.log: "Waiting for QRCode Scan
# (Attempt 1)", "Login with success", "Checking phone is connected...",
# WhatsApp Web's post-link reload, "Waiting for QRCode Scan (Attempt 2)" — a
# NEW QR — and 13 s after the login "Phone not connected", browserClose.
# Nothing reached the pairing dialog.
#
# #886/#893 fixed the reload for the phone code only, and their tests ran the
# phone code only, so the QR path kept the bug. The contract below runs over
# both methods: a fix to one path that leaves the other behind fails here.

LINK_ACCEPTED_AT = 15_000
RELOAD_AT = 17_000
METHODS = ("code", "qr")


def _post_link_reload(restored_at=None):
    """The phone accepts at 15 s ("Login with success"), WhatsApp Web reloads
    at 17 s, and the reloaded page asks to scan while it restores the login
    (needsToScan() true, a fresh QR ref on offer), with WhatsApp's QR-ref
    rotation going on. The login comes back at ``restored_at``, or never."""
    events = [
        [LINK_ACCEPTED_AT, "set", {"registered": True}],
        [RELOAD_AT, "set", {"registered": False}],
        [RELOAD_AT, "reload", None],
        [RELOAD_AT + 3_000, "rotate", None],
        [RELOAD_AT + 23_000, "rotate", None],
        [RELOAD_AT + 43_000, "rotate", None],
    ]
    if restored_at is None:
        events.append([RELOAD_AT, "set", {"socket": "UNPAIRED"}])
    else:
        events.append([restored_at, "set", {"registered": True, "stream": "MAIN"}])
    return events


def _new_code_or_qr_after_acceptance(run, method):
    if method == "code":
        return [at for at in run["loginByCode"] if at >= LINK_ACCEPTED_AT]
    return [q for q in run["qrs"] if q["at"] >= LINK_ACCEPTED_AT]


class TestTheReportedQrPairing:
    def test_shipped_5944_reproduces_the_report(self):
        """Control: the user's exact sequence on the build they had."""
        run = _run(_shipped_5944(), _post_link_reload(restored_at=25_000), method="qr")
        assert run["result"]["state"] == "rejected"
        assert run["result"]["error"] == "Phone not connected"
        assert run["result"]["at"] < RELOAD_AT + 5_000  # seconds, not a bound
        assert _statuses(run)[-2:] == ["qrReadSuccess", "phoneNotConnected"]
        assert [q["attempt"] for q in run["qrs"]] == [1, 2]
        assert run["qrs"][1]["at"] >= RELOAD_AT  # a new QR after acceptance
        assert run["reports"] == []  # and the dialog heard nothing

    def test_patched_waits_out_the_restore_and_connects(self):
        run = _run(_patched(), _post_link_reload(restored_at=25_000), method="qr")
        assert run["result"]["state"] == "resolved" and run["result"]["value"] is True
        assert _statuses(run)[-2:] == ["qrReadSuccess", "inChat"]
        assert [q["attempt"] for q in run["qrs"]] == [1]
        assert run["reports"] == []

    def test_the_in_chat_wait_alone_outlasts_a_transient_logout(self):
        """Defence in depth: should checkQrCode()'s step ever stop matching,
        a transient isLogged = false after the link still does not end
        waitForInChat() with "Phone not connected" (#893's loop did)."""
        without_qr_step = _patched().replace(
            completion.PATCHED_CHECK_QR_CODE_AFTER_PROBE,
            completion.ORIGINAL_CHECK_QR_CODE_AFTER_PROBE, 1,
        )
        run = _run(without_qr_step, _post_link_reload(restored_at=25_000), method="qr")
        assert run["result"]["state"] == "resolved" and run["result"]["value"] is True
        assert "phoneNotConnected" not in _statuses(run)

    def test_a_qr_reload_without_a_link_still_shows_the_new_qr(self):
        """Nothing was accepted: the new page's QR is the one to scan."""
        run = _run(_patched(), [[20_000, "reload", None]], until=60_000, method="qr")
        assert run["result"]["state"] == "pending"
        assert [q["attempt"] for q in run["qrs"]] == [1, 2]
        assert run["qrs"][1]["at"] >= 20_000


@pytest.mark.parametrize("method", METHODS)
class TestPostLinkReloadContract:
    """Whatever the pairing method, a link the phone accepted ends connected
    or with PairingNotCompleted on the dialog — never "Phone not connected" in
    silence — and no new code or QR goes out while the login is restored."""

    @pytest.mark.parametrize("restored_after", [8_000, 50_000])
    def test_login_restored_on_the_reloaded_page_connects(self, method, restored_after):
        run = _run(_patched(), _post_link_reload(restored_at=RELOAD_AT + restored_after),
                   method=method)
        assert run["result"]["state"] == "resolved" and run["result"]["value"] is True
        assert "phoneNotConnected" not in _statuses(run)
        assert _statuses(run)[-2:] == ["qrReadSuccess", "inChat"]
        assert _new_code_or_qr_after_acceptance(run, method) == []
        assert run["reports"] == []

    def test_login_never_restored_is_reported_to_the_dialog(self, method):
        run = _run(_patched(), _post_link_reload(restored_at=None), method=method)
        assert run["result"]["state"] == "rejected"
        assert [r["name"] for r in run["reports"]] == ["PairingNotCompleted"]
        assert run["result"]["at"] <= LINK_ACCEPTED_AT + completion.PAIRING_GIVE_UP_AFTER_MS + 5_000
        assert run["reports"][0]["at"] <= run["result"]["at"]
        assert _new_code_or_qr_after_acceptance(run, method) == []

    def test_both_methods_get_the_same_reload_and_bound(self, method):
        """A QR link is not thrown away sooner than a phone-code one: the
        restore wait leaves the end to waitForInChat(), which reloads once at
        90 s and only then gives up."""
        run = _run(_patched(), _post_link_reload(restored_at=None), method=method)
        assert "page.reload()" in [r["why"] for r in run["reloads"]]
        assert run["reports"][0]["at"] >= LINK_ACCEPTED_AT + completion.PAIRING_GIVE_UP_AFTER_MS

    def test_a_second_reload_during_the_restore_still_connects(self, method):
        events = _post_link_reload(restored_at=RELOAD_AT + 30_000) + [
            [RELOAD_AT + 10_000, "reload", None],
        ]
        run = _run(_patched(), events, method=method)
        assert run["result"]["state"] == "resolved" and run["result"]["value"] is True
        assert _new_code_or_qr_after_acceptance(run, method) == []
        assert run["reports"] == []


# --- the patch itself --------------------------------------------------------

class TestPatchText:
    def test_applies_to_the_upstream_excerpt_and_is_idempotent(self):
        once = _patched()
        notes = []
        assert completion.patch_pairing_completion(once, notes) == once
        assert all("already" in n for n in notes), notes

    def test_upgrades_the_shipped_v1_texts(self):
        notes = []
        out = completion.patch_pairing_completion(_shipped_v1(), notes)
        assert out == _patched()
        assert not any("DID NOT MATCH" in n for n in notes)

    def test_upgrades_the_shipped_v2_in_chat_wait(self):
        """An install patched before #912 gets the per-module state check."""
        notes = []
        out = completion.patch_pairing_completion(_shipped_v2(), notes)
        assert out == _patched()
        assert any(n.startswith("waitForInChat pairing bound: upgraded") for n in notes), notes
        assert not any("DID NOT MATCH" in n for n in notes)

    def test_upgrades_the_shipped_5944_texts(self):
        """An install #893 already patched gets the QR path on top, cleanly."""
        notes = []
        out = completion.patch_pairing_completion(_shipped_5944(), notes)
        assert out == _patched()
        assert any(n.startswith("waitForInChat pairing bound: upgraded") for n in notes), notes
        assert any(n.startswith("checkQrCode wait after a QR link: patched") for n in notes), notes
        assert not any("DID NOT MATCH" in n for n in notes)

    def test_upgrades_the_first_cut_including_its_nested_guard(self):
        """PR #886's first cut (builds up to 5915), then the v1 patcher run
        over it after an update — which wrapped the guard's inner call."""
        first_cut = _shipped_v1().replace(
            completion.V1_RELOAD_GUARD, completion.V0_NESTED_RELOAD_GUARD, 1
        ).replace(completion.V1_POST_SCAN_CHECK, completion.V0_POST_SCAN_CHECK, 1)
        assert completion.V0_NESTED_RELOAD_GUARD in first_cut
        notes = []
        out = completion.patch_pairing_completion(first_cut, notes)
        assert out == _patched()
        assert "__wzCodeAccepted" not in out
        assert out.count("Pairing code already accepted") == 1

    def test_a_runtime_without_these_functions_is_left_alone(self):
        notes = []
        assert completion.patch_pairing_completion("nothing here", notes) == "nothing here"
        assert not any("DID NOT MATCH" in n for n in notes)

    def test_moved_text_is_reported_not_guessed(self):
        src = "async waitForLogin() {}\nasync waitForInChat() {}\n"
        notes = []
        assert completion.patch_pairing_completion(src, notes) == src
        assert sum("DID NOT MATCH" in n for n in notes) == 4
        # checkQrCode()'s wait rides on the in-chat bound, whose alarm is above.
        assert any(n.startswith("checkQrCode wait after a QR link: left untouched — it needs") for n in notes), notes

    def test_a_2_3_runtime_keeps_upstream_check_qr_code(self):
        """2.3.x has checkQrCode()'s lines but no bounded waitForInChat()."""
        src = "async waitForLogin() {}\n" + completion.ORIGINAL_CHECK_QR_CODE_AFTER_PROBE
        notes = []
        assert completion.patch_pairing_completion(src, notes) == src
        assert "checkQrCode wait after a QR link: not in this file — nothing to patch." in notes

    def test_the_qr_link_wait_needs_the_bounded_in_chat_wait(self):
        """A waitForInChat() whose text moved leaves checkQrCode() upstream's:
        without the bound and wzPairingSettled the wait would swallow every
        later QR request in the run."""
        moved = UPSTREAM.replace(
            completion.ORIGINAL_WAIT_FOR_IN_CHAT,
            completion.ORIGINAL_WAIT_FOR_IN_CHAT.replace("return this.isInChat;", "return !!this.isInChat;"),
            1,
        )
        assert moved != UPSTREAM
        notes = []
        out = completion.patch_pairing_completion(moved, notes)
        assert completion.PATCHED_CHECK_QR_CODE_AFTER_PROBE not in out
        assert completion.ORIGINAL_CHECK_QR_CODE_AFTER_PROBE in out
        assert "wzPairingSettled" not in out
        assert "waitForInChat pairing bound: DID NOT MATCH the known source text — left untouched." in notes
        assert any(n.startswith("checkQrCode wait after a QR link: left untouched — it needs") for n in notes), notes

    def test_a_patched_managed_host_layer_stays_ok_on_every_launch(self):
        """The full host.layer.js patch runs on every launch: once part 6 sits
        inside 2.3.5+'s checkQrCode(), its auth-probe check must still
        recognise the function, or every launch raises DID NOT MATCH."""
        from tests.test_pairing_code_patch import _managed_source

        for head in (host.MANAGED_V233_CHECK_QR_CODE, host.MANAGED_V235_CHECK_QR_CODE):
            content = _managed_source(
                checkqrcode_text=head
                + completion.ORIGINAL_CHECK_IN_CHAT_PROBE
                + completion.ORIGINAL_WAIT_FOR_IN_CHAT
            )
            first, notes, ok = host.patch_host_layer_source(content)
            assert ok, notes
            assert completion.PATCHED_CHECK_QR_CODE_AFTER_PROBE in first
            for _launch in range(2):
                again, notes, ok = host.patch_host_layer_source(first)
                assert ok, notes
                assert again == first
                assert any("no patch needed" in n for n in notes), notes

    def test_the_patched_excerpt_parses(self):
        node = _node()
        if not node:
            pytest.skip("node not available")
        subprocess.run([node, "-e", "class X {\n" + _patched() + "\n}"],
                       check=True, capture_output=True, timeout=60)

    def test_the_bound_applies_only_to_a_link_made_in_this_run(self):
        patched = completion.PATCHED_WAIT_FOR_IN_CHAT
        assert "this.wzLinkedThisRun === true" in patched
        assert "!(this.options.deviceSyncTimeout > 0)" in patched
        assert "this.wzLinkedThisRun = true;" in completion.PATCHED_POST_SCAN_CHECK
        qr_wait = completion.PATCHED_CHECK_QR_CODE_AFTER_PROBE
        assert "this.wzLinkedThisRun === true" in qr_wait
        assert "!(this.options.deviceSyncTimeout > 0)" in qr_wait
        assert "wzReportPairingNotCompleted" not in qr_wait  # waitForInChat()'s to report

    def test_the_live_host_layer_is_patched_and_stays_idempotent(self):
        path = ROOT.joinpath("client", "api", "node_modules", "@wppconnect-team",
                             "wppconnect", "dist", "api", "layers", "host.layer.js")
        if not path.exists():
            pytest.skip("client/api is not set up")
        live, notes, ok = host.patch_host_layer_source(path.read_text(encoding="utf-8"))
        assert ok, notes
        again, notes, ok = host.patch_host_layer_source(live)
        assert ok, notes  # the next launch, over the file this one wrote
        assert again == live
