"""The page-side call-state poll must survive losing its timer.

Measured live on 2026-09-21 over CDP: the poll was installed while
window.setInterval was still the browser's own (timer id 4), before WhatsApp
swapped in its JSScheduler wrappers, and that timer had silently stopped -- a
non-pausing logpoint on the tick's first line never fired while a fresh
interval ticked normally. No call state reached Python at all, so an outgoing
call the other person rejected kept the call window open and the microphone
capturing until the user hung up by hand.

The fix keeps the poll's closure and lets a Node-side watchdog re-create only
the timer when the tick's heartbeat goes stale. This runs the poll block and
the page-side watchdog function verbatim under Node with a fake clock and a
fake timer table.
"""

import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SESSION_UTIL = ROOT / "client" / "api_patches" / "src" / "util" / "createSessionUtil.ts"


def _node():
    bundled = ROOT / "client" / "node" / "node.exe"
    if bundled.exists():
        return str(bundled)
    return shutil.which("node")


def _strip_types(block: str) -> str:
    block = block.replace("(window as any)", "window")
    block = block.replace("window as any", "window")
    block = re.sub(r"let engineEndProbe:[^=]+=", "let engineEndProbe =", block)
    block = re.sub(r"\((\w+):\s*(?:any|string|number)\)(?::\s*string)?", r"(\1)", block)
    block = re.sub(r"(let \w+):\s*any\s*=", r"\1 =", block)
    return block


def _poll_block() -> str:
    src = SESSION_UTIL.read_text(encoding="utf-8")
    start = src.index("          let lastActiveSignature = '';")
    end_marker = "          armCallStatePoll();\n"
    return _strip_types(src[start:src.index(end_marker, start) + len(end_marker)])


def _revive_function() -> str:
    src = SESSION_UTIL.read_text(encoding="utf-8")
    start = src.index("export function reviveStalledCallStatePoll")
    end = src.index("\n}\n", start) + 3
    return _strip_types(src[start:end].replace("export ", "", 1))


_HARNESS = r"""
let fakeNow = 1_000_000;
Date.now = () => fakeNow;
const timers = new Map();
let nextTimerId = 1;
const window = {
  setInterval(fn) { const id = nextTimerId++; timers.set(id, fn); return id; },
  clearInterval(id) { timers.delete(id); },
};
let activeCall = null;
const stores = [{ get activeCall() { return activeCall; } }];
const WPP = { whatsapp: { CallStore: null, functions: {
  getVoipStackInterface: async () => ({ getCallInfo: async () => '' }),
} } };
const findCall = () => null;
const callIdOf = (c) => c?.id || '';
const callStateOf = (c) => c?.state || '';
const peerJidOf = () => '';
const isIncomingRingingCall = () => false;
const isHistoricalIncomingCall = () => false;
const rememberCall = () => {};
const emitCall = () => {};
const emits = [];
const emitCallState = (event, call, state) => emits.push({ event, state, id: callIdOf(call) });
REVIVE
BLOCK
const settle = () => new Promise((r) => setImmediate(r));
const tickAll = async (ms = 250) => {
  fakeNow += ms;
  for (const fn of [...timers.values()]) fn();
  await settle(); await settle();
};

(async () => {
  const out = {};
  out.installed_timer = timers.has(window.__winzappCallStatePoll);
  out.rearm_exposed = typeof window.__winzappRearmCallStatePoll === 'function';

  // A healthy poll: heartbeat fresh, the watchdog leaves it alone.
  activeCall = { id: 'C1', state: 'CALLING' };
  await tickAll();
  out.saw_calling = emits.some((e) => e.state === 'CALLING');
  out.healthy = reviveStalledCallStatePoll(2000);

  // The timer is lost the way it was live: gone, never cleared by us.
  const lostId = window.__winzappCallStatePoll;
  timers.delete(lostId);
  for (let i = 0; i < 12; i++) await tickAll();       // 3 s with no tick
  out.stale = reviveStalledCallStatePoll(2000);
  out.new_timer_differs = window.__winzappCallStatePoll !== lostId;
  out.single_timer = timers.size === 1;

  // The closure survived: the call it remembered can still end.
  emits.length = 0;
  activeCall = null;
  for (let i = 0; i < 8 && !emits.some((e) => e.event === 'ended'); i++) await tickAll();
  out.ended_after_rearm = emits.filter((e) => e.event === 'ended').map((e) => e.id);

  // Re-arming twice never leaves two timers running.
  window.__winzappRearmCallStatePoll();
  window.__winzappRearmCallStatePoll();
  out.timers_after_double_rearm = timers.size;

  delete window.__winzappRearmCallStatePoll;
  out.absent = reviveStalledCallStatePoll(2000);
  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def outcomes(tmp_path_factory):
    node = _node()
    if not node:
        pytest.skip("no Node runtime available")
    script = tmp_path_factory.mktemp("watchdog") / "watchdog.js"
    script.write_text(
        _HARNESS.replace("REVIVE", _revive_function()).replace("BLOCK", _poll_block()),
        encoding="utf-8",
    )
    run = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_the_poll_is_armed_and_can_be_rearmed(outcomes):
    assert outcomes["installed_timer"] is True
    assert outcomes["rearm_exposed"] is True
    assert outcomes["saw_calling"] is True


def test_a_ticking_poll_is_left_alone(outcomes):
    assert outcomes["healthy"] == "ok"


def test_a_lost_timer_is_recreated(outcomes):
    assert outcomes["stale"] == "rearmed"
    assert outcomes["new_timer_differs"] is True
    assert outcomes["single_timer"] is True


def test_rearming_keeps_the_call_it_was_tracking(outcomes):
    """Only the timer is replaced: the rejected call still ends, so the
    call window closes instead of waiting for the user to hang up."""
    assert outcomes["ended_after_rearm"] == ["C1"]


def test_rearming_never_leaves_two_polls_running(outcomes):
    assert outcomes["timers_after_double_rearm"] == 1


def test_no_listener_means_nothing_to_revive(outcomes):
    assert outcomes["absent"] == "absent"


def test_the_node_side_watchdog_drives_the_revival():
    src = SESSION_UTIL.read_text(encoding="utf-8")
    start = src.index("async onIncomingCallDirect(")
    body = src[start:src.index("\n  }\n", start)]
    assert ".evaluate(reviveStalledCallStatePoll, CALL_STATE_POLL_STALE_MS)" in body
    # one watchdog per client: a re-wire replaces, never stacks
    assert "clearInterval(previousWatchdog)" in body
    assert "page.isClosed?.()" in body
