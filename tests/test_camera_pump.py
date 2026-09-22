"""The page's steady camera pump, run on the REAL code under Node.

A canvas captureStream() only emits a frame when the canvas is PAINTED. Left
to the desktop's camera frames alone, the synthetic video track went idle
whenever they stopped or stuttered, WebRTC reported it muted, and WhatsApp Web
reacted on its own. Measured live on 2026-09-21: the peer saw the image
flicker at the start of a video call (seconds of idle between the offer and
the first frame, then socket jitter), and after "turn video off" painted black
once, WhatsApp re-requested media with video=false and never asked for video
again -- turning video back on left the peer on black for the rest of the call.

The pump repaints the canvas at a steady rate while a page call is live: the
last real frame while video is on, black while it is off. This extracts that
block verbatim from the patched source, strips the TypeScript annotations and
drives it with a fake canvas and a manual clock. Skipped without Node, the
same way test_call_end_chime_policy.py is.
"""

import json
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


def _pump_block() -> str:
    src = BRIDGE.read_text(encoding="utf-8")
    start = src.index("  const blankCameraCanvas = () => {")
    end_marker = "    state.cameraPump = win.setInterval(pumpCamera, CAMERA_PUMP_MS);\n  };\n"
    block = src[start:src.index(end_marker, start) + len(end_marker)]
    block = re.sub(r"(\w+)\??:\s*(?:number|boolean)\b", r"\1", block)
    block = re.sub(r":\s*(?:RTCRtpSender\[\]|string\[\])", "", block)
    block = re.sub(r"\((\w+):\s*any\)", r"(\1)", block)
    block = block.replace(" as RTCPeerConnection[]", "")
    block = block.replace("new Set<string>()", "new Set()")
    return block


_HARNESS = r"""
const paints = [];
const context = {
  fillStyle: '',
  fillRect() { paints.push('black'); },
  drawImage(picture) { paints.push(picture.name); },
};
const canvas = { width: 640, height: 360, getContext: () => context };
let intervalFn = null;
const win = {
  setInterval(fn) { intervalFn = fn; return 1; },
  clearInterval() { intervalFn = null; },
};
let callLive = true;
const isLivePageCall = () => callLive;
const currentPageCall = () => ({});
const report = () => {};
const state = {
  cameraCanvas: canvas, cameraGeneration: 0, cameraPending: false,
  cameraStoppedEpoch: -1, cameraLastPicture: null, cameraShowing: false,
  cameraPump: 0, cameraPumpIdleTicks: 0,
};
BLOCK
const tick = (n = 1) => { for (let i = 0; i < n; i++) if (intervalFn) intervalFn(); };
const out = {};

// WhatsApp takes the track: the pump starts before any camera frame exists.
ensureCameraPump();
paints.length = 0; tick(5);
out.before_first_frame = paints.slice();

// A real frame is drawn (what pushCameraFrame's onload does), then the desktop
// stutters: the pump keeps repainting that frame instead of going idle.
state.cameraLastPicture = { name: 'frame-A' }; state.cameraShowing = true;
paints.length = 0; tick(5);
out.during_stutter = paints.slice();

// "Turn video off": black, and it KEEPS being painted, so the track never idles.
state.stopCamera(1000);
paints.length = 0; tick(30);
out.after_stop = paints.slice();

// "Turn video on": the first new frame is drawn and the pump shows it.
state.cameraLastPicture = { name: 'frame-B' }; state.cameraShowing = true;
paints.length = 0; tick(3);
out.after_restart = paints.slice();

// The call ends: no painting while the page has no live call, and the pump
// stops itself after its idle budget instead of running forever.
callLive = false;
paints.length = 0; tick(49);
out.pump_alive_before_budget = intervalFn !== null;
tick(1);
out.pump_alive_after_budget = intervalFn !== null;
out.painted_after_call = paints.length;

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def outcomes(tmp_path_factory):
    node = _node()
    if not node:
        pytest.skip("no Node runtime available to execute the camera pump")
    script = tmp_path_factory.mktemp("pump") / "pump.js"
    script.write_text(_HARNESS.replace("BLOCK", _pump_block()), encoding="utf-8")
    run = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_the_track_is_fed_before_the_first_camera_frame(outcomes):
    """The seconds between the offer and the first desktop frame used to leave
    the track idle -- the flicker at the start of the call."""
    assert outcomes["before_first_frame"] == ["black"] * 5


def test_a_desktop_stutter_never_leaves_the_track_idle(outcomes):
    assert outcomes["during_stutter"] == ["frame-A"] * 5


def test_turning_video_off_keeps_painting_black(outcomes):
    """Black is painted on EVERY tick, not once: a single blank followed by
    silence is what made WhatsApp drop the video for good."""
    assert outcomes["after_stop"] == ["black"] * 30


def test_turning_video_back_on_shows_the_new_frame(outcomes):
    assert outcomes["after_restart"] == ["frame-B"] * 3


def test_the_pump_stops_itself_once_no_call_is_live(outcomes):
    assert outcomes["painted_after_call"] == 0
    assert outcomes["pump_alive_before_budget"] is True
    assert outcomes["pump_alive_after_budget"] is False
