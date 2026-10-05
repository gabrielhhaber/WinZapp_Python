"""Status posts: WhatsApp's status sender must accept WA-JS's positional call.

Reported 2026-09-23: every status post (text, video, audio) failed with
messageSendResult=ERROR_UNKNOWN. WA-JS 4.6.0 wraps
WAWebSendMsgJob.encryptAndSendMsg and, for status@broadcast, calls
encryptAndSendStatusMsg(msg, proto, reporter) positionally inside a try/catch
that returns null on any error. Current WhatsApp takes one object. The first
shim wrapped encryptAndSendMsg, which only works while it is the outermost
wrapper; measured over CDP, WA-JS's wrapper had ended up outside it and a fake
record reached the sender with 3 positional arguments. The adapter now sits
on encryptAndSendStatusMsg itself, which WA-JS reads live (its exportModule
getter), so wrapper order no longer matters.

The adapter's install() is run verbatim under Node against a fake module, a
live getter shaped like WA-JS's exportModule, and a recording sender - the
same approach as tests/test_call_state_poll_watchdog.py.
"""

import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SESSION_UTIL = ROOT / "client" / "api_patches" / "src" / "util" / "createSessionUtil.ts"
SOURCE = SESSION_UTIL.read_text(encoding="utf-8")


def _node():
    bundled = ROOT / "client" / "node" / "node.exe"
    if bundled.exists():
        return str(bundled)
    return shutil.which("node")


def _install_block() -> str:
    start = SOURCE.index("      const install = (): string => {",
                         SOURCE.index("async function restoreStatusSender("))
    end = SOURCE.index("      const now = install();", start)
    block = SOURCE[start:end]
    block = block.replace("(window as any)", "window").replace("(adapted as any)", "adapted")
    block = block.replace("(): string =>", "() =>")
    block = re.sub(r"function \(this: any, first: any, \.\.\.rest: any\[\]\)",
                   "function (first, ...rest)", block)
    return block


