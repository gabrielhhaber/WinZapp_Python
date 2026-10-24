'use strict';
// Execute actual transpiled helper and extracted registration methods in a VM.
// No browser, HTTP, provider process or account. Timers and stores are plain fakes.
const fs = require('fs'), path = require('path'), vm = require('vm');
const input = JSON.parse(process.argv[2]);
const ts = require(input.compiler);
const root = path.resolve(__dirname, '..');
const hostTimers = new Map(); let timerId = 0;
const host = vm.createContext({console, setTimeout(fn) {hostTimers.set(++timerId, fn); return timerId;},
  clearTimeout(id) {hostTimers.delete(id);}});
const loaded = new Map();
function load(name) {
  if (loaded.has(name)) return loaded.get(name);
  const file = path.join(root, 'client/api_patches/src/util', name + '.ts');
  const emitted = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: {target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS}, reportDiagnostics: true});
  if (emitted.diagnostics.some(d => d.category === ts.DiagnosticCategory.Error)) throw Error('Transpile failed');
  const output = {};
  host.moduleExports = output;
  host.require = spec => load(path.basename(spec));
  vm.runInContext('(function(exports,require){' + emitted.outputText + '\n})(moduleExports,require)', host);
  loaded.set(name, output); return output;
}
const helper = load('contactPresenceBridge');
function store() {
  const handlers = new Map(), models = new Map();
  return {handlers, models,
    on(name, fn) {if (!handlers.has(name)) handlers.set(name, new Set()); handlers.get(name).add(fn);},
    off(name, fn) {handlers.get(name)?.delete(fn);},
    emit(name, changed) {for (const fn of handlers.get(name) || []) fn(changed);},
    get(id) {return models.get(String(id));}, getModelsArray() {return [...models.values()];},
    count() {return [...handlers.values()].reduce((n, x) => n + x.size, 0);}};
}
function model(id = '123@c.us') {
  return {id: {_serialized: id, toString() {return id;}}, hasData: true, isOnline: false,
    chatstate: {type: 'unavailable', t: 1000, deny: false}};
}
function world() {
  const presence = store(), microtasks = [], sent = [];
  const m = model(input.id); presence.models.set(String(m.id), m);
  const window = {WPP: {isFullReady: true, whatsapp: {
    WidFactory: {createWid: id => id}, PresenceStore: presence, ChatStore: {get: () => null}}},
    __winzappContactPresenceChanged: envelope => {sent.push(envelope);}};
  const context = vm.createContext({window, queueMicrotask: fn => microtasks.push(fn), Date: {now: () => 1700000000123}});
  return {window, context, presence, m, sent, microtasks,
    flush() {for (let n = 0; microtasks.length; n++) {if (n > 30) throw Error('Unbounded tasks'); microtasks.shift()();}},
    install(generation = 1) {return vm.runInContext('(' + helper.CONTACT_PRESENCE_BRIDGE_SOURCE + ')(' + generation + ')', context, {timeout: 1000});}};
}
function fields() {
  const w = world(), {m, presence: s} = w;
  if (input.case === 'hasData-ready') m.hasData = false;
  if (input.case === 'unsupported') delete s.off;
  if (input.case === 'attach-failure') {
    const on = s.on.bind(s);
    s.on = (name, fn) => {if (name === 'change:hasData') throw Error('synthetic'); on(name, fn);};
  }
  const installed = w.install();
  switch (input.case) {
    case 't': m.chatstate.t = 2000; s.emit('change:chatstate.t', m.chatstate); break;
    case 'add': s.models.delete(String(m.id)); s.models.set(String(m.id), m); s.emit('add', m); break;
    case 'online': m.isOnline = true; s.emit('change:isOnline', m); break;
    case 'deny': m.chatstate.deny = true; s.emit('change:chatstate.deny', m.chatstate); break;
    case 'hasData-pending': m.hasData = false; s.emit('change:hasData', m); break;
    case 'hasData-ready': m.hasData = true; s.emit('change:hasData', m); break;
    case 'chatstate': m.chatstate = {type: 'unavailable', t: 3000}; s.emit('change:chatstate', m); break;
    case 'type': m.chatstate.type = 'typing'; s.emit('change:chatstate.type', m.chatstate); break;
    case 'burst':
      m.chatstate.t = 2000; s.emit('change:chatstate.t', m.chatstate);
      m.isOnline = true; s.emit('change:isOnline', m);
      m.chatstate.deny = true; s.emit('change:chatstate.deny', m.chatstate); break;
    case 'duplicate':
      s.emit('change:chatstate.t', m.chatstate); w.flush(); s.emit('change:chatstate.t', m.chatstate); break;
    case 'reinstall': w.install(); w.install(); s.emit('change:chatstate.t', m.chatstate); break;
    case 'old-generation':
      s.emit('change:chatstate.t', m.chatstate); w.install(2); w.install(1);
      m.chatstate.t = 4000; s.emit('change:chatstate.t', m.chatstate); break;
    case 'replacement-store': {
      s.emit('change:chatstate.t', m.chatstate);
      const next = store(), n = model(); next.models.set(String(n.id), n);
      w.window.WPP.whatsapp.PresenceStore = next; w.install(2);
      next.emit('change:chatstate.t', n.chatstate); break;
    }
    case 'old-model':
      s.models.set(String(m.id), model()); s.emit('change:chatstate.t', m.chatstate); break;
    case 'remove':
      s.emit('change:chatstate.t', m.chatstate); s.models.delete(String(m.id)); s.emit('remove', m); break;
    case 'reset': s.emit('change:chatstate.t', m.chatstate); s.emit('reset'); break;
    case 'unsupported': break;
    default: s.emit('change:chatstate.t', m.chatstate);
  }
  w.flush();
  return {installed, sent: w.sent, listeners: s.count()};
}
async function settle() {for (let i = 0; i < 30; i++) await Promise.resolve();}
function page(w) {
  const bindings = {}, events = new Map();
  return {bindings, events, exposes: 0, evaluates: 0, closed: false,
    on(name, fn) {events.set(name, fn);}, isClosed() {return this.closed;},
    async exposeFunction(name, fn) {if (bindings[name]) throw Error('Already exposed');
      this.exposes++; bindings[name] = fn; w.window[name] = fn;},
    async evaluate(expression) {this.evaluates++; return vm.runInContext(expression, w.context, {timeout: 1000});}};
}
async function lifecycle() {
  const w = world(), p = page(w), sent = [], logs = [];
  const req = {io: {emit: (name, event) => sent.push({name, event})}, logger: {warn: value => logs.push(value)}};
  const first = {page: p, session: 'first'}, second = {page: p, session: 'second'};
  let releaseOld;
  if (input.case === 'late-evaluate') {
    const evaluate = p.evaluate.bind(p);
    p.evaluate = expression => {
      if (!releaseOld) return new Promise(resolve => {releaseOld = () => resolve(vm.runInContext(expression, w.context));});
      return evaluate(expression);
    };
  }
  if (input.case === 'retry' || input.case === 'close') w.window.WPP.isFullReady = false;
  helper.installContactPresenceBridge(first, req); await settle();
  const oldGeneration = w.window.__winzappContactPresenceBridge?.generation;
  if (input.case === 'repeat') {helper.installContactPresenceBridge(first, req); await settle();}
  if (input.case === 'owner') {helper.installContactPresenceBridge(second, req); await settle();}
  if (input.case === 'late-evaluate') {
    helper.installContactPresenceBridge(second, req); await settle(); releaseOld(); await settle();
  }
  if (input.case === 'reload') {p.events.get('load')(); await settle();}
  if (input.case === 'close') {p.closed = true; p.events.get('close')();}
  if (input.case === 'page_move') first.page = {isClosed: () => false};
  if (input.case === 'retry') {
    for (let i = 0; hostTimers.size; i++) {
      if (i > 30) throw Error('Unbounded retries');
      const [id, fn] = hostTimers.entries().next().value; hostTimers.delete(id); fn(); await settle();
    }
  }
  const activeGeneration = w.window.__winzappContactPresenceBridge?.generation;
  const event = {id: '123@c.us', isGroup: false, isOnline: false, restricted: false,
    state: 'unavailable', snapshotStatus: 'ready', lastSeen: 1000, t: 1700000000123};
  if (input.case === 'owner' || input.case === 'reload') p.bindings.__winzappContactPresenceChanged({generation: oldGeneration, event});
  if (input.case === 'malformed') {
    for (const patch of [{id: '1@g.us'}, {snapshotStatus: 'other'}, {lastSeen: -1}, {restricted: 0},
      {isOnline: null}, {t: 'bad'}, {state: {}}, {id: {toString: () => '123@c.us'}}]) {
      p.bindings.__winzappContactPresenceChanged({generation: activeGeneration, event: {...event, ...patch}});
    }
  } else {
    p.bindings.__winzappContactPresenceChanged({generation: activeGeneration, event: {...event, ignoredSecret: 'DROP'}});
  }
  return {sent, logs, exposes: p.exposes, evaluates: p.evaluates, timers: hostTimers.size,
    listeners: w.presence.count(), loadHooks: p.events.size,
    firstLegacy: helper.forwardLegacyContactPresence(first, event),
    secondLegacy: helper.forwardLegacyContactPresence(second, event),
    groupLegacy: helper.forwardLegacyContactPresence(first, {id: '1@g.us'})};
}
async function bootstrap() {
  const file = path.join(root, 'client/api_patches/src/util/createSessionUtil.ts');
  const source = fs.readFileSync(file, 'utf8'), ast = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true);
  let block, method;
  function walk(node) {
    if (ts.isIfStatement(node) && node.expression.getText(ast) === 'req.serverOptions.webhook.onPresenceChanged') block = node.getText(ast);
    if (ts.isMethodDeclaration(node) && node.name.getText(ast) === 'onPresenceChanged') method = node.getText(ast);
    ts.forEachChild(node, walk);
  }
  walk(ast); if (!block || !method) throw Error('Missing actual listener source');
  const w = world(), p = page(w), io = [], webhooks = [], calls = [];
  const req = {serverOptions: {webhook: {onPresenceChanged: true}}, io: {emit: (name, e) => io.push(e)}, logger: {warn() {}}};
  const client = {page: p, session: 'test', onPresenceChanged: async fn => {client.callback = fn;}};
  host.installContactPresenceBridge = helper.installContactPresenceBridge;
  host.forwardLegacyContactPresence = helper.forwardLegacyContactPresence;
  host.legacyPresenceOwnerCurrent = helper.legacyPresenceOwnerCurrent;
  host.callWebHook = (c, r, name, e) => webhooks.push(e);
  const code = 'async function setup(client,req){' + block + '}\nclass Forwarder {' + method + '}';
  const emitted = ts.transpileModule(code, {compilerOptions: {target: ts.ScriptTarget.ES2020}}).outputText;
  vm.runInContext(emitted + '\nthis.Forwarder=Forwarder;', host);
  let completed = false;
  host.setup.call({onPresenceChanged: async () => {calls.push('legacy');
    if (input.case === 'reject') throw Error('synthetic'); return new Promise(() => {});},
    onGroupPresenceBridge: async () => calls.push('group')}, client, req).then(() => completed = true);
  await settle();
  const f = new host.Forwarder(); await f.onPresenceChanged(client, req);
  const legacy = {id: '123@c.us', state: 'typing', t: 10};
  await client.callback(legacy); await client.callback({id: '1@g.us', state: 'typing'});
  w.presence.emit('change:chatstate.t', w.m.chatstate); w.flush(); await settle();
  return {completed, calls, io, webhooks, nativeListeners: w.presence.count()};
}
function snapshot() {
  const w = world(), id = String(w.m.id);
  if (input.model !== undefined) w.presence.models.set(id, input.model);
  w.window.WPP.whatsapp.ChatStore.get = () => {
    if (input.fault === 'chat') throw Error('PRIVATE_ERROR');
    return input.fallback ? {presence: input.fallback} : null;
  };
  if (input.fault === 'primary') w.presence.get = () => {throw Error('PRIVATE_ERROR');};
  return vm.runInContext('(' + load('contactPresenceRuntime').CONTACT_PRESENCE_SOURCE + ')(' + JSON.stringify(id) + ')', w.context);
}
(async () => {
  const result = input.mode === 'fields' ? fields() : input.mode === 'bootstrap' ? await bootstrap()
    : input.mode === 'snapshot' ? snapshot() : await lifecycle();
  process.stdout.write(JSON.stringify(result));
})().catch(error => {process.stderr.write(String(error.stack)); process.exitCode = 1;});
