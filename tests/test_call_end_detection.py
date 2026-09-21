"""How fast WinZapp learns that a call is over.

Measured live on 2026-09-21 with a read-only recorder over CDP: when the other
person rejects, WhatsApp removes CallStore.activeCall at once, but leaves no
terminal state behind (the last state seen was PREACCEPT_RECEIVED, and the
call was already absent from the collection). The page poll could not tell
that apart from WhatsApp briefly swapping activeCall mid-call, so it waited a
full 5 s of absence before telling Python -- the call window stayed up after
WhatsApp had already played its end tone, and the user hung up by hand.

The VoIP engine answers the question directly: getCallInfo() is an empty
string once no call is ongoing (verified live), JSON naming the call
otherwise. This runs the page poll verbatim under Node with a fake clock,
CallStore and engine.
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


def _poll_block() -> str:
    src = SESSION_UTIL.read_text(encoding="utf-8")
    start = src.index("          let lastActiveSignature = '';")
    end_marker = "          armCallStatePoll();\n"
    block = src[start:src.index(end_marker, start) + len(end_marker)]
    block = block.replace("(window as any)", "window")
    block = re.sub(r"let engineEndProbe:[^=]+=", "let engineEndProbe =", block)
    block = re.sub(r"\((\w+):\s*(?:any|string)\)", r"(\1)", block)
    block = re.sub(r"(let \w+):\s*any\s*=", r"\1 =", block)
    return block


_HARNESS = r"""
let fakeNow = 1_000_000;
Date.now = () => fakeNow;
let pollFn = null;
const window = { setInterval(fn) { pollFn = fn; return 1; }, clearInterval() {} };
let activeCall = null;
const stores = [{ get activeCall() { return activeCall; } }];
let engineInfo = '';
let engineAvailable = true;
const WPP = {
  whatsapp: {
    CallStore: null,
    functions: {
      get getVoipStackInterface() {
        return engineAvailable ? async () => ({ getCallInfo: async () => engineInfo }) : undefined;
      },
    },
  },
};
const findCall = () => null;
const callIdOf = (c) => c?.id || '';
const callStateOf = (c) => c?.state || '';
const peerJidOf = () => '';
const isIncomingRingingCall = () => false;
const isHistoricalIncomingCall = () => false;
const rememberCall = () => {};
const emitCall = () => {};
const emits = [];
const emitCallState = (event, call, state) => emits.push({ event, state, at: fakeNow });
BLOCK
const settle = () => new Promise((r) => setImmediate(r));
const step = async (ms = 250) => { fakeNow += ms; pollFn(); await settle(); await settle(); };
const endedAfter = (since) => {
  const e = emits.find((x) => x.event === 'ended');
  return e ? e.at - since : null;
};

(async () => {
  const out = {};

  // 1. rejected / ended: the engine says "no call" -> ended within a poll or two
  activeCall = { id: 'C1', state: 'PREACCEPT_RECEIVED' };
  await step(); emits.length = 0;
  activeCall = null; engineInfo = '';
  const gone1 = fakeNow;
  for (let i = 0; i < 30 && !emits.some((e) => e.event === 'ended'); i++) await step();
  out.rejected_ms = endedAfter(gone1);

  // 2. WhatsApp swapping activeCall mid-call: the engine still holds THIS call
  emits.length = 0;
  activeCall = { id: 'C2', state: 'ACTIVE' };
  await step(); emits.length = 0;
  activeCall = null; engineInfo = JSON.stringify({ call_id: 'C2', call_ending: false });
  for (let i = 0; i < 8; i++) await step();             // 2 s of absence
  out.transient_ended_early = emits.some((e) => e.event === 'ended');
  activeCall = { id: 'C2', state: 'ACTIVE' };             // it comes back
  for (let i = 0; i < 30; i++) await step();
  out.transient_ever_ended = emits.some((e) => e.event === 'ended');

  // 3. the engine names a DIFFERENT call -> this one is over
  emits.length = 0;
  activeCall = { id: 'C3', state: 'CALLING' };
  await step(); emits.length = 0;
  activeCall = null; engineInfo = JSON.stringify({ call_id: 'OTHER' });
  const gone3 = fakeNow;
  for (let i = 0; i < 30 && !emits.some((e) => e.event === 'ended'); i++) await step();
  out.other_call_ms = endedAfter(gone3);

  // 4. engine unavailable -> the original 5 s grace still protects
  emits.length = 0;
  activeCall = { id: 'C4', state: 'ACTIVE' };
  await step(); emits.length = 0;
  engineAvailable = false; activeCall = null;
  const gone4 = fakeNow;
  for (let i = 0; i < 40 && !emits.some((e) => e.event === 'ended'); i++) await step();
  out.no_engine_ms = endedAfter(gone4);

  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def outcomes(tmp_path_factory):
    node = _node()
    if not node:
        pytest.skip("no Node runtime available")
    script = tmp_path_factory.mktemp("end") / "end.js"
    script.write_text(_HARNESS.replace("BLOCK", _poll_block()), encoding="utf-8")
    run = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_a_rejected_call_ends_within_a_second_not_after_five(outcomes):
    assert outcomes["rejected_ms"] is not None
    assert outcomes["rejected_ms"] <= 1000


def test_a_mid_call_activecall_swap_still_never_ends_the_call(outcomes):
    """The reason the grace exists: WhatsApp briefly replacing activeCall
    while the engine still holds the same call must not tear it down."""
    assert outcomes["transient_ended_early"] is False
    assert outcomes["transient_ever_ended"] is False


def test_an_engine_naming_another_call_ends_this_one_at_once(outcomes):
    assert outcomes["other_call_ms"] is not None
    assert outcomes["other_call_ms"] <= 1000


def test_without_the_engine_the_original_grace_still_applies(outcomes):
    assert 5000 <= outcomes["no_engine_ms"] <= 5500
