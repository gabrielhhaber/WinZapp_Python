"""wa-js must select the chat action when two native modules export the same name.

The lower-level WAWebFindChat skips ChatCollection and returns database rows;
WAWebFindChatAction supplies the cache-aware bridge and returns ChatModels.
Tests execute the published export binding under Node with synthetic modules.
No WhatsApp session, server, network, or message-send method is involved.
"""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from core.wppconnect_wa_js_patch import (
    STATUS_ALREADY, STATUS_APPLIED, STATUS_NO_MATCH, WA_JS_BUNDLE_PARTS,
    patch_chat_lookup_bundle, patch_chat_lookup_source,
)

ROOT = Path(__file__).resolve().parents[1]
# Export binding from the installed wa-js 4.7.1 bundle. Webpack wraps this
# expression in a module; the synthetic loader below executes the same call.
BINDING = (
    '(0,r(54993).exportModule)(t,{findOrCreateLatestChat:"findOrCreateLatestChat"},'
    'e=>e.findOrCreateLatestChat)'
)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def chat_bundle(tmp_path):
    bundle = tmp_path.joinpath(*WA_JS_BUNDLE_PARTS)
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(('"use strict";\r\n' + BINDING + ';\r\n//# sourceMappingURL=x\r\n').encode())
    return tmp_path, bundle


class TestChatLookupSource:
    @pytest.mark.parametrize("binding", [BINDING, BINDING.replace("e=>e.", "$x=>$x.")])
    def test_repeated_patching_is_a_no_op(self, binding):
        once, status = patch_chat_lookup_source(binding)
        twice, repeated = patch_chat_lookup_source(once)

        assert status == STATUS_APPLIED
        assert repeated == STATUS_ALREADY
        assert twice == once

    @pytest.mark.parametrize("content", [
        BINDING.replace("findOrCreateLatestChat", "findExistingChat"),
        BINDING.replace("e=>e.", "e=>other."),
        BINDING.replace("e=>e.findOrCreateLatestChat", '(e,t)=>"AnotherModule"===t'),
    ])
    def test_unrelated_or_unrecognized_bindings_are_untouched(self, content):
        patched, status = patch_chat_lookup_source(content)
        assert (patched, status) == (content, STATUS_NO_MATCH)


_HARNESS = r"""
const assert = require('node:assert/strict');
const config = CONFIG;
const metrics = {databaseReads: 0, actionCalls: 0};
const id = {toString: () => config.id};
const options = {forceUsync: true, isGroupJoin: false};
const cachedChat = {id, model: true};
const databaseRow = {id: config.id};
const raw = {async findOrCreateLatestChat() {
  metrics.databaseReads++;
  if (!config.databaseHasChat) throw new Error('findChat: new chat not found');
  return {chat: databaseRow, created: false};
}};
const action = {async findOrCreateLatestChat(wid, origin, receivedOptions) {
  metrics.actionCalls++;
  assert.equal(wid, id);
  assert.equal(origin, 'createChat');
  assert.equal(receivedOptions, options);
  return {chat: cachedChat, created: false};
}};
const modules = new Map([
  ['WAWebFindChat', raw], ['WAWebFindChatAction', action],
]);
const t = {};
const r = () => ({exportModule(target, names, predicate) {
  const match = [...modules].find(([name, module]) => predicate(module, name));
  if (!match) throw new Error('Native module not found');
  for (const [publicName, nativeName] of Object.entries(names)) {
    target[publicName] = match[1][nativeName];
  }
}});
BINDING;
(async () => {
  try {
    const result = await t.findOrCreateLatestChat(id, 'createChat', options);
    console.log(JSON.stringify({cachedModel: result.chat === cachedChat, created: result.created, metrics}));
  } catch (error) {
    console.log(JSON.stringify({error: error.message, metrics}));
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
"""


def _run(binding, database_has_chat, jid="10000000000001@lid"):
    bundled = ROOT / "client/node/node.exe"
    node = str(bundled) if bundled.exists() else shutil.which("node")
    if not node:
        pytest.skip("node not available")
    script = _HARNESS.replace("CONFIG", json.dumps({"databaseHasChat": database_has_chat, "id": jid})).replace("BINDING", binding)
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20, check=True)
    return json.loads(result.stdout)


class TestNativeModuleSelection:
    def test_ambiguous_binding_throws_despite_the_cached_chat(self):
        assert _run(BINDING, False) == {
            "error": "findChat: new chat not found",
            "metrics": {"databaseReads": 1, "actionCalls": 0},
        }

    def test_ambiguous_binding_can_return_a_database_row_instead_of_a_model(self):
        assert _run(BINDING, True)["cachedModel"] is False

    @pytest.mark.parametrize("database_has_chat", [False, True])
    @pytest.mark.parametrize("jid", ["10000000000001@lid", "10000000001@c.us", "10000000001-1000000000@g.us"])
    def test_patched_binding_uses_the_action_and_preserves_call_arguments(self, database_has_chat, jid):
        patched, _ = patch_chat_lookup_source(BINDING)
        assert _run(patched, database_has_chat, jid) == {
            "cachedModel": True, "created": False,
            "metrics": {"databaseReads": 0, "actionCalls": 1},
        }


class TestChatLookupFile:
    def test_only_the_binding_changes_and_a_repeat_preserves_bytes(self, chat_bundle):
        api, bundle = chat_bundle
        before = bundle.read_bytes()
        ok, _ = patch_chat_lookup_bundle(str(api))
        after = bundle.read_bytes()
        assert ok
        patched, _ = patch_chat_lookup_source(BINDING)
        assert after == before.replace(BINDING.encode(), patched.encode())
        assert patch_chat_lookup_bundle(str(api))[0]
        assert bundle.read_bytes() == after

    def test_missing_bundle_is_reported(self, tmp_path):
        ok, note = patch_chat_lookup_bundle(str(tmp_path))
        assert not ok
        assert "not found" in note

    def test_unrecognized_bundle_is_reported_without_writing(self, chat_bundle):
        api, bundle = chat_bundle
        before = b"an upstream rewrite\r\n"
        bundle.write_bytes(before)
        ok, note = patch_chat_lookup_bundle(str(api))
        assert not ok
        assert "did not match" in note
        assert bundle.read_bytes() == before


class TestChatLookupCallSites:
    def test_setup_api(self, chat_bundle):
        api, bundle = chat_bundle
        assert _load("setup_api")._patch_chat_lookup_bundle(str(api))
        assert patch_chat_lookup_source(bundle.read_text())[1] == STATUS_ALREADY

    def test_api_setup_on_startup(self, chat_bundle):
        from ui.dialogs.api_setup import ApiSetupDialog
        api, bundle = chat_bundle
        ApiSetupDialog._apply_node_modules_patches(str(api))
        assert patch_chat_lookup_source(bundle.read_text())[1] == STATUS_ALREADY

    def test_build_api(self, chat_bundle):
        api, bundle = chat_bundle
        _load("build_api")._apply_node_modules_patches(str(api))
        assert patch_chat_lookup_source(bundle.read_text())[1] == STATUS_ALREADY
