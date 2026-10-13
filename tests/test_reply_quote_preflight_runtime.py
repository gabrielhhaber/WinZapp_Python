"""Execute the actual preflight and response builder with a fake browser page."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'client/api_patches/src/util/quotedMessagePreflight.ts'


def node():
    bundled = ROOT / 'client/node' / ('node.exe' if os.name == 'nt' else 'node')
    return os.environ.get('WINZAPP_TEST_NODE') or (str(bundled) if bundled.exists() else shutil.which('node'))


HARNESS = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8').replace(/^export /gm, '');
const world = vm.createContext({window: {WPP: {chat: {}}}});
vm.runInContext(source, world);
let reads = 0;
const client = {page: {evaluate: expression => vm.runInContext(expression, world)}};
(async () => {
  world.window.WPP.chat.getMessageById = async id => { reads++; assert.equal(id, 'false_chat@c.us_ID'); return {id}; };
  assert.equal(await world.replyQuoteIsMissing(client, 'false_chat@c.us_ID'), false);
  world.window.WPP.chat.getMessageById = async () => { throw {code: 'msg_not_found'}; };
  assert.equal(await world.replyQuoteIsMissing(client, 'false_chat@c.us_ID'), true);
  for (const error of [new Error('Message X not found'), {code: 'TargetCloseError'}, {code: 'other'}]) {
    world.window.WPP.chat.getMessageById = async () => {throw error;};
    await assert.rejects(() => world.replyQuoteIsMissing(client, 'X'), e => e === error);
  }
  world.window.WPP.chat.getMessageById = async () => null;
  await assert.rejects(() => world.replyQuoteIsMissing(client, 'X'));
  let code, body;
  const res = {status: n => {code = n; return res;}, json: b => {body = b; return res;}};
  world.missingReplyQuoteResponse(res);
  assert.equal(code, 500); // old clients must NEVER strip the quote on a 4xx
  assert.equal(JSON.stringify(body), JSON.stringify({status:'error',code:'quoted_message_not_found',stage:'quote_resolution',sendAttempted:false}));
  assert.equal(reads, 1);
  console.log('preflight behavior OK');
})().catch(e => { console.error(e); process.exitCode = 1; });
"""


@pytest.mark.skipif(node() is None, reason='Node is not available')
def test_runtime_distinguishes_only_a_proven_missing_quote():
    done = subprocess.run([node(), '-e', HARNESS, str(RUNTIME)],
                          capture_output=True, text=True, encoding='utf-8', timeout=10)
    assert done.returncode == 0, done.stderr
    assert 'preflight behavior OK' in done.stdout


@pytest.mark.parametrize('name', ['replyMessage', 'sendMentioned'])
def test_preflight_is_wired_before_the_recipient_loop(name):
    # Structural guard: the behavioral tests above cannot detect a controller
    # moving this safe read after a send to an earlier recipient.
    source = (ROOT / 'client/api_patches/src/controller/messageController.ts').read_text(encoding='utf-8')
    part = source.split('export async function ' + name + '(', 1)[1].split('export async function ', 1)[0]
    assert part.index('replyQuoteIsMissing(') < part.index('for (const contato of phone)')
    assert part.index('missingReplyQuoteResponse(res)') < part.index('for (const contato of phone)')
    assert "!messageId.includes('status@broadcast')" in part
