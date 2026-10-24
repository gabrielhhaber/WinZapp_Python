'use strict';
// Actual provider exposure/registration and actual TS methods, all in fake VMs.
const fs = require('fs'), path = require('path'), vm = require('vm'), crypto = require('crypto');
const {EventEmitter} = require('events');
const input = JSON.parse(process.argv[2]), ts = require(input.compiler);
const root = path.resolve(__dirname, '..'), timers = new Map(); let nextTimer = 0;
const host = vm.createContext({setTimeout(fn) {timers.set(++nextTimer, fn); return nextTimer;},
  clearTimeout(id) {timers.delete(id);}});
const modules = new Map();
function load(name) {
  if (modules.has(name)) return modules.get(name);
  const file = path.join(root, 'client/api_patches/src/util', name + '.ts');
  const emitted = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: {target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS}, reportDiagnostics: true});
  if (emitted.diagnostics.some(d => d.category === ts.DiagnosticCategory.Error)) throw Error('Transpile failure');
  const exports = {}; modules.set(name, exports);
  const run = vm.runInContext('(function(exports,require){' + emitted.outputText + '\n})', host);
  run(exports, spec => load(path.basename(spec))); return exports;
}
function declarations(file, names) {
  const source = fs.readFileSync(file, 'utf8'), ast = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true), found = {};
  function walk(n) {
    if (ts.isMethodDeclaration(n) && names.includes(n.name.getText(ast))) found[n.name.getText(ast)] = n;
    ts.forEachChild(n, walk);
  }
  walk(ast); return {ast, found, source};
}
const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/group_presence_provider.json'), 'utf8'));
const version = input.version || '2.3.5', expected = fixture.versions[version];
let fragments = expected.fragments;
if (input.provider) {
  const providerFile = path.join(input.provider, expected.source_path);
  const provider = declarations(providerFile, ['onPresenceChanged', 'registerEvent', 'afterPageScriptInjected']);
  const body = provider.found.afterPageScriptInjected.body.statements;
  fragments = {
    functions: body.find(n => ts.isVariableStatement(n) && n.getText(provider.ast).startsWith('const functions')).getText(provider.ast),
    exposing: body.find(n => ts.isForOfStatement(n)).getText(provider.ast),
    registerEvent: provider.found.registerEvent.getText(provider.ast),
    onPresenceChanged: provider.found.onPresenceChanged.getText(provider.ast),
  };
}
for (const [key, source] of Object.entries(fragments)) {
  if (crypto.createHash('sha256').update(source).digest('hex') !== expected.fragment_sha256[key]) throw Error('Provider fixture drift');
}
host.EventEmitter = EventEmitter; host.exposed_enum_1 = {ExposedFn: {onPresenceChanged: 'onPresenceChanged'}};
vm.runInContext('class Provider {constructor(page,session){this.page=page;this.session=session;this.listenerEmitter=new EventEmitter();}'
  + 'log(){} async initialize(){' + fragments.functions + fragments.exposing + '}'
  + fragments.registerEvent + fragments.onPresenceChanged
  + '} this.Provider=Provider;', host);
const server = declarations(path.join(root, 'client/api_patches/src/util/createSessionUtil.ts'),
  ['onPresenceChanged', 'onGroupPresenceBridge']);
const code = 'class Forwarder {' + Object.values(server.found).map(n => n.getText(server.ast)).join('\n') + '} this.Forwarder=Forwarder;';
Object.assign(host, load('contactPresenceBridge'));
const groupFile = path.join(root, 'client/api_patches/src/util/groupPresenceBridge.ts');
if (fs.existsSync(groupFile)) Object.assign(host, load('groupPresenceBridge'));
vm.runInContext(ts.transpileModule(code, {compilerOptions: {target: ts.ScriptTarget.ES2020}}).outputText, host);
const io = [], hooks = [], logs = [];
host.callWebHook = (client, req, name, event) => hooks.push({owner: client.session, request: req.label, event: JSON.parse(JSON.stringify(event))});
function makeStore() {
  const value = new EventEmitter(); value.models = new Map();
  value.get = id => value.models.get(String(id)); value.getModelsArray = () => [...value.models.values()]; return value;
}
let store = makeStore();
function model(id = '120-123@g.us') {
  const m = new EventEmitter(); m.id = {_serialized: id, toString() {return id;}};
  m.typingUserIds = []; m.recordingUserIds = []; return m;
}
let current = model(); store.models.set(String(current.id), current);
const queue = [], window = {WPP: {isFullReady: true, whatsapp: {PresenceStore: store,
  WidFactory: {createWid: id => id}, ChatStore: {get: () => null}}}};
