"""Behaviour of the page's call-end-chime exemption, run on the REAL code.

callMediaBridge.ts mutes WhatsApp Web's own page audio (WinZapp plays its own
sounds) and opens a 2500 ms exemption only when a call that was actually
CONNECTED ends, so the genuine call-ended chime stays audible while a
coincident message-notification ping does not.

What decides "actually connected" used to be state.enabled, which was wrong
three ways at once: offerCall() enables the bridge before the offer is sent
(so an unanswered OUTGOING call opened the window), the Linux/PulseAudio path
never calls enable() (so no call ever opened it there), and reset() cleared
the flag the poll still needed when it observed ENDED after a slow teardown.

Rather than assert on strings, this extracts the policy block verbatim from
the patched source, strips the TypeScript annotations, and executes it under
Node against a simulated CallStore. Skipped when no Node is available, the
same way test_api_patches_in_sync skips without a client/api/ checkout.
"""

import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "client" / "api_patches" / "src" / "util" / "callMediaBridge.ts"


def _node():
    bundled = ROOT / "client" / "node" / "node.exe"
    if bundled.exists():
        return str(bundled)
    return shutil.which("node")


def _policy_block() -> tuple[str, str]:
    src = BRIDGE.read_text(encoding="utf-8")
    start = src.index("  let callWasActive = false;")
    end_marker = "    lastPageCallKey = key;\n  };\n"
    block = src[start:src.index(end_marker, start) + len(end_marker)]
    reset_line = re.search(
        r"^\s*if \(lastCallWasAnswered\(\)\) allowCallEndChime\(\);\s*$", src, re.M
    )
    assert reset_line, "reset() no longer arms the chime through lastCallWasAnswered()"
    block = re.sub(r":\s*Record<string,\s*string>", "", block)
    block = re.sub(r"let answeredPageCallKey:\s*string\s*\|\s*null", "let answeredPageCallKey", block)
    block = re.sub(r"\((\w+):\s*(?:any|HTMLMediaElement)\)", r"(\1)", block)
    block = re.sub(r"\):\s*(?:string|boolean|any)\s*=>", ") =>", block)
    return block, reset_line.group(0).strip()


_SCENARIOS = r"""
const chimeOpen = () => now <= allowCallEndChimeUntil;
const call = (id, st) => ({ id, getState: () => st });
const tick = (c) => { store.call = c; refreshCallAudioPolicy(); now += 250; };
const reset = () => { RESET_LINE state.enabled = false; };
const results = {};
const fresh = () => { now = 0; store.call = null; state.enabled = false;
  callWasActive = false; lastPageCallKey = ''; answeredPageCallKey = null;
  allowCallEndChimeUntil = -1; };

fresh(); state.enabled = true;
tick(call('o1', 'CALLING')); tick(call('o1', 'CALLING')); reset(); tick(null);
results.outgoing_unanswered = chimeOpen();

fresh(); state.enabled = true;
tick(call('o2', 'CALLING')); tick(call('o2', 'ACTIVE')); reset();
results.outgoing_answered_local_hangup = chimeOpen();

fresh();
tick(call('i1', 'INCOMING_RING')); tick(call('i1', 'INCOMING_RING')); reset(); tick(null);
results.incoming_rejected_while_ringing = chimeOpen();

fresh();
tick(call('i2', 'INCOMING_RING')); state.enabled = true; tick(call('i2', 'ACTIVE')); tick(null);
results.incoming_answered_remote_hangup = chimeOpen();

fresh(); state.enabled = true;
tick(call('s1', 'ACTIVE')); reset(); now += 3000;
results.slow_teardown_after_reset_window = chimeOpen();
tick(null);
results.slow_teardown_late_ended = chimeOpen();

fresh();
tick(call('l1', 'INCOMING_RING')); tick(call('l1', 'ACTIVE')); tick(null);
results.linux_answered_without_enable = chimeOpen();

fresh(); state.enabled = true;
tick(call('r1', 'ACTIVE')); reset(); tick(call('r2', 'CALLING'));
now += 3000; tick(call('r2', 'CALLING')); tick(null);
results.redial_second_call_cancelled = chimeOpen();

fresh(); tick(call('m1', 'ACTIVE')); now += 3000; tick(call('m1', 'ACTIVE'));
results.ongoing_call_no_spurious_window = chimeOpen();

console.log(JSON.stringify(results));
"""


@pytest.fixture(scope="module")
def outcomes(tmp_path_factory):
    node = _node()
    if not node:
        pytest.skip("no Node runtime available to execute the bridge policy")
    block, reset_line = _policy_block()
    js = (
        "let now = 0;\n"
        "const store = { call: null };\n"
        "const win = { performance: { now: () => now }, require: () => ({ activeCall: store.call }) };\n"
        "const state = { enabled: false };\n"
        "const pageAudioState = new WeakMap();\n"
        "const mutedPageElements = new Set();\n"
        + block
        + _SCENARIOS.replace("RESET_LINE", reset_line)
    )
    script = tmp_path_factory.mktemp("chime") / "policy.js"
    script.write_text(js, encoding="utf-8")
    run = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    import json
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_an_unanswered_outgoing_call_opens_no_window(outcomes):
    """REGRESSION: offerCall() enables the bridge before the offer is sent, so
    with state.enabled as the signal an outgoing call counted as answered
    while still ringing, and cancelling it let a message ping through."""
    assert outcomes["outgoing_unanswered"] is False


def test_the_chime_is_allowed_on_the_linux_path_too(outcomes):
    """REGRESSION: setCallMediaBridgeActive() returns before enable() on the
    Linux/PulseAudio path, so no call was ever "answered" there and the real
    terminal chime was muted on every remote-API install."""
    assert outcomes["linux_answered_without_enable"] is True


def test_an_ended_observed_after_a_slow_teardown_still_rearms_the_chime(outcomes):
    """REGRESSION: reset() cleared the answered flag, so when the poll only saw
    ENDED after the reset window had expired, the genuine chime was muted."""
    assert outcomes["slow_teardown_after_reset_window"] is False
    assert outcomes["slow_teardown_late_ended"] is True


def test_what_already_worked_still_works(outcomes):
    """The cases the previous code got right must be unchanged."""
    assert outcomes["outgoing_answered_local_hangup"] is True
    assert outcomes["incoming_rejected_while_ringing"] is False
    assert outcomes["incoming_answered_remote_hangup"] is True
    assert outcomes["ongoing_call_no_spurious_window"] is False


def test_a_redial_does_not_inherit_the_previous_calls_answer(outcomes):
    """The answered record is keyed by call id so it can survive reset()
    without leaking: call 1 answered, then call 2 dialled with no idle poll in
    between and cancelled unanswered, must not open a window of its own."""
    assert outcomes["redial_second_call_cancelled"] is False
