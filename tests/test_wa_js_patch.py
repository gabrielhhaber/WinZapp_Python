"""Tests for the wa-js bundle patch: a message to Meta AI failing before it
is sent (issue #365).

prepareRawMessage() reads `BotProfileStore.get(chat.id).personaId` for a bot
chat. The store is a cache WhatsApp fills on its own schedule, so on an
account whose bot profile has not arrived the lookup is undefined and every
send to Meta AI ends in HTTP 500:

    TypeError: Cannot read properties of undefined (reading 'personaId')

The patch guards the lookup, so the message goes out without a persona id —
what WhatsApp Web's own send path does. The bot branch below is copied
verbatim from the published bundles and run under Node, unpatched and
patched, against a profile store that does and does not hold the profile.
"""

import importlib.util
import json
import pathlib
import shutil
import subprocess

import pytest

from core.wppconnect_wa_js_patch import (
    STATUS_ALREADY, STATUS_APPLIED, STATUS_NO_MATCH, WA_JS_BUNDLE_PARTS,
    patch_wa_js_bundle, patch_wa_js_source,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]

META_AI_CHAT = "13135550002@c.us"
META_AI_PERSONA = "867051314767696"

_BOT_BRANCH = (
    "(null===({is_bot}=e.id)||void 0==={is_bot}?void 0:{is_bot}.isBot())&&(t=Object.assign("
    "Object.assign({{}},t),{{messageSecret:await(0,c.genBotMsgSecretFromMsgSecret)("
    "crypto.getRandomValues(new Uint8Array(32))),botPersonaId:s.BotProfileStore.get("
    "null===({tmp}=e.id)||void 0==={tmp}?void 0:{tmp}.toString()).personaId}}));"
)
#: The bot branch of prepareRawMessage() as wa-js 4.5.0 to 4.6.1 ship it...
BOT_BRANCH_4_6 = _BOT_BRANCH.format(is_bot="g", tmp="m")
#: ...and as 3.23.3 to 4.4.3 ship it: the same code, other temporaries.
BOT_BRANCH_4_4 = _BOT_BRANCH.format(is_bot="p", tmp="g")

BOTH_BUILDS = pytest.mark.parametrize(
    "bot_branch", [BOT_BRANCH_4_6, BOT_BRANCH_4_4], ids=["wa-js-4.6", "wa-js-4.4"],
)


def _bundle(bot_branch: str) -> str:
    return f'"use strict";var before=1;{bot_branch}if(r.messageId){{}}\n//# sourceMappingURL=x\n'


def _load_setup_api():
    spec = importlib.util.spec_from_file_location("setup_api", ROOT / "setup_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_api_dir(tmp_path):
    bundle = tmp_path.joinpath(*WA_JS_BUNDLE_PARTS)
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(_bundle(BOT_BRANCH_4_6).encode("utf-8"))
    return tmp_path, bundle


class TestSourcePatch:
    @BOTH_BUILDS
    def test_guards_the_lookup_and_nothing_else(self, bot_branch):
        content = _bundle(bot_branch)

        patched, status = patch_wa_js_source(content)

        assert status == STATUS_APPLIED
        assert "||{}).personaId}));" in patched
        assert patched.replace("botPersonaId:(", "botPersonaId:").replace("||{})", "") == content

    @BOTH_BUILDS
    def test_is_idempotent(self, bot_branch):
        once, _ = patch_wa_js_source(_bundle(bot_branch))

        twice, status = patch_wa_js_source(once)

        assert status == STATUS_ALREADY
        assert twice == once

    def test_a_bundle_without_the_lookup_is_left_alone(self):
        content = '"use strict";botPersonaId:lookup(e.id)?.personaId;'

        patched, status = patch_wa_js_source(content)

        assert status == STATUS_NO_MATCH
        assert patched == content

    def test_the_temporary_must_be_one_identifier_throughout(self):
        """Not a lookup of the chat's own id, so not the expression to guard."""
        content = _bundle(BOT_BRANCH_4_6).replace("void 0===m?void 0:m.toString()", "void 0===m?void 0:q.toString()")

        patched, status = patch_wa_js_source(content)

        assert status == STATUS_NO_MATCH
        assert patched == content


def _node():
    bundled = ROOT / "client" / "node" / "node.exe"
    if bundled.exists():
        return str(bundled)
    return shutil.which("node")


_HARNESS = """
const profiles = new Map(PROFILES);
const s = { BotProfileStore: { get: (id) => profiles.get(id) } };
const c = { genBotMsgSecretFromMsgSecret: async () => 'secret' };
const e = { id: { isBot: () => true, toString: () => CHAT } };
(async () => {
  let t = { type: 'chat', body: 'hi' }, g, m, p;
  BOT_BRANCH
  return t;
})().then(
  (message) => console.log(JSON.stringify({ message })),
  (error) => console.log(JSON.stringify({ error: error.message })),
);
"""


def _run_bot_branch(bot_branch: str, profiles: dict) -> dict:
    node = _node()
    if not node:
        pytest.skip("node not available")
    script = (
        _HARNESS.replace("PROFILES", json.dumps([[k, v] for k, v in profiles.items()]))
        .replace("CHAT", json.dumps(META_AI_CHAT))
        .replace("BOT_BRANCH", bot_branch)
    )
    out = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=60, check=True,
    )
    return json.loads(out.stdout)


