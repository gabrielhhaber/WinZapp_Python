// Exercise the real message listener and download utility with no networking.
const fs = require('node:fs');
const vm = require('node:vm');
const {stripTypeScriptTypes} = require('node:module');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const read = path => stripTypeScriptTypes(fs.readFileSync(path, 'utf8'),
  {mode: 'transform', sourceMap: false})
  .replace(/^import[\s\S]*?from ['"][^'"]+['"];\r?\n/gm, '')
  .replace(/^export default /gm, '').replace(/^export /gm, '');
const listeners = {}, emitted = [], posts = [];
let downloads = 0;
const client = {session: 'synthetic', config: {},
  decryptFile: async () => { downloads++; return Buffer.from('synthetic media'); },
  onMessage: async fn => {listeners.message = fn;},
  onAnyMessage: async fn => {listeners.any = fn;},
  onMessageEdit: async () => {}, onIncomingCall: async () => {}};
const req = {serverOptions: {
  websocket: {autoDownload: !!input.websocket, uploadS3: false},
  webhook: {url: input.webhook ? 'https://configured.invalid' : null,
    autoDownload: true, autoDownloadMaxSize: 0, uploadS3: false},
  mapper: {enable: false}},
  io: {emit: (event, data) => emitted.push({event, data: JSON.parse(JSON.stringify(data))})},
  logger: {info() {}, warn() {}, error(error) {throw error;}}};
const sandbox = {Buffer, Date, Map, Set, Promise, setTimeout, clearTimeout,
  fs: {unlink() {}}, promisify: () => () => Promise.resolve(),
  config: {webhook: {uploadS3: false}},
  process, eventEmitter: {emit() {}},
  api: {post: async (_url, data) => posts.push(JSON.parse(JSON.stringify(data)))}};
vm.createContext(sandbox);
vm.runInContext(read(process.argv[2]) + '\n' + read(process.argv[3]) +
  '\nglobalThis.listener = new CreateSessionUtil();', sandbox);
(async () => {
  await sandbox.listener.listenMessages(client, req);
  const message = {id: 'synthetic', type: 'document', mimetype: 'application/pdf',
    size: 24 * 1024 * 1024, fromMe: !!input.fromMe, from: 'synthetic@lid',
    body: '', caption: 'synthetic document'};
  await listeners.any(message);
  if (!message.fromMe) await listeners.message(message);
  // callWebHook is deliberately fire-and-forget in the listener.
  await new Promise(resolve => setImmediate(resolve));
  console.log(JSON.stringify({downloads, posts, emitted}));
})().catch(error => {console.error(error); process.exitCode = 1;});
