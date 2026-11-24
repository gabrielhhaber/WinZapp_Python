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
  reloading: false, browserGone: false, throwing: false,
};
globalThis.window = {};
globalThis.WPP = {
  on() {},
  conn: {
    isRegistered: () => world.registered,
    isMainReady: () => world.latch,
  },
  whatsapp: {
    Stream: { get mode() { return world.stream; } },
    Socket: { get state() { return world.socket; } },
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
};
const browser_1 = { InjectionTimeoutError: class extends Error {} };

const out = { statusFind: [], loginByCode: [], reports: [], logs: [], reloads: [] };

class Host {
  constructor() {
    this.isStarted = true;
    this.isLogged = false;
    this.isInChat = false;
    this.isInjected = false;
    this.session = 's';
    this.page = page;
    this.lastLinkCode = null;
    this.options = {
      phoneNumber: '15550000000',
      deviceSyncTimeout: 0,
      autoClose: 0,
      catchLinkCodeError: (failure) => out.reports.push({ at: now, ...failure }),
    };
    this.statusFind = (status) => out.statusFind.push({ at: now, status });
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

function reload(why) {
  out.reloads.push({ at: now, why });
  world.reloading = true;
  world.latch = false;  // a new document: wa-js starts over
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


def _run(source: str, events, until=1_200_000, latch_missed=False) -> dict:
    node = _node()
    if not node:
        pytest.skip("node not available")
    script = _HARNESS.replace("UPSTREAM_METHODS", source)
    env = dict(os.environ, WZ_SCENARIO=json.dumps(
        {"events": events, "until": until, "latchMissed": latch_missed}))
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

    def test_the_live_host_layer_is_patched_and_stays_idempotent(self):
        path = ROOT.joinpath("client", "api", "node_modules", "@wppconnect-team",
                             "wppconnect", "dist", "api", "layers", "host.layer.js")
        if not path.exists():
            pytest.skip("client/api is not set up")
        live, notes, ok = host.patch_host_layer_source(path.read_text(encoding="utf-8"))
        assert ok, notes
        assert host.patch_host_layer_source(live)[0] == live
