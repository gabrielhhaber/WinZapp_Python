// Self-contained: Puppeteer serializes this function into the WhatsApp page.
// Kept as JavaScript syntax so Node tests can execute it without a compiler.
export async function executeListCommand(input) {
  const wpp = globalThis['WPP'];
  const fail = (code) => { throw Object.assign(new Error(code), { code }); };
  const lists = wpp?.lists;
  const store = wpp?.whatsapp?.ChatStore;
  const labels = wpp?.whatsapp?.LabelStore;
  if (typeof lists?.list !== 'function' || typeof store?.getModelsArray !== 'function'
      || typeof labels?.get !== 'function') fail('lists_not_available');
  const canEdit = () => {
    try {
      return wpp.whatsapp.labelsEditingEnabled?.() === true
        && ['create', 'rename', 'remove', 'addChats', 'removeChats']
          .every((method) => typeof lists[method] === 'function');
    } catch { return false; }
  };
  const validJid = (jid) => typeof jid === 'string'
    && /^\d+(?:-\d+)?(?::\d+)?@(c\.us|s\.whatsapp\.net|lid|g\.us)$/.test(jid);
  const custom = lists.list();
  if (!Array.isArray(custom)) fail('list_response_invalid');
  // WA-JS's custom list contract is type=5. Do not expose Business labels or
  // an unverified predefined/favorite identifier as an editable custom list.
  const isCustom = (id) => labels.get(id)?.type === 5;
  if (input?.action === 'read') {
    const chats = store.getModelsArray();
    const rows = custom.filter((item) => isCustom(String(item.id)));
    const members = new Map();
    for (const item of rows) members.set(String(item.id), new Set());
    for (const chat of chats) {
      const jid = chat.id?._serialized ?? chat.id?.toString?.();
      if (!validJid(jid) || !Array.isArray(chat.labels)) continue;
      for (const id of chat.labels) members.get(String(id))?.add(jid);
    }
    return {
      canEdit: canEdit(),
      lists: rows.map((item) => ({
        id: String(item.id), name: item.name,
        members: [...members.get(String(item.id))],
      })),
    };
  }
  if (!['create', 'rename', 'remove', 'addChats', 'removeChats'].includes(input?.action)) {
    fail('list_command_invalid');
  }
  if (!canEdit()) fail('list_editing_not_available');
  if (input.action !== 'create') {
    if (typeof input.id !== 'string' || !/^[A-Za-z0-9_-]{1,64}$/.test(input.id)) {
      fail('list_command_invalid');
    }
    if (!isCustom(input.id) || !custom.some((item) => String(item.id) === input.id)) {
      fail('list_not_found');
    }
  }
  if (input.action === 'create' || input.action === 'rename') {
    if (typeof input.name !== 'string' || !input.name.trim() || input.name.trim().length > 100) {
      fail('list_name_required');
    }
  }
  if (input.action === 'addChats' || input.action === 'removeChats') {
    if (!Array.isArray(input.chatIds) || input.chatIds.length === 0
        || input.chatIds.length > 500 || !input.chatIds.every(validJid)) {
      fail('list_command_invalid');
    }
    // A mutation must address chats the linked device actually holds. No
    // assertGetChat-driven implicit creation or lookup of a guessed phone.
    const known = new Set(store.getModelsArray().map((chat) => chat.id?._serialized));
    if (!input.chatIds.every((id) => known.has(id))) fail('list_chat_not_found');
  }
  if (input.action === 'create') {
    const createdId = await lists.create(input.name.trim());
    return { action: input.action, createdId: String(createdId) };
  }
  if (input.action === 'rename') await lists.rename(input.id, input.name.trim());
  if (input.action === 'remove') await lists.remove(input.id);
  if (input.action === 'addChats') await lists.addChats(input.id, [...new Set(input.chatIds)]);
  if (input.action === 'removeChats') await lists.removeChats(input.id, [...new Set(input.chatIds)]);
  return { action: input.action, id: input.id };
}
