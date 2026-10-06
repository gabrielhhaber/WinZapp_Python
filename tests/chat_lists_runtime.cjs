// Execute the actual self-contained page function on synthetic WhatsApp stores.
// No browser, API process, npm install or user profile is involved.
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8').replace('export async function', 'async function');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const calls = [];
const labels = new Map((input.lists || []).map(item => [item.id, { ...item, type: item.type ?? 5 }]));
const chats = (input.chats || []).map(chat => ({ id: { _serialized: chat.id }, labels: [...chat.labels] }));
let nextId = 100;
const runtime = {
  whatsapp: {
    ChatStore: { getModelsArray: () => chats },
    LabelStore: { get: id => labels.get(id) },
    labelsEditingEnabled: () => input.editable !== false,
  },
  lists: {
    list: () => [...labels.values()],
    create: async name => { const id = String(nextId++); calls.push(['create', name]); labels.set(id, { id, name, type: 5 }); return id; },
    rename: async (id, name) => { calls.push(['rename', id, name]); labels.get(id).name = name; },
    remove: async id => { calls.push(['remove', id]); labels.delete(id); },
    addChats: async (id, ids) => { calls.push(['addChats', id, ids]); for (const chat of chats) if (ids.includes(chat.id._serialized)) chat.labels.push(id); },
    removeChats: async (id, ids) => { calls.push(['removeChats', id, ids]); for (const chat of chats) if (ids.includes(chat.id._serialized)) chat.labels = chat.labels.filter(label => label !== id); },
  },
};
if (input.missing) delete runtime[input.missing];
if (input.missingMethod) delete runtime.lists[input.missingMethod];
const context = vm.createContext({ WPP: runtime });
vm.runInContext(source, context);
const execute = context.executeListCommand;
(async () => {
  const results = [];
  for (const command of input.commands || []) {
    try { results.push({ value: await execute(command) }); }
    catch (error) { results.push({ error: error.code || error.message }); }
  }
  process.stdout.write(JSON.stringify({ results, calls }));
})().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