_HARNESS = r"""
const calls = [];
function real(o) { calls.push({ argc: arguments.length, o, self: this }); return Promise.resolve('sent'); }
let mod = { encryptAndSendStatusMsg: real };
const window = { WPP: { loader: { moduleRequire: (n) =>
  n === 'WAWebEncryptAndSendStatusMsg' ? mod : undefined } } };
// WA-JS's exportModule: a getter that re-reads the module export every time.
const p = {};
Object.defineProperty(p, 'encryptAndSendStatusMsg', { get: () => mod.encryptAndSendStatusMsg });
BLOCK
(async () => {
  const out = {};
  out.first = install();
  const adapted = mod.encryptAndSendStatusMsg;
  out.second = install();
  out.sameAfterReinstall = mod.encryptAndSendStatusMsg === adapted;

  const rec = { data: { to: 'status@broadcast' } };
  out.returned = await (0, p.encryptAndSendStatusMsg)(rec, 'PROTO', 'REP');
  out.positional = { argc: calls[0].argc, keys: Object.keys(calls[0].o),
    rec: calls[0].o.sendMsgRecord === rec, proto: calls[0].o.msgProtobuf,
    rep: calls[0].o.metricsReporter };

  const obj = { sendMsgRecord: rec, msgProtobuf: 'P2', metricsReporter: 'R2' };
  const self = { tag: 'self' };
  await mod.encryptAndSendStatusMsg.call(self, obj);
  out.objectPassthrough = calls[1].o === obj && calls[1].argc === 1 && calls[1].self === self;

  mod = { encryptAndSendStatusMsg: (...r) => real(...r) };
  out.wrapped = install();
  out.wrappedAdapted = !!mod.encryptAndSendStatusMsg.__winzappPositionalAdapter;

  mod = { encryptAndSendStatusMsg: function (a, b, c) {} };
  out.legacy = install();
  out.legacyAdapted = !!mod.encryptAndSendStatusMsg.__winzappPositionalAdapter;

  // WA-JS 4.6.1's own call site (module 71459): the export's .length picks
  // the shape, and it reads the export live, like this getter.
  const wajs461 = async (r, proto, rep) =>
    1 === p.encryptAndSendStatusMsg.length
      ? await (0, p.encryptAndSendStatusMsg)({ metricsReporter: rep, msgProtobuf: proto, sendMsgRecord: r })
      : await (0, p.encryptAndSendStatusMsg)(r, proto, rep);
  const seen = () => ({ n: calls.length, argc: calls[0].argc,
    rec: calls[0].o.sendMsgRecord === rec, proto: calls[0].o.msgProtobuf,
    rep: calls[0].o.metricsReporter });
  calls.length = 0;
  mod = { encryptAndSendStatusMsg: real };
  install();
  await wajs461(rec, 'PROTO', 'REP');
  out.v461WithAdapter = seen();
  calls.length = 0;
  mod = { encryptAndSendStatusMsg: (...r) => real(...r) };
  install();
  await wajs461(rec, 'PROTO', 'REP');
  out.v461WrappedWithAdapter = seen();
  calls.length = 0;
  mod = { encryptAndSendStatusMsg: real };
  await wajs461(rec, 'PROTO', 'REP');
  out.v461NoAdapter = seen();

  mod = undefined;
  out.notReady = install();
  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    node = _node()
    if not node:
        pytest.skip("Node is not available")
    script = tmp_path_factory.mktemp("status_adapter") / "harness.js"
    script.write_text(_HARNESS.replace("BLOCK", _install_block()), encoding="utf-8")
    proc = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_positional_call_reaches_the_sender_as_one_object(result):
    assert result["positional"] == {
        "argc": 1, "keys": ["sendMsgRecord", "msgProtobuf", "metricsReporter"],
        "rec": True, "proto": "PROTO", "rep": "REP",
    }
    assert result["returned"] == "sent"


def test_whatsapps_own_object_call_passes_through_with_its_this(result):
    assert result["objectPassthrough"] is True


def test_install_reports_what_it_did_and_is_idempotent(result):
    assert result["first"] == "installed (sender length 1)"
    assert result["second"] == "already installed"
    assert result["sameAfterReinstall"] is True


def test_a_wrapped_sender_of_length_zero_is_still_adapted(result):
    """WA-JS's wrapFunction shape reports length 0; skipping it while logging
    "installed" would send the next person the wrong way."""
    assert result["wrapped"] == "installed (sender length 0)"
    assert result["wrappedAdapted"] is True


def test_a_positional_sender_is_skipped_and_says_so(result):
    assert result["legacy"] == "skipped: sender takes 3 positional args"
    assert result["legacyAdapted"] is False


@pytest.mark.parametrize("case", ["v461WithAdapter", "v461WrappedWithAdapter",
                                  "v461NoAdapter"])
def test_wa_js_4_6_1s_own_object_call_reaches_the_sender_once_as_an_object(result, case):
    """4.6.1 picks the call shape from the export's .length. The adapter has
    length 1, so on 4.6.1 it receives the object call and passes it through:
    one send, one argument, nothing double-wrapped. The same code keeps
    serving 4.6.0's positional call (the tests above)."""
    assert result[case] == {"n": 1, "argc": 1, "rec": True,
                            "proto": "PROTO", "rep": "REP"}


def test_not_ready_yet_keeps_the_retry_going(result):
    assert result["notReady"] == ""


def test_it_no_longer_wraps_encrypt_and_send_msg():
    body = SOURCE[SOURCE.index("async function restoreStatusSender("):]
    body = body[: body.index("\n}\n")]
    assert "WAWebSendMsgJob" not in body


def test_the_retry_reports_how_it_ended():
    body = SOURCE[SOURCE.index("async function restoreStatusSender("):]
    body = body[: body.index("\n}\n")]
    assert "[browser-evaluate] status sender shim:" in body


def test_it_is_still_installed_at_session_start():
    assert SOURCE.count("restoreStatusSender(client.page, req.logger, session)") == 2
