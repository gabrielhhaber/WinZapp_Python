"""The other person's video is taken from WhatsApp's own renderer registry.

WhatsApp Web never exposes the peer's video as a MediaStreamTrack or as a
<video> the page can read. Measured live on 2026-09-21 over CDP during a video
call with a phone: the WASM engine reported "Decoding 640x432 (H264) @ 19 fps",
yet no track, <video>, VideoDecoder or canvas draw on the page carried it.
Its frames go only to canvases registered with WAWebVoipVideoRendererRegistry,
which is what WhatsApp's own call UI does for the peer tile. Registering a
canvas the same way made the engine paint the peer into it (live, changing
pixels), and feeding that canvas into __winzappOnCallRemoteVideo put the peer
in WinZapp's call window for the first time.

This runs syncPeerVideo()/stopPeerVideo() verbatim under Node against a fake
registry, a fake CallStore and a manual clock.
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


def _block() -> str:
    src = BRIDGE.read_text(encoding="utf-8")
    start = src.index("  const PEER_VIDEO_CAPTURE_MS = ")
    block = src[start:src.index("  const scanMediaElements = () => {", start)]
    block = re.sub(r" as (?:HTMLCanvasElement \| null|any)", "", block)
    block = re.sub(r"\(\):\s*any\s*=>", "() =>", block)
    block = re.sub(r"\((\w+):\s*any\)", r"(\1)", block)
    return block


_HARNESS = r"""
const log = [];
let rendered = false;
const registry = {
  registerVideoCanvas(canvas, portal) { log.push(['register', portal]); },
  assignSourceToCanvas({ canvas, mirror, source }) { log.push(['assign', source.peer, source.stream, mirror]); },
  unassignSourceFromCanvas(source, canvas) { log.push(['unassign', source.peer]); },
  unregisterVideoCanvas(canvas) { log.push(['unregister']); },
  hasRenderedFirstFrameForCanvas() { return rendered; },
};
const renderSource = {
  WAWebVoipVideoRenderSource: { peer: (peer, stream) => ({ peer, stream }) },
  WAWebVoipVideoRenderStream: { CAMERA: 'CAMERA' },
};
let timers = new Map(); let nextTimer = 1;
const win = {
  require(name) {
    if (name === 'WAWebVoipVideoRendererRegistry') return { videoRendererRegistry: registry };
    if (name === 'WAWebVoipVideoRenderSource') return renderSource;
    return null;
  },
  setInterval(fn) { const id = nextTimer++; timers.set(id, fn); return id; },
  clearInterval(id) { timers.delete(id); },
  __winzappOnCallRemoteVideo(jpeg) { log.push(['frame', jpeg]); return Promise.resolve(); },
};
const document = {
  createElement() {
    return {
      width: 0, height: 0,
      getContext: () => ({ drawImage() {} }),
      toDataURL: () => 'data:image/jpeg;base64,FRAME',
    };
  },
};
let call = null;
const currentPageCall = () => call;
const isLivePageCall = (c) => !!c && c.state === 'ACTIVE';
const pageCallKey = (c) => String(c?.id || '');
const report = () => {};
const state = { remoteVideoFramesSent: 0 };
BLOCK
const tickTimers = (n) => { for (let i = 0; i < n; i++) for (const fn of timers.values()) fn(); };
const out = {};

// voice call: nothing registered
call = { id: 'voice-1', state: 'ACTIVE', isVideo: false, peerJid: 'peer-A' };
syncPeerVideo(); syncPeerVideo();
out.voice = log.slice(); log.length = 0;

// video call: registered once, however often the 250 ms scan runs
call = { id: 'video-1', state: 'ACTIVE', isVideo: true, peerJid: 'peer-A' };
for (let i = 0; i < 10; i++) syncPeerVideo();
out.registration = log.slice(); log.length = 0;

// before the engine paints anything, no frame is sent
tickTimers(5);
out.before_first_paint = log.filter((e) => e[0] === 'frame').length;

// once painted, frames flow through the call window's callback
rendered = true; tickTimers(3);
out.frames = log.filter((e) => e[0] === 'frame').map((e) => e[1]);
log.length = 0;

// call ends: the canvas is released and capture stops
call = null; syncPeerVideo();
out.teardown = log.slice(); log.length = 0;
out.timers_left = timers.size;

// a new video call registers a fresh canvas for the new peer
call = { id: 'video-2', state: 'ACTIVE', isVideo: true, peerJid: 'peer-B' };
syncPeerVideo();
out.next_call = log.slice();

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def outcomes(tmp_path_factory):
    node = _node()
    if not node:
        pytest.skip("no Node runtime available")
    script = tmp_path_factory.mktemp("peer") / "peer.js"
    script.write_text(_HARNESS.replace("BLOCK", _block()), encoding="utf-8")
    run = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_a_voice_call_registers_nothing(outcomes):
    assert outcomes["voice"] == []


def test_a_video_call_registers_one_peer_canvas_like_whatsapps_own_ui(outcomes):
    assert outcomes["registration"] == [
        ["register", False],
        ["assign", "peer-A", "CAMERA", False],
    ]


def test_no_frame_is_sent_before_the_engine_paints_one(outcomes):
    assert outcomes["before_first_paint"] == 0


def test_painted_frames_reach_the_call_windows_callback(outcomes):
    assert outcomes["frames"] == ["FRAME"] * 3


def test_the_canvas_is_released_when_the_call_ends(outcomes):
    assert outcomes["teardown"] == [["unassign", "peer-A"], ["unregister"]]
    assert outcomes["timers_left"] == 0


def test_the_next_call_gets_its_own_canvas(outcomes):
    assert outcomes["next_call"] == [
        ["register", False],
        ["assign", "peer-B", "CAMERA", False],
    ]
