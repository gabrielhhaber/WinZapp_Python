"""Forwarding must survive a WhatsApp Web build whose forward module cannot be required.

Measured 2026-10-02 on WhatsApp Web 2.3000.1049007170: every POST
/forward-messages answered 500 ``forward_messages_not_available``. wa-js's
``WPP.chat.forwardMessages`` awaits ``ensureLazyModule('WAWebChatForwardMessage')``
and then needs its own ``functions.forwardMessages`` binding; ``require()`` of
that module throws "unresolved dependencies" because a script the Bootloader
lists among the page's resources was never loaded, and wa-js neither loads it
nor binds late. client/api_patches/src/util/forwardRuntime.ts loads the
unloaded resources and calls WhatsApp's function directly.

The code under test is a string evaluated in a real page, so two kinds of check:
source contracts (like tests/test_status_reply_quotes_the_live_model.py) and,
when ``node`` is on PATH, the very same text run against a fake ``window``.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "client" / "api_patches" / "src"
RUNTIME = SRC / "util" / "forwardRuntime.ts"
CONTROLLER = SRC / "controller" / "deviceController.ts"


@pytest.fixture(scope="module")
def runtime_ts() -> str:
    return RUNTIME.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def page_source(runtime_ts: str) -> str:
    match = re.search(r"String\.raw`(.*?)`;", runtime_ts, re.S)
    assert match, "the in-page function must stay a String.raw template"
    return match.group(1)


@pytest.fixture(scope="module")
def handler() -> str:
    source = CONTROLLER.read_text(encoding="utf-8")
    start = source.index("export async function forwardMessages")
    return source[start:source.index("export async function", start + 10)]


class TestWiring:
    def test_the_handler_uses_the_runtime_not_the_library(self, handler):
        assert "buildForwardRuntimeExpression(" in handler
        assert "forwardMessagesV2" not in handler, (
            "the library call is the one that fails with "
            "forward_messages_not_available"
        )

    def test_the_handler_does_not_retry_or_fall_back(self, handler):
        assert handler.count("page.evaluate(") == 1, (
            "a second evaluate after a failure could forward twice"
        )

    def test_a_failure_is_reported_with_its_detail(self, handler):
        assert "status(500)" in handler
        assert "detail:" in handler

    def test_the_success_shape_is_unchanged(self, handler):
        assert "res.status(201).json({ status: 'success', response: response })" \
            in handler

    def test_the_file_is_in_every_list(self):
        name = "src/util/forwardRuntime.ts"
        for path in ("setup_api.py", "build.py", "client/ui/dialogs/api_setup.py",
                     "tests/test_api_patches_in_sync.py"):
            assert name in (ROOT / path).read_text(encoding="utf-8"), path


class TestPageFunctionContract:
    def test_only_whatsapp_static_scripts_are_loaded(self, page_source):
        assert "url.protocol === 'https:'" in page_source
        assert "url.hostname === 'static.whatsapp.net'" in page_source
        assert "url.pathname.endsWith('.js')" in page_source

    def test_the_direct_call_arguments(self, page_source):
        call = page_source[page_source.index("mod.forwardMessages({"):]
        call = call[:call.index("});")]
        for part in ("chat: chat", "msgs: msgs", "multicast: false",
                     "includeCaption: false", "appendedText: false"):
            assert part in call

    def test_there_is_exactly_one_direct_call(self, page_source):
        assert page_source.count("mod.forwardMessages(") == 1

    def test_the_send_is_not_wrapped_in_a_catch(self, page_source):
        send = page_source.index("mod.forwardMessages(")
        assert "catch" not in page_source[send:], (
            "a rejected forward must propagate, never be retried"
        )

    def test_it_is_self_contained(self, page_source):
        assert "`" not in page_source and "${" not in page_source


HARNESS = r"""
const fs = require('fs');
const [, , tsPath, scenario] = process.argv;
const ts = fs.readFileSync(tsPath, 'utf8');
const src = /String\.raw`([\s\S]*?)`;/.exec(ts)[1];
const calls = { forward: [], lib: [], scripts: [], sentTypes: [] };
const world = JSON.parse(process.env.WORLD);
let loadedScripts = 0;
const modAvailable = () => loadedScripts >= world.scriptsNeeded;
// Voice messages are real models: one instance per id, with mediaData.toJSON
// on the prototype (or own, world.ownToJSON), as in WhatsApp Web.
class MediaData { toJSON() { return { type: 'ptt', filehash: 'h' }; } }
const ownToJSON = function () { return { type: 'ptt', filehash: 'h' }; };
const voiceModels = {};
for (const id of world.voiceIds || []) {
  const mediaData = new MediaData();
  if (world.ownToJSON) mediaData.toJSON = ownToJSON;
  voiceModels[id] = { msgId: id, type: 'ptt', mediaData };
}
// An audio FILE: a media message too, but not a voice message.
for (const id of world.audioIds || []) {
  voiceModels[id] = { msgId: id, type: 'audio',
                      mediaData: { toJSON: ownToJSON, isAudioFile: true } };
}
// What WAWebMediaForwardMediaMsg.forwardMediaMsg does to a voice message.
const whatsappConverts = (msgs) => {
  for (const m of msgs) {
    if (!m || !m.mediaData) continue;
    const q = m.mediaData.toJSON();
    if (q.type === 'ptt' && !world.noConversion) q.type = 'audio';
    calls.sentTypes.push(Object.assign({}, q).type);
  }
};
const guardsLeft = () => Object.values(voiceModels).filter((m) => world.ownToJSON || m.mediaData.isAudioFile
  ? m.mediaData.toJSON !== ownToJSON
  : Object.prototype.hasOwnProperty.call(m.mediaData, 'toJSON')).length;