const browser = vm.createContext({window, Date: {now: () => 1700000000123}, queueMicrotask: fn => queue.push(fn)});
const pageEvents = new Map(), bindings = {};
let evaluateCount = 0, releaseOld = null;
const page = {closed: false, bindings, isClosed() {return this.closed;},
  on(name, fn) {if (!pageEvents.has(name)) pageEvents.set(name, []); pageEvents.get(name).push(fn);},
  async exposeFunction(name, fn) {if (bindings[name]) throw Error('Already bound'); bindings[name] = fn; window[name] = fn;},
  async evaluate(expression, ...args) {
    evaluateCount++; browser.args = args;
    const run = () => vm.runInContext(typeof expression === 'string' ? expression : '(' + expression.toString() + ')(...args)', browser, {timeout: 1000});
    if (input.case === 'late_install' && !releaseOld && typeof expression === 'string' && expression.includes('__winzappGroupPresenceBridge')) {
      return new Promise(resolve => {releaseOld = () => resolve(run());});
    }
    return run();
  }};
const first = new host.Provider(page, 'first'), second = new host.Provider(page, 'second');
const request = (client, label = client.session) => ({label, io: {emit: (name, event) => io.push({owner: label, name, event: JSON.parse(JSON.stringify(event))})},
  logger: {warn: value => logs.push(value), info() {}}});
