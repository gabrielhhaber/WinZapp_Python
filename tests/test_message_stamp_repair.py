"""get-messages repairs a quote stand-in's missing time from the IndexedDB.

Runs client/api_patches/src/util/messageStampRepair.ts itself under Node,
with a fake page and a fake IndexedDB shaped like WhatsApp Web's
`model-storage` database (measured live, 2026-11-19).
"""

import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "client/api_patches/src/util/messageStampRepair.ts"
CONTROLLER = ROOT / "client/api_patches/src/controller/deviceController.ts"


def node():
    bundled = ROOT / "client/node" / ("node.exe" if os.name == "nt" else "node")
    return os.environ.get("WINZAPP_TEST_NODE") or (
        str(bundled) if bundled.exists() else shutil.which("node"))


HARNESS = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8').replace(/^export /gm, '');

// A fake model-storage with one stored row; requests answer asynchronously.
function fakeIndexedDB(rows, opts = {}) {
  const later = fn => setTimeout(fn, 0);
  return {
    opened: 0,
    open(name) {
      this.opened++;
      assert.equal(name, 'model-storage');
      const request = {};
      later(() => {
        if (opts.openFails) return request.onerror && request.onerror();
        request.result = {
          closed: false,
          close() { this.closed = true; },
          transaction(store, mode) {
            assert.equal(store, 'message'); assert.equal(mode, 'readonly');
            const tx = {objectStore: () => ({get(id) {
              const read = {};
              later(() => { read.result = rows[id]; read.onsuccess && read.onsuccess(); });
              return read;
            }})};
            later(() => later(() => tx.oncomplete && tx.oncomplete()));
            return tx;
          },
        };
        request.onsuccess && request.onsuccess();
      });
      return request;
    },
  };
}

const STAND_IN = 'false_g@g.us_STANDIN_a@lid';
const LOST = 'false_g@g.us_LOST_a@lid';
const world = vm.createContext({setTimeout, clearTimeout, Promise, JSON, Number, Math, Object});
vm.runInContext(source, world);
world.indexedDB = fakeIndexedDB({[STAND_IN]: {t: 1795095053, ack: 3}});
const client = {page: {evaluate: expression => vm.runInContext(expression, world)}};
const logs = [];
const logger = {info: m => logs.push(m), warn: m => logs.push('WARN ' + m)};

(async () => {
  const messages = [
    {id: 'false_g@g.us_OLDER_a@lid', t: 1795095019, timestamp: 1795095019, ack: 3},
    {id: STAND_IN, clientReceivedTsMillis: 1795095053648},
    {id: {_serialized: LOST}, clientReceivedTsMillis: 1795095060999},
    {id: 'false_g@g.us_NONE_a@lid'},
  ];
  assert.deepEqual(world.messagesWithoutStamp(messages),
                   [STAND_IN, LOST, 'false_g@g.us_NONE_a@lid']);

  const out = await world.repairMissingStamps(client, messages, logger);
  assert.equal(out, messages);
  // The stored row is the truth: its time and its ack.
  assert.equal(messages[1].t, 1795095053); assert.equal(messages[1].timestamp, 1795095053);
  assert.equal(messages[1].ack, 3);
  // No stored row: the stand-in's receive time, and no invented ack.
  assert.equal(messages[2].t, 1795095060); assert.equal(messages[2].ack, undefined);
  // Nothing to go by: left without a time for the client to handle.
  assert.equal(messages[3].t, undefined);
  // A message that had its time is untouched.
  assert.equal(messages[0].t, 1795095019);
  assert.ok(logs.some(m => m.includes('3 message(s) without a time; 1 stored, 2 repaired')));
  assert.ok(!logs.some(m => m.includes('g@g.us')), 'no id in the log');

  // Nothing missing: the page is not touched at all.
  const opened = world.indexedDB.opened;
  await world.repairMissingStamps(client, [{id: 'x', t: 5}], logger);
  assert.equal(world.indexedDB.opened, opened);
  assert.equal(await world.repairMissingStamps(client, null, logger), null);

  // The database cannot be opened: the receive time still applies.
  world.indexedDB = fakeIndexedDB({}, {openFails: true});
  const unopened = [{id: STAND_IN, clientReceivedTsMillis: 1795095053648}];
  await world.repairMissingStamps(client, unopened, logger);
  assert.equal(unopened[0].t, 1795095053);

  // The evaluate itself fails: same fallback, never a failed page.
  const broken = {page: {evaluate: async () => { throw new Error('Execution context destroyed'); }}};
  const failed = [{id: STAND_IN, clientReceivedTsMillis: 1795095053648}];
  await world.repairMissingStamps(broken, failed, logger);
  assert.equal(failed[0].t, 1795095053);
  assert.ok(logs.some(m => m.startsWith('WARN ')));
  console.log('stamp repair OK');
})().catch(e => { console.error(e); process.exitCode = 1; });
"""


@pytest.mark.skipif(node() is None, reason="Node is not available")
def test_stand_ins_take_the_stored_time_or_their_receive_time():
    done = subprocess.run([node(), "-e", HARNESS, str(RUNTIME)],
                          capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert done.returncode == 0, done.stderr
    assert "stamp repair OK" in done.stdout


def _get_messages_route() -> str:
    source = CONTROLLER.read_text(encoding="utf-8")
    start = source.index("export async function getMessages(")
    return source[start:source.index("\nexport ", start + 1)]


def test_get_messages_repairs_before_answering():
    route = _get_messages_route()
    repair = route.index("response = await repairMissingStamps(req.client, response, req.logger)")
    assert repair < route.index("res.status(200).json(")


def test_unanchored_paging_orders_a_stand_in_by_its_receive_time():
    # The page-side sort and slice run before the repair: a stand-in stamped 0
    # sorted first and the slice could drop it.
    route = _get_messages_route()
    stamp = route[route.index("const stampOf"):route.index("const fetchBatch")]
    assert "clientReceivedTsMillis" in stamp
