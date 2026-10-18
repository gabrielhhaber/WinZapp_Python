// Execute the actual controller/page callback against synthetic WhatsApp models.
// No browser, desktop window, API process or user data is opened.
const fs = require('node:fs');
const vm = require('node:vm');
const {stripTypeScriptTypes} = require('node:module');
const source = fs.readFileSync(process.argv[2], 'utf8');
const transformed = stripTypeScriptTypes(source, {mode: 'transform', sourceMap: false})
  .replace(/^import[\s\S]*?from ['"][^'"]+['"];\r?\n/gm, '')
  .replace(/^export /gm, '');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const contact = {isMyContact: true};
contact.self = contact;
contact.toJSON = () => ({isMyContact: true});
const chat = {id: {_serialized: 'synthetic@g.us'}, t: 123, contact};
let reads = 0;
const page = {
  async evaluate(callback, args) {
    const value = await callback(args);
    // Like CDP's returnByValue, this does not invoke a model's toJSON hook.
    // An untransferable graph must not be mistaken for an empty ChatStore.
    try { return structuredClone(value); } catch { return undefined; }
  },
};
const sandbox = {
  WPP: {isReady: true, conn: {isMainReady: () => true},
    whatsapp: {ChatStore: {length: 1}},
    chat: {list: async () => input.recovery && reads++ === 0 ? [] : [chat]}},
  WAPI: {_serializeChatObj: value => value},
  __winzappChatStoreRecoveredAt: Date.now(),
  observeEvaluate: (_operation, run) => run(), countStoreRecovery() {},
  observePayload: (_schema, value) => ({status: 'success', response: value}),
  SyncChatListSchema: {},
  buildDiag: (raw, visible) => ({raw: raw.length, visible: visible.length}),
  diagStateFor: () => ({}), shouldLogDiag: () => false,
  Date, Map, Set, Promise, structuredClone, setTimeout, clearTimeout,
};
vm.createContext(sandbox);
vm.runInContext(transformed + '\nglobalThis.route = listChats;', sandbox);
const req = {client: {page}, body: {ignoreGroupMetadata: true},
  params: {session: 'synthetic'},
  logger: {info() {}, warn() {}, error() {}}};
const res = {statusCode: null, body: null,
  status(code) {this.statusCode = code; return this;},
  json(body) {this.body = body; return this;}};
sandbox.route(req, res).then(() => {
  console.log(JSON.stringify({status: res.statusCode, body: res.body}));
}).catch(error => {console.error(error); process.exitCode = 1;});
