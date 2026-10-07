"""Execute the shipped in-page source and HTTP handlers on synthetic models."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UTIL = ROOT / "client/api_patches/src/util/contactPresenceRuntime.ts"
CONTROLLER = ROOT / "client/api_patches/src/controller/deviceController.ts"
SESSION = ROOT / "client/api_patches/src/controller/sessionController.ts"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="Node required for offline runtime")


def _run(world):
    source = re.search(r"String\.raw`(.*?)`;", UTIL.read_text(encoding="utf-8"), re.S).group(1)
    controller = CONTROLLER.read_text(encoding="utf-8").split("export async function getLastSeen(", 1)[1]
    controller = "async function getLastSeen(" + controller.split("\nexport async function", 1)[0]
    controller = controller.replace("req: Request", "req").replace("res: Response", "res").replace(" as any", "")
    subscription = SESSION.read_text(encoding="utf-8").split("export async function subscribePresence(", 1)[1]
    subscription = "async function subscribePresence(" + subscription.split("\nexport async function", 1)[0]
    subscription = subscription.replace("req: Request", "req").replace("res: Response", "res").replace(" as any", "")
    subscription = re.sub(r"\((\w+): (?:string|any)\)", r"(\1)", subscription)
    harness = r"""
const vm = require('vm');
const input = JSON.parse(process.argv[1]);
const calls = { evaluate: 0, legacy: [], subscriptions: [] };
const model = input.world.model;
const window = { WPP: input.world.unsupported ? null : { whatsapp: {
  WidFactory: { createWid: id => id },
  PresenceStore: { get: id => input.world.storeThrows ? (()=>{throw Error('synthetic')})() : model },
  ChatStore: { get: id => input.world.chatFallback ? {presence: model} : null },
}, contact: input.world.noModern ? {} : {subscribePresence: async id => {
  calls.subscriptions.push(id);
  if (input.world.subscribeThrows) throw Error('synthetic');
  return input.world.emptySubscription ? [] : [{_serialized: id}];
}} }};
const page = { evaluate: async (expression, id) => {
  calls.evaluate++;
  return typeof expression === 'function'
    ? vm.runInNewContext('(' + expression.toString() + ')', {window})(id)
    : vm.runInNewContext(expression, {window});
}};
const res = { code: 0, body: null, status(code) { this.code = code; return this; },
  json(body) { this.body = body; } };
const req = {params: {phone: input.world.phone || '123'},
  body: {phone: input.world.phone || '123'}, logger: {error(){},info(){},warn(){}},
  client: {page: input.world.noPage ? null : page,
    getLastSeen: async id => { calls.legacy.push(id); return 1000; },
    subscribePresence: async id => { calls.legacy.push(id); }
  }};
const context = { CONTACT_PRESENCE_SOURCE: input.source,
  contactToArray: phone => [phone.includes('@') ? phone : phone + '@c.us'] };
vm.createContext(context);
vm.runInContext(input.controller + '\n' + input.subscription, context);
(async () => {
  if (input.world.subscription) await context.subscribePresence(req, res);
  else await context.getLastSeen(req, res);
  process.stdout.write(JSON.stringify({code: res.code, body: res.body, calls}));
})().catch(err=>{process.stderr.write(String(err)); process.exitCode=1;});
"""
    result = subprocess.run([NODE, "-e", harness, json.dumps({"world": world, "source": source, "controller": controller, "subscription": subscription})], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("value,expected", [(1000, 1000), (1700000000123, 1700000000), (False, None), (True, None), (None, None), (0, None), (-1, None), ("bad", None)])
def test_actual_route_reads_chatstate_timestamp(value, expected):
    result = _run({"model": {"hasData": True, "isOnline": False, "chatstate": {"type": "unavailable", "t": value}}})
    assert result["code"] == 200
    assert result["body"]["presence"]["lastSeen"] == expected
    assert result["calls"]["legacy"] == []


@pytest.mark.parametrize("state", ["available", "paused", "typing", "recording_audio"])
def test_online_never_returns_previous_offline_timestamp(state):
    result = _run({"model": {"hasData": True, "isOnline": True, "chatstate": {"type": state, "t": 1000}}})
    presence = result["body"]["presence"]
    assert presence["lastSeen"] is None and presence["isOnline"] is True
    assert presence["state"] == {"typing": "composing", "recording_audio": "recording"}.get(state, "available")


def test_privacy_denial_never_exposes_model_timestamp():
    result = _run({"model": {"hasData": True, "isOnline": False, "chatstate": {"type": "unavailable", "t": 1000, "deny": True}}})
    assert result["body"]["presence"]["restricted"] is True
    assert result["body"]["presence"]["lastSeen"] is None


@pytest.mark.parametrize("world", [{}, {"model": {"hasData": False}}, {"storeThrows": True}])
def test_pending_does_not_fall_back_to_legacy_or_claim_withholding(world):
    result = _run(world)
    assert result["body"]["presence"] == {"status": "pending"}
    assert result["calls"]["legacy"] == []


@pytest.mark.parametrize("phone,expected", [("123", "123@c.us"), ("123@c.us", "123@c.us"), ("123@s.whatsapp.net", "123@c.us"), ("456@lid", "456@lid")])
def test_legacy_only_when_snapshot_unsupported_preserves_identity(phone, expected):
    result = _run({"unsupported": True, "phone": phone})
    assert result["calls"]["legacy"] == [expected]
    assert result["body"] == {"status": "success", "response": 1000}


@pytest.mark.parametrize("phone", ["123@g.us", "123@newsletter", "bad", "123@lid@c.us", "123');throw Error('x')"])
def test_invalid_contact_rejected_before_browser_or_legacy_call(phone):
    result = _run({"phone": phone})
    assert result["code"] == 400
    assert result["calls"]["evaluate"] == 0 and result["calls"]["legacy"] == []


@pytest.mark.parametrize("world,code,legacy", [({}, 200, []), ({"emptySubscription": True}, 500, []), ({"subscribeThrows": True}, 500, []), ({"noModern": True}, 200, ["123@c.us"]), ({"noPage": True}, 200, ["123@c.us"])])
def test_subscription_must_confirm_success_or_report_failure(world, code, legacy):
    result = _run({**world, "subscription": True})
    assert result["code"] == code and result["calls"]["legacy"] == legacy


def test_runtime_is_in_all_four_shipping_manifests():
    for path in ("setup_api.py", "build.py", "client/ui/dialogs/api_setup.py", "tests/test_api_patches_in_sync.py"):
        assert '"src/util/contactPresenceRuntime.ts"' in (ROOT / path).read_text(encoding="utf-8")