const req1 = request(first), req2 = request(second), f = new host.Forwarder();
async function settle() {
  for (let i = 0; i < 35; i++) {
    while (queue.length) queue.shift()();
    await Promise.resolve();
  }
}
async function setup(client, req, legacy = true) {
  if (legacy) await client.initialize();
  host.installContactPresenceBridge(client, req);
  if (legacy) await f.onPresenceChanged(client, req);
  await f.onGroupPresenceBridge(client, req); await settle();
}
function change(m = current, state = 'typing') {
  m.typingUserIds = state === 'typing' ? ['123@c.us'] : [];
  m.recordingUserIds = state === 'recording_audio' ? ['123@c.us'] : [];
  m.emit('change:typingUserIds', m); store.emit('change:typingUserIds', m);
  m.emit('change:recordingUserIds', m); store.emit('change:recordingUserIds', m);
}
function emitPage(name) {for (const fn of pageEvents.get(name) || []) fn();}
(async () => {
  if (input.case === 'partial_attach') {
    const on = store.on.bind(store);
    store.on = (name, fn) => {if (name === 'change:recordingUserIds') throw Error('PRIVATE_ATTACH'); return on(name, fn);};
  }
  if (input.case === 'unsupported') store.off = undefined;
  if (input.case === 'retry' || input.case === 'replacement_retry' || input.case === 'close_retry') window.WPP.isFullReady = false;
  await setup(first, req1, input.case !== 'no_legacy');
  const binding = bindings.onPresenceChanged;
  const oldGeneration = window.__winzappGroupPresenceBridge?.generation;
  if (['owner', 'replacement_retry', 'late_install', 'old_generation'].includes(input.case)) await setup(second, req2);
  if (input.case === 'late_install') {releaseOld(); await settle();}
  if (input.case === 'old_generation') {
    bindings.__winzappGroupPresenceChanged({generation: oldGeneration, event: {
      id: '120-123@g.us', isOnline: false, isGroup: true, isUser: false, t: 1,
      state: 'typing', participants: [{id: '123@c.us', state: 'typing', shortName: ''}]}});
    await settle();
  }
  let oldStoreListeners = null;
  if (input.case === 'replacement_store') {
    const oldStore = store, oldModel = current;
    store = makeStore(); current = model(); store.models.set(String(current.id), current);
    window.WPP.whatsapp.PresenceStore = store;
    await setup(first, req1);
    oldModel.typingUserIds = ['456@lid']; oldStore.emit('change:typingUserIds', oldModel);
    oldStoreListeners = oldStore.eventNames().reduce((n, name) => n + oldStore.listenerCount(name), 0);
  }
  if (input.case === 'close' || input.case === 'close_retry') {page.closed = true; emitPage('close');}
  if (input.case === 'local_failure') {
    const emit = req1.io.emit; let failed = false;
    req1.io.emit = (...args) => {if (!failed) {failed = true; throw Error('PRIVATE_DELIVERY');} return emit(...args);};
  }
  if (input.case === 'page_move') first.page = {isClosed: () => false};
  if (input.case === 'same_client_request') await setup(first, request(first, 'replacement_req'));
  if (input.case === 'old_model') {const old = current; current = model(); store.models.set(String(current.id), current); change(old);}
  else if (input.case === 'remove') {store.models.delete(String(current.id)); store.emit('remove', current); change();}
  else if (input.case === 'queued_remove') {change(); store.models.delete(String(current.id)); store.emit('remove', current);}
  else if (input.case === 'reset') {change(); store.emit('reset');}
  else if (input.case === 'reset_fallback') {
    change(); await settle(); store.emit('reset');
    window.onPresenceChanged({id: '120-123@g.us', isGroup: true, isUser: false, isOnline: false, state: 'paused', t: 2,
      participants: [{id: '123@c.us', state: 'paused', shortName: ''}]});
  }
  else if (input.case === 'late_add') {
    current = model('456-789@g.us'); current.typingUserIds = ['456@lid'];
    store.models.set(String(current.id), current); store.emit('add', current);
  }
  else if (input.case === 'reload') {emitPage('load'); await settle(); change();}
  else if (input.case === 'recording') change(current, 'recording_audio');
  else if (input.case === 'paused') {change(); await settle(); change(current, 'paused');}
  else if (input.case === 'duplicate') {change(); await settle(); change();}
  else if (input.case === 'private_scope') {const other = model('123@c.us'); store.models.set(String(other.id), other); change(other);}
  else if (input.case.startsWith('envelope_')) {
    const event = {id: '120-123@g.us', isOnline: false, isGroup: true, isUser: false, state: 'typing', t: 1,
      participants: [{id: '123@c.us', state: 'typing', shortName: ''}]};
    const variant = input.case.slice(9);
    if (variant === 'private') event.id = '123@c.us';
    if (variant === 'state') event.state = 'available';
    if (variant === 'mismatch') event.state = 'recording_audio';
    if (variant === 'time') event.t = '1';
    if (variant === 'participant') event.participants[0].id = '120-123@g.us';
    if (variant === 'name') event.participants[0].shortName = 'PRIVATE_NAME';
    if (variant === 'empty') event.participants = [];
    if (variant === 'extra') {event.lastSeen = 123; event.secret = 'PRIVATE_SECRET'; event.participants[0].secret = 'PRIVATE_SECRET';}
    bindings.__winzappGroupPresenceChanged({generation: oldGeneration, event});
  }
  else if (input.case === 'unsupported' || input.case === 'partial_attach') {
    window.onPresenceChanged({id: '120-123@g.us', isGroup: true, isUser: false, isOnline: false, state: 'typing', t: 1,
      participants: [{id: '123@c.us', state: 'typing', shortName: ''}]});
  }
  else if (!input.case.endsWith('retry')) change();
  await settle();
  if (input.case.endsWith('retry')) {
    let count = 0;
    while (timers.size && count++ < 500) {
      const [id, fn] = timers.entries().next().value; timers.delete(id); fn(); await settle();
    }
    if (count >= 500) throw Error('Unbounded retry chain');
  }
  process.stdout.write(JSON.stringify({io, hooks, logs, timers: timers.size, evaluates: evaluateCount,
    bindingSame: binding === bindings.onPresenceChanged,
    loadHooks: (pageEvents.get('load') || []).length, closeHooks: (pageEvents.get('close') || []).length,
    oldStoreListeners, listeners: store.eventNames().reduce((n, name) => n + store.listenerCount(name), 0),
    groupExposes: Object.keys(bindings).filter(key => key === '__winzappGroupPresenceChanged').length,
    provider: version, providerMode: input.provider ? 'published-package' : 'verified-portable-fixture',
    sourceUsed: ['onPresenceChanged', 'registerEvent', 'afterPageScriptInjected.exposure-loop']}));
})().catch(error => {process.stderr.write(String(error.stack)); process.exitCode = 1;});