class TestBotBranchUnderNode:
    @BOTH_BUILDS
    def test_unpatched_fails_when_the_profile_has_not_loaded(self, bot_branch):
        """The reported failure, word for word."""
        result = _run_bot_branch(bot_branch, {})

        assert result == {"error": "Cannot read properties of undefined (reading 'personaId')"}

    @BOTH_BUILDS
    def test_patched_sends_without_a_persona_id(self, bot_branch):
        patched, _ = patch_wa_js_source(bot_branch)

        result = _run_bot_branch(patched, {})

        assert result == {"message": {"type": "chat", "body": "hi", "messageSecret": "secret"}}

    @BOTH_BUILDS
    def test_patched_still_sends_the_persona_id_of_a_loaded_profile(self, bot_branch):
        patched, _ = patch_wa_js_source(bot_branch)

        result = _run_bot_branch(patched, {META_AI_CHAT: {"personaId": META_AI_PERSONA}})

        assert result["message"]["botPersonaId"] == META_AI_PERSONA


class TestBundleFile:
    def test_patches_the_bundle_in_place(self, fake_api_dir):
        api_dir, bundle = fake_api_dir

        ok, note = patch_wa_js_bundle(str(api_dir))

        assert ok is True
        assert "Patched" in note
        assert b"||{}).personaId" in bundle.read_bytes()

    def test_keeps_every_other_byte_including_line_endings(self, fake_api_dir):
        api_dir, bundle = fake_api_dir
        before = bundle.read_bytes()

        patch_wa_js_bundle(str(api_dir))

        after = bundle.read_bytes()
        assert after.replace(b"botPersonaId:(", b"botPersonaId:").replace(b"||{})", b"") == before

    def test_a_second_run_is_a_no_op(self, fake_api_dir):
        api_dir, bundle = fake_api_dir
        patch_wa_js_bundle(str(api_dir))
        first_pass = bundle.read_bytes()

        ok, note = patch_wa_js_bundle(str(api_dir))

        assert ok is True
        assert "already applied" in note
        assert bundle.read_bytes() == first_pass

    def test_a_missing_bundle_is_a_safe_no_op(self, tmp_path):
        ok, note = patch_wa_js_bundle(str(tmp_path))

        assert ok is False
        assert "not found" in note

    def test_a_bundle_that_moved_upstream_is_reported_and_untouched(self, fake_api_dir):
        api_dir, bundle = fake_api_dir
        bundle.write_bytes(b'"use strict";botPersonaId:lookup(e.id)?.personaId;')

        ok, note = patch_wa_js_bundle(str(api_dir))

        assert ok is False
        assert "did not match" in note
        assert bundle.read_bytes() == b'"use strict";botPersonaId:lookup(e.id)?.personaId;'


class TestEveryCallSiteAppliesIt:
    """setup_api.py (dev and CI), ApiSetupDialog (the end-user install and
    every launch) and build_api.py: a patch missing from one ships broken."""

    def test_setup_api(self, fake_api_dir):
        api_dir, bundle = fake_api_dir

        assert _load_setup_api()._patch_wa_js_bundle(str(api_dir)) is True

        assert b"||{}).personaId" in bundle.read_bytes()

    def test_setup_api_reports_a_missing_bundle(self, tmp_path):
        assert _load_setup_api()._patch_wa_js_bundle(str(tmp_path)) is False

    def test_api_setup_dialog(self, fake_api_dir):
        from ui.dialogs.api_setup import ApiSetupDialog
        api_dir, bundle = fake_api_dir

        ApiSetupDialog._apply_node_modules_patches(str(api_dir))

        assert b"||{}).personaId" in bundle.read_bytes()

    def test_build_api(self):
        src = (ROOT / "build_api.py").read_text(encoding="utf-8")
        patchers = src[src.index("for patcher in ("):src.index("patcher(api_dir)")]

        assert "canonical_setup._patch_wa_js_bundle," in patchers
