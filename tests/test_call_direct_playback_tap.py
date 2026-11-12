"""The call is heard when WhatsApp Web plays it straight into its AudioContext.

Measured 2026-11-12 on a real account over CDP: web_voip_audio_playback_impl=3
(the shared-buffer worklet) with web_voip_audio_playback_use_media_element_output
off. WhatsApp Web then runs `worklet.connect(context.destination)` and no
MediaStream ever carries the other side: the bridge saw no remote track
(remoteTracksAttached=0), WinZapp played -120 dBFS for the whole call and
nobody heard anybody. The bridge now taps a page worklet where it reaches a
destination, and keeps it off the speakers: WhatsApp Web had pointed that
context at the default output, so the other person was heard twice (an echo).

This runs the block of callMediaBridge.ts verbatim under Node against fake
Web Audio classes.
"""

import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "client" / "api_patches" / "src" / "util" / "callMediaBridge.ts"
START = "  // ── Direct AudioContext playback"
END = "  // ── end of direct AudioContext playback"


def _node():
    bundled = ROOT / "client" / "node" / "node.exe"
    if bundled.exists():
        return str(bundled)
    return shutil.which("node")


def _block() -> str:
    src = BRIDGE.read_text(encoding="utf-8")
    start = src.index(START)
    block = src[start:src.index(END, start)]
    block = block.replace("new WeakMap<any, any>()", "new WeakMap()")
    block = re.sub(r"\(this: any, target: any, \.\.\.rest: any\[\]\)", "(target, ...rest)", block)
    block = re.sub(r"\((\w+): any, (\w+): any, (\w+): any\)(?:: boolean)?", r"(\1, \2, \3)", block)
    block = block.replace(" as any", "")
    return block


_HARNESS = r"""
const log = [];
class AudioNode { constructor(context) { this.context = context; } connect(target) { log.push(['native', this.name, target.name]); return target; } }
class AudioWorkletNode extends AudioNode {}
class GainNode extends AudioNode {}
const gains = [];
class AudioDestinationNode extends AudioNode {}
class MediaStreamAudioDestinationNode extends AudioNode {}
const makeContext = (name) => {
  const context = { name, sampleRate: 48000 };
  context.destination = Object.assign(new AudioDestinationNode(context), { name: name + '.destination' });
  context.createMediaStreamDestination = () => Object.assign(
    new MediaStreamAudioDestinationNode(context), { name: name + '.tap', stream: name + '.stream' });
  context.createGain = () => { const g = Object.assign(new GainNode(context), { name: name + '.muted', gain: { value: 1 } }); gains.push(g); return g; };
  return context;
};
const ours = makeContext('ours');
const page = makeContext('page');
const win = { AudioNode, AudioWorkletNode, AudioDestinationNode };
const state = { context: ours };
const report = (event, details) => log.push(['report', event]);
const attachRemoteStream = (stream) => log.push(['attach', stream]);

%BLOCK%

const playback = Object.assign(new AudioWorkletNode(page), { name: 'voip-playback' });
const returned = playback.connect(page.destination);
playback.connect(page.destination);                       // reconnected after a device switch
const toElement = Object.assign(new AudioWorkletNode(page), { name: 'via-element' });
toElement.connect(page.createMediaStreamDestination());    // the media element route
const gain = Object.assign(new GainNode(page), { name: 'gain' });
gain.connect(page.destination);                            // a tone, not the call
const mine = Object.assign(new AudioWorkletNode(ours), { name: 'winzapp' });
mine.connect(ours.destination);
const throwing = makeContext('broken');
throwing.createMediaStreamDestination = () => { throw new Error('closed'); };
const late = Object.assign(new AudioWorkletNode(throwing), { name: 'late' });
const lateReturned = late.connect(throwing.destination);
console.log(JSON.stringify({ log, returned: returned.name, lateReturned: lateReturned.name,
  gains: gains.map((g) => [g.name, g.gain.value]) }));
"""


@pytest.fixture(scope="module")
def outcome(tmp_path_factory):
    node = _node()
    if not node:
        pytest.skip("no Node to run the bridge block")
    script = tmp_path_factory.mktemp("direct-tap") / "run.js"
    script.write_text(_HARNESS.replace("%BLOCK%", _block()), encoding="utf-8")
    run = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_a_page_worklet_played_into_the_destination_is_tapped_once(outcome):
    log = outcome["log"]
    assert ["native", "voip-playback", "page.tap"] in log
    assert log.count(["attach", "page.stream"]) == 1
    assert log.count(["report", "remote-direct"]) == 1
    assert outcome["returned"] == "page.destination"     # WhatsApp sees the connection it asked for


def test_it_never_reaches_the_speakers_itself(outcome):
    log = outcome["log"]
    assert ["native", "voip-playback", "page.destination"] not in log
    assert ["native", "voip-playback", "page.muted"] in log
    assert ["native", "page.muted", "page.destination"] in log
    assert ["page.muted", 0] in outcome["gains"]
    assert outcome["gains"].count(["page.muted", 0]) == 1  # one route, reused on reconnect


def test_other_connections_are_left_alone(outcome):
    log = outcome["log"]
    assert ["native", "via-element", "page.tap"] in log    # already seen by the media scan
    assert ["native", "gain", "page.destination"] in log   # a tone plays as before
    assert ["native", "winzapp", "ours.destination"] in log
    assert not any(entry[0] == "attach" and entry[1] != "page.stream" for entry in log)


def test_a_tap_that_fails_never_breaks_the_page(outcome):
    assert outcome["lateReturned"] == "broken.destination"
    assert ["native", "late", "broken.destination"] in outcome["log"]
