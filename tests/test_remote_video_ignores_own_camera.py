"""The remote-video extraction must never show the user their own camera.

WhatsApp Web plays the camera track WinZapp hands it in hidden <video>
elements to feed its encoder, and the bridge's media scan picks up every
<video> carrying a stream as "remote video". Confirmed live on 2026-09-21
(a sighted-assistance description of the call window): WinZapp's call
window showed the user's OWN image instead of the other person's. Two WinZapp
users calling each other each saw themselves -- or black with the camera off
-- while the video they sent arrived intact on a phone. The microphone had
this guard all along (localTrackIds in attachRemoteTrack); video never did.

The ownership check and the clone hook are run verbatim under Node; the
guard's placement in attachRemoteVideo is checked on the source.
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


def test_attach_remote_video_refuses_our_camera_before_attaching_anything():
    src = BRIDGE.read_text(encoding="utf-8")
    body = src[src.index("  const attachRemoteVideo = (track: MediaStreamTrack) => {"):]
    body = body[: body.index("\n  };\n")]
    guard = body.index("if (isOurCameraTrack(track)) return;")
    assert guard < body.index("state.remoteVideoIds.add(track.id);")
    assert guard < body.index("document.createElement('video')")


_HARNESS = r"""
let nextId = 1;
class FakeTrack {
  constructor() { this.id = 'track-' + (nextId++); this.kind = 'video'; }
  clone() { return new FakeTrack(); }
}
const win = { MediaStreamTrack: FakeTrack };
const state = { cameraTrack: null, cameraCloneIds: new Set() };
BLOCK
state.cameraTrack = new FakeTrack();
const handed = state.cameraTrack.clone();         // what cameraTrack() gives WhatsApp
const whatsappCopy = handed.clone();              // WhatsApp cloning it again
const remote = new FakeTrack();                   // the other person's video
const remoteCopy = remote.clone();
console.log(JSON.stringify({
  canvas_track: isOurCameraTrack(state.cameraTrack),
  handed_clone: isOurCameraTrack(handed),
  whatsapp_reclone: isOurCameraTrack(whatsappCopy),
  remote: isOurCameraTrack(remote),
  remote_clone: isOurCameraTrack(remoteCopy),
  nothing: isOurCameraTrack(null),
}));
"""


@pytest.fixture(scope="module")
def ownership(tmp_path_factory):
    node = _node()
    if not node:
        pytest.skip("no Node runtime available")
    src = BRIDGE.read_text(encoding="utf-8")
    start = src.index("  const isOurCameraTrack = ")
    block = src[start:src.index("  const cameraTrack = () => {", start)]
    # The specific `this` annotation must go first: the generic "(x: any)"
    # rule below would otherwise turn it into an invalid "function (this)".
    block = block.replace("function (this: any)", "function ()").replace("(clone as any)", "clone")
    block = re.sub(r"\((\w+):\s*any\)", r"(\1)", block)
    block = re.sub(r"\):\s*boolean\s*=>", ") =>", block)
    js = _HARNESS.replace("BLOCK", block)
    path = tmp_path_factory.mktemp("ownership") / "ownership.js"
    path.write_text(js, encoding="utf-8")
    run = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_our_camera_and_every_clone_of_it_are_recognised(ownership):
    assert ownership["canvas_track"] is True
    assert ownership["handed_clone"] is True
    # WhatsApp re-cloning the track we gave it must not launder it into
    # something the remote-video extraction would accept.
    assert ownership["whatsapp_reclone"] is True


def test_the_other_persons_video_is_never_mistaken_for_ours(ownership):
    assert ownership["remote"] is False
    assert ownership["remote_clone"] is False
    assert ownership["nothing"] is False
