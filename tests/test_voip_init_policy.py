"""Execute the shared page policy under Node with fake backends, no page/device.

The TS helper is serialized by Puppeteer in production. Its few annotations
are removed here; the function body itself executes, not a reimplementation.
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / 'client/api_patches/src/util/voipInitPolicy.ts'

HARNESS = r'''
const assert = require('node:assert/strict');
const out = {};
const fresh = () => { global.window = {}; installVoipInitPolicyInPage(); };
const backend = (state, counters = {}) => ({
  VoipInitEventEmitter: {
    getIsVoipInited: () => state.ready,
    getDidVoipInitError: () => state.failed,
    getVoipInitFailureClass: () => state.failure,
    canRetryWAWebVoipInitAfterFailure: () => state.retry,
  },
  initWAWebVoip: async () => { counters.init = (counters.init || 0) + 1; },
  retryWAWebVoipInitAfterFailure: async () => { counters.retry = (counters.retry || 0) + 1; },
});
(async () => {
  fresh();
  const fatal = { ready: false, failed: true, failure: 'wasm_contract_mismatch', retry: false };
  const counts = {};
  const b = backend(fatal, counts);
  await assert.rejects(window.__winzappInitializeVoip(b, 'warmup'), /wasm_contract_mismatch/);
  await assert.rejects(window.__winzappInitializeVoip(b, 'call'), /wasm_contract_mismatch/);
  assert.deepEqual(counts, {});
  out.fatalBeforeInit = true;

  fresh();
  const s = { ...fatal, failed: false, retry: true };
  const c = {};
  const allowed = backend(s, c);
  allowed.initWAWebVoip = async () => { c.init = (c.init || 0) + 1; s.failed = true; };
  allowed.retryWAWebVoipInitAfterFailure = async () => { c.retry = (c.retry || 0) + 1; s.ready = true; };
  await Promise.all([window.__winzappInitializeVoip(allowed, 'warmup'),
                     window.__winzappInitializeVoip(allowed, 'call')]);
  assert.deepEqual(c, { init: 1, retry: 1 });
  await window.__winzappInitializeVoip(allowed, 'again');
  assert.deepEqual(c, { init: 1, retry: 1 });
  out.sharedReadyFlight = true;

  fresh();
  const later = { ...fatal, failed: false };
  const post = backend(later);
  post.initWAWebVoip = async () => { later.failed = true; };
  await assert.rejects(window.__winzappInitializeVoip(post, 'call'), /wasm_contract_mismatch/);
  assert.equal(window.__winzappVoipInitFlight, undefined);
  out.fatalAfterInit = true;

  fresh();
  const legacyCounts = {};
  const legacy = backend({ ready: false, failed: true }, legacyCounts);
  await window.__winzappInitializeVoip(legacy, 'warmup');
  await window.__winzappInitializeVoip(legacy, 'call');
  assert.equal(legacyCounts.retry, 1);
  out.legacyRetryBounded = true;

  fresh();
  const throwing = { VoipInitEventEmitter: new Proxy({}, { get: () => () => { throw Error('private data'); } }) };
  assert.deepEqual(window.__winzappReadVoipInitState(throwing),
    { ready: null, failed: null, canRetry: null, voipInitFailureClass: '' });
  assert.equal(window.__winzappReadVoipInitState(backend({ failure: 'a private contact' })).voipInitFailureClass, 'other');
  out.optionalGettersAndRedaction = true;

  fresh();
  const owner = { ready: false, failed: true, failure: 'wasm_contract_mismatch', retry: false };
  owner.getIsVoipInited = function () { return this.ready; };
  owner.getDidVoipInitError = function () { return this.failed; };
  owner.getVoipInitFailureClass = function () { return this.failure; };
  owner.canRetryWAWebVoipInitAfterFailure = function () { return this.retry; };
  assert.equal(window.__winzappReadVoipInitState(owner).canRetry, false);
  await assert.rejects(window.__winzappInitializeVoip(owner, 'call'), /wasm_contract_mismatch/);
  out.moduleReceiver = true;

  fresh();
  const sync = { initWAWebVoip() { throw Error('synchronous failure'); } };
  await assert.rejects(window.__winzappInitializeVoip(sync, 'warmup'), /synchronous failure/);
  assert.equal(window.__winzappVoipInitFlight, undefined);
  sync.initWAWebVoip = async () => {};
  await window.__winzappInitializeVoip(sync, 'call');
  out.failedFlightReleased = true;

  fresh();
  const once = { initWAWebVoip() { throw Error('cannot load module more than once per process'); } };
  await window.__winzappInitializeVoip(once, 'warmup');
  assert.equal(window.__winzappVoipLoadedOnce, true);
  once.initWAWebVoip = () => assert.fail('second wasm load');
  await window.__winzappInitializeVoip(once, 'call');
  out.onceOnlyWasm = true;

  fresh();
  let fallbackCalls = 0;
  await window.__winzappInitializeVoip({}, 'call', async () => { fallbackCalls++; });
  assert.equal(fallbackCalls, 1);
  out.explicitBackendFallback = true;

  fresh();
  const entryCounts = {};
  const entryBackend = backend(fatal, entryCounts);
  window.WPP = { call: { enableCallInterface: async () => {} },
    whatsapp: { functions: { requireVoipJsBackend: async () => entryBackend } } };
  window.setTimeout = () => assert.fail('fatal mismatch must not sleep');
  const warnings = [];
  const page = { evaluate: async (fn, ...args) => fn(...args) };
  assert.equal(await warmCallVoipRuntime({ page, session: 'synthetic' },
    { warn: text => warnings.push(text) }), false);
  assert.match(warnings[0], /wasm_contract_mismatch/);
  const actionReadiness = () => {
    const win = window;
    const delay = () => assert.fail('fatal call must not sleep');
    const getNativeVoipStack = async () => ({});
    const isVoipInitialized = () => true;
    ENSURE_BLOCK
    return ensureVoipRuntimeReady();
  };
  await assert.rejects(actionReadiness(), /wasm_contract_mismatch/);
  assert.deepEqual(entryCounts, {});
  out.entrypointsFailFast = true;
  console.log(JSON.stringify(out));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''


@pytest.fixture(scope='module')
def result(tmp_path_factory):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node not available')
    bridge = (ROOT / 'client/api_patches/src/util/callMediaBridge.ts').read_text(encoding='utf-8')
    start = bridge.index('export async function warmCallVoipRuntime(')
    warmup = bridge[start:bridge.index('\n}\n', start) + 3].replace('export ', '', 1)
    controller = (ROOT / 'client/api_patches/src/controller/callController.ts').read_text(encoding='utf-8')
    start = controller.index('const ensureVoipRuntimeReady =')
    readiness = controller[start:controller.index('\n      const isVoipInitError', start)]
    source = POLICY.read_text(encoding='utf-8').replace('export function ', 'function ')
    source += '\n' + warmup + '\n' + HARNESS.replace('ENSURE_BLOCK', readiness)
    for annotation in (' as any', ': any', ': string', ': void', ': number',
                       ': Promise<boolean>', ': Promise<any>'):
        source = source.replace(annotation, '')
    script = tmp_path_factory.mktemp('voip-policy') / 'policy.cjs'
    script.write_text(source, encoding='utf-8')
    proc = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=15,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize('scenario', [
    'fatalBeforeInit', 'sharedReadyFlight', 'fatalAfterInit', 'legacyRetryBounded',
    'optionalGettersAndRedaction', 'moduleReceiver', 'failedFlightReleased',
    'onceOnlyWasm', 'explicitBackendFallback', 'entrypointsFailFast',
])
def test_shared_policy_scenario(result, scenario):
    assert result[scenario]
