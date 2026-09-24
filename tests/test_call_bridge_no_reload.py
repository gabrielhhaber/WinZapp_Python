"""The call media bridge must never reload WhatsApp Web to install itself.

Measured 2026-09-24 on two consecutive starts after updating an alpha:
WhatsApp Web reloaded itself right after its first load, the bridge's
"priming" page.reload() landed on top of it, and the renderer stopped
answering anything (CDP Runtime.evaluate, even a trace start) while idle,
with no dialog, no freeze and no pending navigation to blame. The session
stayed INITIALIZING and WinZapp went offline; removing the reload alone
brought it straight back.

The hooks still have to exist before WhatsApp's modules evaluate, so start.js
registers them as a new-document script before the first goto() (asserted in
tests/test_pinned_page_interception.py). This file runs
registerCallMediaBridgeBeforeLoad() under Node and pins that no reload is left.
"""

import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "client" / "api_patches" / "src" / "util" / "callMediaBridge.ts"
SOURCE = BRIDGE.read_text(encoding="utf-8")


def _node():
    bundled = ROOT / "client" / "node" / "node.exe"
    if bundled.exists():
        return str(bundled)
    return shutil.which("node")


def _function(name: str) -> str:
    start = SOURCE.index(f"export async function {name}(")
    end = SOURCE.index("\n}\n", start) + 3
    block = SOURCE[start:end].replace("export ", "", 1)
    block = re.sub(r"\((\w+): any\)", r"(\1)", block)
    return block.replace("): Promise<boolean> {", ") {")


_HARNESS = r"""
const installCallMediaBridgeInPage = function installCallMediaBridgeInPage() {};
const linuxAudioRelayActive = () => false;
BLOCK
(async () => {
  const calls = [];
  const page = {
    evaluateOnNewDocument: async (fn, arg) => { calls.push({ fn: fn.name, arg }); },
    reload: async () => { calls.push({ fn: 'RELOAD' }); },
  };
  const out = {};
  out.first = await registerCallMediaBridgeBeforeLoad(page);
  out.second = await registerCallMediaBridgeBeforeLoad(page);
  out.calls = calls;
  out.flag = page.__winzappCallMediaNewDocumentInstalled === true;
  out.noPage = await registerCallMediaBridgeBeforeLoad(null);
  out.noMethod = await registerCallMediaBridgeBeforeLoad({});
  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    node = _node()
    if not node:
        pytest.skip("Node is not available")
    script = tmp_path_factory.mktemp("bridge") / "harness.js"
    script.write_text(
        _HARNESS.replace("BLOCK", _function("registerCallMediaBridgeBeforeLoad")),
        encoding="utf-8",
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_it_registers_the_bridge_as_a_new_document_script_once(result):
    assert result["first"] is True and result["second"] is True
    assert result["calls"] == [{"fn": "installCallMediaBridgeInPage", "arg": False}]
    assert result["flag"] is True


def test_it_never_reloads(result):
    assert all(call["fn"] != "RELOAD" for call in result["calls"])


def test_without_a_usable_page_it_reports_false(result):
    assert result["noPage"] is False
    assert result["noMethod"] is False


def test_no_reload_is_left_anywhere_in_the_bridge():
    code = [line for line in SOURCE.splitlines()
            if "reload(" in line and not line.lstrip().startswith(("//", "*"))]
    assert not code, f"the bridge must not reload the page: {code}"


def test_ensure_reads_the_flag_from_the_page_the_early_registration_set():
    start = SOURCE.index("export async function ensureCallMediaBridge(")
    body = SOURCE[start:SOURCE.index("\n}\n", start)]
    assert "if (!page.__winzappCallMediaNewDocumentInstalled) {" in body
    assert "await registerCallMediaBridgeBeforeLoad(page);" in body
    assert "__winzappCallMediaPrimed" not in body


def test_the_dom_observer_survives_a_document_start_install():
    """At document start documentElement can be null; observe() would throw
    inside its try and the DOM-change scan would never attach."""
    assert ".observe(document.documentElement || document, {" in SOURCE