const mod = {
  forwardMessages: async (a) => {
    calls.forward.push(a);
    if (world.forwardDelayMs) await new Promise((r) => setTimeout(r, world.forwardDelayMs));
    whatsappConverts(a.msgs);
    if (world.rejectForward || (world.rejectFirstOnly && calls.forward.length === 1)) {
      throw new Error('rejected by whatsapp');
    }
    if (world.circular) { const c = { id: 'x' }; c.self = c; return c; }
    return ['sent'];
  },
};
const resources = new Map(Object.entries(world.resources));
const componentMap = new Map(Object.entries(world.components));
global.window = {
  require: (name) => {
    if (name === 'Bootloader') return { __debug: { componentMap, resources, loaded: new Set() } };
    if (name === 'WAWebChatForwardMessage') {
      if (modAvailable() || world.alwaysWorks) return mod;
      const e = new Error('x');
      e.messageParams = ['m', 'Requiring module WAWebChatForwardMessage with unresolved dependencies: WAWebDualUploadsSendPolicy is not defined'];
      throw e;
    }
    throw new Error('unknown module ' + name);
  },
  WPP: {
    whatsapp: { functions: world.bound ? { forwardMessages: () => 1 } : {} },
    chat: {
      find: async (id) => ({ chatId: id }),
      getMessageById: async (id) => voiceModels[id] || { msgId: id },
      forwardMessages: async (c, ids) => {
        calls.lib.push([c, ids]);
        // By id the library builds its own model of the message.
        whatsappConverts(ids.map((x) => (typeof x === 'string' && voiceModels[x]
          ? { mediaData: new MediaData() } : x)));
        return ['lib'];
      },
    },
  },
};
global.document = {
  head: {
    appendChild: (s) => {
      calls.scripts.push(s.src);
      setTimeout(() => {
        if (world.scriptFails) return s.onerror();
        loadedScripts += 1;
        s.onload();
      }, 0);
    },
  },
  createElement: () => ({}),
};
const fn = eval('(' + src + ')');
const args = { chatId: 'chat@c.us', messageIds: ['a', 'b'] };
(async () => {
  const batches = [];
  for (const batch of world.batches || [[args]]) {
    const settled = await Promise.allSettled(batch.map((a) => fn(a)));
    batches.push(settled.map((r) => r.status === 'fulfilled'
      ? { value: r.value } : { err: String(r.reason && r.reason.message || r.reason) }));
  }
  const first = batches[0];
  const out = first.filter((r) => 'value' in r).map((r) => r.value);
  const failed = first.find((r) => 'err' in r);
  console.log(JSON.stringify({ out, err: failed ? failed.err : null, batches, calls,
                               guardsLeft: guardsLeft() }));
})();
"""

COMPONENTS = {
    "WAWebForwardMessageFlow.react": {"r": ["h1", "h2", "h3", "h4"]},
    "WAWebOther": {"r": ["h9"]},
}
RESOURCES = {
    "h1": {"src": "https://static.whatsapp.net/rsrc.php/v4/y4/l/en_US/a.js"},
    "h2": {"src": "https://static.whatsapp.net/rsrc.php/v4/y4/l/b.css"},
    "h3": {"src": "https://evil.example/c.js"},
    "h4": {"src": "http://static.whatsapp.net/d.js"},
    "h9": {"src": "https://static.whatsapp.net/never-asked.js"},
}


def _run(tmp_path, **world):
    base = {"scriptsNeeded": 1, "resources": RESOURCES, "components": COMPONENTS}
    base.update(world)
    script = tmp_path / "harness.js"
    script.write_text(HARNESS, encoding="utf-8")
    import os
    env = dict(os.environ, WORLD=json.dumps(base))
    done = subprocess.run(
        ["node", str(script), str(RUNTIME), "x"],
        capture_output=True, text=True, timeout=20, env=env,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node missing")


@needs_node
class TestRunInFakePage:
    def test_heals_with_whitelisted_scripts_and_calls_once(self, tmp_path):
        r = _run(tmp_path)
        assert r["err"] is None
        assert r["calls"]["scripts"] == [RESOURCES["h1"]["src"]]
        assert len(r["calls"]["forward"]) == 1
        args = r["calls"]["forward"][0]
        assert args == {
            "chat": {"chatId": "chat@c.us"},
            "msgs": [{"msgId": "a"}, {"msgId": "b"}],
            "multicast": False, "includeCaption": False, "appendedText": False,
        }
        assert r["out"] == [{"ok": True, "response": ["sent"]}]

    def test_unhealable_reports_the_chain_and_sends_nothing(self, tmp_path):
        r = _run(tmp_path, scriptFails=True)
        assert r["calls"]["forward"] == []
        out = r["out"][0]
        assert out["ok"] is False
        assert "WAWebDualUploadsSendPolicy is not defined" in out["detail"]

    def test_a_rejected_forward_is_not_retried(self, tmp_path):
        r = _run(tmp_path, rejectForward=True)
        assert r["err"] == "rejected by whatsapp"
        assert len(r["calls"]["forward"]) == 1

    def test_concurrent_forwards_share_one_heal(self, tmp_path):
        other = {"chatId": "chat@c.us", "messageIds": ["c"]}
        first = {"chatId": "chat@c.us", "messageIds": ["a"]}
        r = _run(tmp_path, batches=[[first, other]])
        assert len(r["calls"]["scripts"]) == 1
        assert len(r["calls"]["forward"]) == 2  # different ids: both are sent

    def test_never_loads_a_url_outside_the_whitelist(self, tmp_path):
        """The module never resolves, so the loop walks every candidate; each
        look-alike below must still be refused (mutating the host check to
        ``|| true`` makes this fail)."""
        good = "https://static.whatsapp.net/rsrc.php/ok.js"
        bad = [
            "https://evilstatic.whatsapp.net/x.js",
            "https://static.whatsapp.net.evil.com/x.js",
            "https://user@static.whatsapp.net/x.js",
            "https://user:pw@static.whatsapp.net/x.js",
            "https://static.whatsapp.net:8443/x.js",
            "http://static.whatsapp.net/x.js",
            "https://static.whatsapp.net/x.css",
            "https://static.whatsapp.net/x.js.css",
        ]
        urls = [good] + bad
        resources = {f"r{i}": {"src": u} for i, u in enumerate(urls)}
        components = {"WAWebForwardMessageFlow.react": {"r": list(resources)}}
        r = _run(tmp_path, scriptsNeeded=99, resources=resources,
                 components=components)
        assert r["calls"]["scripts"] == [good]
        assert r["calls"]["forward"] == []
        assert r["out"][0]["ok"] is False

    def test_a_later_candidate_can_resolve_the_module(self, tmp_path):
        resources = {
            "r0": {"src": "https://static.whatsapp.net/one.js"},
            "r1": {"src": "https://static.whatsapp.net/two.js"},
        }
        components = {"WAWebForwardMessageFlow.react": {"r": ["r0", "r1"]}}
        r = _run(tmp_path, scriptsNeeded=2, resources=resources,
                 components=components)
        assert len(r["calls"]["scripts"]) == 2
        assert r["out"][0]["ok"] is True

    def test_an_overlapping_retry_waits_instead_of_sending(self, tmp_path):
        same = {"chatId": "chat@c.us", "messageIds": ["a", "b"]}
        r = _run(tmp_path, forwardDelayMs=50, batches=[[same, same]])
        assert len(r["calls"]["forward"]) == 1
        first, second = r["batches"][0]
        assert first == second and first["value"]["ok"] is True

    def test_different_chat_or_ids_are_not_deduped(self, tmp_path):
        a = {"chatId": "chat@c.us", "messageIds": ["a"]}
        b = {"chatId": "chat@c.us", "messageIds": ["b"]}
        c = {"chatId": "other@c.us", "messageIds": ["a"]}
        r = _run(tmp_path, forwardDelayMs=20, batches=[[a, b, c]])
        assert len(r["calls"]["forward"]) == 3

    def test_a_rejection_releases_the_key_for_a_later_forward(self, tmp_path):
        same = {"chatId": "chat@c.us", "messageIds": ["a"]}
        r = _run(tmp_path, rejectFirstOnly=True, forwardDelayMs=20,
                 batches=[[same, same], [same]])
        # The overlapping twin shares the rejection (no second send) ...
        assert [x.get("err") for x in r["batches"][0]] == ["rejected by whatsapp"] * 2
        # ... and the key is free again for a deliberate later forward.
        assert r["batches"][1][0]["value"]["ok"] is True
        assert len(r["calls"]["forward"]) == 2

    def test_the_response_is_serializable_even_for_a_circular_result(self, tmp_path):
        r = _run(tmp_path, circular=True)
        assert r["err"] is None
        assert r["out"] == [{"ok": True, "response": None}]

    def test_a_working_build_uses_the_library_path(self, tmp_path):
        r = _run(tmp_path, alwaysWorks=True, bound=True)
        assert r["calls"]["forward"] == [] and r["calls"]["scripts"] == []
        assert r["calls"]["lib"] == [["chat@c.us", ["a", "b"]]]


def _voice(**extra):
    return dict({"chatId": "chat@c.us", "messageIds": ["v"], "keepVoice": True}, **extra)


@needs_node
class TestAForwardedVoiceMessageStaysOne:
    """WhatsApp Web turns a forwarded voice message into a plain audio, which
    ends the sequential playback of voice messages at the destination.
    keepVoice makes that one write a no-op for the message being forwarded; the
    fake page converts exactly as WAWebMediaForwardMediaMsg does."""

    def test_without_the_option_it_goes_out_as_audio(self, tmp_path):
        plain = {"chatId": "chat@c.us", "messageIds": ["v"]}
        r = _run(tmp_path, voiceIds=["v"], batches=[[plain]])
        assert r["calls"]["sentTypes"] == ["audio"]
        assert r["out"] == [{"ok": True, "response": ["sent"]}]

    def test_with_it_the_type_survives_and_the_outcome_says_so(self, tmp_path):
        r = _run(tmp_path, voiceIds=["v"], batches=[[_voice()]])
        assert r["calls"]["sentTypes"] == ["ptt"]
        assert r["out"][0]["voice"] == {"asked": 1, "kept": 1}
        assert r["guardsLeft"] == 0

    def test_only_the_voice_messages_of_a_batch_are_guarded(self, tmp_path):
        r = _run(tmp_path, voiceIds=["v"],
                 batches=[[_voice(messageIds=["a", "v", "b"])]])
        assert r["calls"]["sentTypes"] == ["ptt"]
        assert r["out"][0]["voice"] == {"asked": 1, "kept": 1}

    def test_an_audio_file_is_not_a_voice_message(self, tmp_path):
        """Guarding it would report it as a voice message that was not kept."""
        r = _run(tmp_path, audioIds=["f"], batches=[[_voice(messageIds=["f"])]])
        assert r["out"][0]["voice"] == {"asked": 0, "kept": 0}
        assert r["guardsLeft"] == 0

    def test_a_batch_without_a_voice_message_is_untouched(self, tmp_path):
        r = _run(tmp_path, batches=[[_voice(messageIds=["a", "b"])]])
        assert r["out"][0] == {"ok": True, "response": ["sent"],
                               "voice": {"asked": 0, "kept": 0}}
        assert r["calls"]["forward"][0]["msgs"] == [{"msgId": "a"}, {"msgId": "b"}]

    @pytest.mark.parametrize("own", [False, True])
    def test_the_message_is_left_as_it_was(self, tmp_path, own):
        """toJSON on the prototype (the real case) or as an own property."""
        r = _run(tmp_path, voiceIds=["v"], ownToJSON=own, batches=[[_voice()]])
        assert r["calls"]["sentTypes"] == ["ptt"] and r["guardsLeft"] == 0

    def test_a_rejected_forward_still_takes_the_guard_off(self, tmp_path):
        r = _run(tmp_path, voiceIds=["v"], rejectForward=True, batches=[[_voice()]])
        assert r["err"] == "rejected by whatsapp"
        assert r["guardsLeft"] == 0

    def test_the_same_message_to_two_chats_at_once(self, tmp_path):
        """Both forwards share one guard. Independent wrappers would restore
        each other's function and leave one behind for good."""
        r = _run(tmp_path, voiceIds=["v"], forwardDelayMs=30,
                 batches=[[_voice(), _voice(chatId="other@c.us")]])
        assert r["calls"]["sentTypes"] == ["ptt", "ptt"]
        assert [x["value"]["voice"] for x in r["batches"][0]] == [{"asked": 1, "kept": 1}] * 2
        assert r["guardsLeft"] == 0

    def test_an_audio_forward_right_after_is_an_audio_again(self, tmp_path):
        plain = {"chatId": "chat@c.us", "messageIds": ["v"]}
        r = _run(tmp_path, voiceIds=["v"], batches=[[_voice()], [plain]])
        assert r["calls"]["sentTypes"] == ["ptt", "audio"]

    def test_whatsapp_no_longer_converting_there_is_reported_not_fatal(self, tmp_path):
        """The guard never fires: the message is sent as WhatsApp sends it and
        the outcome says the type was not kept by us."""
        r = _run(tmp_path, voiceIds=["v"], noConversion=True, batches=[[_voice()]])
        assert r["err"] is None
        assert r["out"][0]["voice"] == {"asked": 1, "kept": 0}

    def test_a_working_build_hands_the_guarded_models_to_the_library(self, tmp_path):
        r = _run(tmp_path, voiceIds=["v"], alwaysWorks=True, bound=True,
                 batches=[[_voice()]])
        assert r["calls"]["forward"] == []
        assert r["calls"]["sentTypes"] == ["ptt"]
        assert r["out"][0]["voice"] == {"asked": 1, "kept": 1}
        assert r["guardsLeft"] == 0

    def test_a_keep_and_a_plain_forward_of_the_same_message_are_two_sends(self, tmp_path):
        """The overlap guard is for a retry of the SAME request."""
        plain = {"chatId": "chat@c.us", "messageIds": ["v"]}
        r = _run(tmp_path, voiceIds=["v"], forwardDelayMs=20, batches=[[_voice(), plain]])
        assert len(r["calls"]["forward"]) == 2


class TestKeepVoiceWiring:
    def test_only_an_explicit_true_asks_for_it(self, handler):
        assert "keepVoice: keepVoice === true" in handler

    def test_a_voice_message_that_went_out_as_audio_is_logged(self, handler):
        assert "outcome.voice.kept < outcome.voice.asked" in handler
        assert "req.logger.warn(" in handler
