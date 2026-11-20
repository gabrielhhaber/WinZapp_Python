"""The page-side @lid -> number lookups of check-number-status and all-contacts,
run under Node against a fake WPP (client/api_patches/src/util/lidNumberPage.ts)."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "client/api_patches/src/util/lidNumberPage.ts"


def node():
    bundled = ROOT / "client/node" / ("node.exe" if os.name == "nt" else "node")
    return os.environ.get("WINZAPP_TEST_NODE") or (
        str(bundled) if bundled.exists() else shutil.which("node"))


HARNESS = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8').replace(/^export /gm, '');
function load(wpp) {
  const context = vm.createContext({window: {WPP: wpp}, Object, String});
  vm.runInContext(source, context);
  return context;
}
const phones = {'1@lid': {phoneNumber: {_serialized: '5511999990000@c.us'}},
                '2@lid': {phoneNumber: '5522988880000@c.us'},
                '3@lid': {phoneNumber: '5533@g.us'},
                '4@lid': {}};
const full = load({whatsapp: {
  ApiContact: {getPhoneNumber: wid => (phones[wid.id] || {}).phoneNumber},
  WidFactory: {createWid: id => ({id})},
  ContactStore: {get: id => phones[id]},
}});
assert.equal(full.pageNumberOfLid('1@lid'), '5511999990000@c.us');
assert.equal(full.pageNumberOfLid('2@lid'), '5522988880000@c.us');
assert.equal(full.pageNumberOfLid('3@lid'), '');
assert.equal(full.pageNumberOfLid('4@lid'), '');
assert.deepEqual(JSON.parse(JSON.stringify(full.pageNumbersOfLids(['1@lid', '2@lid', '3@lid', '4@lid', '5@lid']))),
  {'1@lid': '5511999990000@c.us', '2@lid': '5522988880000@c.us'});
// A page without WPP answers nothing instead of throwing.
const bare = load(undefined);
assert.equal(bare.pageNumberOfLid('1@lid'), '');
assert.deepEqual(JSON.parse(JSON.stringify(bare.pageNumbersOfLids(['1@lid']))), {});
console.log('lid pages OK');
"""


@pytest.mark.skipif(node() is None, reason="Node is not available")
def test_page_lookups_give_only_a_phone_form():
    done = subprocess.run([node(), "-e", HARNESS, str(SOURCE)],
                          capture_output=True, text=True, encoding="utf-8", timeout=20,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert done.returncode == 0, done.stderr
    assert "lid pages OK" in done.stdout


def test_the_controller_uses_the_page_functions():
    source = (ROOT / "client/api_patches/src/controller/deviceController.ts").read_text(encoding="utf-8")
    assert "page.evaluate(pageNumberOfLid, answered)" in source
    assert "pageNumbersOfLids," in source
