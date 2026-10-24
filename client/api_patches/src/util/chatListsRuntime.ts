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
  // WA-JS 4.6.1 still expects labelsEditingEnabled, removed from the current
  // consumer Lists UI; 4.7.1 only tolerates its absence for its own calls, and
  // WinZapp's native contract below is the same on both. Select it before any
  // write; never replace a false legacy gate or retry a failed write by
  // another path.
  const nativeContractVersions = ['4.6.1', '4.7.1', '4.7.2'];
  const resolveEditing = () => {
    const result = (reason) => ({reason, nativeActions: null});
    try {
      if (!['create', 'rename', 'remove', 'addChats', 'removeChats']
          .every((method) => typeof lists[method] === 'function')) {
        return result('runtime_incomplete');
      }
      const gate = wpp.whatsapp.labelsEditingEnabled;
      if (typeof gate !== 'function') {
        // Not ready yet is transient: "refresh and try again", not "this version lacks".
        if (gate === undefined && nativeContractVersions.includes(wpp.version)
            && wpp.isReady !== true) {
          return result('capability_check_failed');
        }
        if (gate !== undefined || !nativeContractVersions.includes(wpp.version)
            || wpp.loader?.loaderType !== 'meta'
            || typeof wpp.loader.loadModule !== 'function') return result('runtime_incomplete');
        const actions = wpp.loader.loadModule('WAWebBizLabelEditingAction');
        const flow = wpp.loader.loadModule('WAWebListsActions');
        const platform = wpp.loader.loadModule('WAWebMobilePlatforms');
        const filters = wpp.loader.loadModule('WAWebInboxFiltersGatingUtils');
        const gating = wpp.loader.loadModule('WAWebListsLabelGatingUtils');
        if (typeof platform?.isSMB !== 'function' || platform.isSMB() !== false
            || typeof filters?.inboxFiltersEnabled !== 'function'
            || typeof gating?.smartFiltersEnabled !== 'function'
            || typeof gating?.labelsEditingEnabled !== 'undefined'
            || !['createNewListAction', 'editListAction', 'deleteListAction']
              .every((name) => typeof flow?.[name] === 'function')
            || ![['labelAddAction', 2], ['labelEditAction', 6], ['labelDeleteAction', 1]]
              .every(([name, arity]) => typeof actions?.[name] === 'function'
                && actions[name].length === arity)) return result('runtime_incomplete');
        const enabled = filters.inboxFiltersEnabled();
        if (enabled !== true) return result(enabled === false ? 'account_disabled' : 'capability_check_failed');
        return {reason: '', nativeActions: actions};
      }
      const enabled = gate();
      if (enabled === true) return result('');
      return result(enabled === false ? 'account_disabled' : 'capability_check_failed');
    } catch { return result('capability_check_failed'); }
  };
  const validJid = (jid) => typeof jid === 'string'
    && /^\d+(?:-\d+)?(?::\d+)?@(c\.us|s\.whatsapp\.net|lid|g\.us)$/.test(jid);
  const custom = lists.list();
  if (!Array.isArray(custom)) fail('list_response_invalid');
  const {reason, nativeActions} = resolveEditing();
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
      canEdit: reason === '', editingReason: reason,
      lists: rows.map((item) => ({
        id: String(item.id), name: item.name,
        members: [...members.get(String(item.id))],
      })),
    };
  }
  if (!['create', 'rename', 'remove', 'addChats', 'removeChats'].includes(input?.action)) {
    fail('list_command_invalid');
  }
  if (reason) fail('list_editing_not_available');
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
    const createdId = nativeActions
      ? await nativeActions.labelAddAction(input.name.trim(), null)
      : await lists.create(input.name.trim());
    if (!['string', 'number'].includes(typeof createdId)
        || (typeof createdId === 'number' && (!Number.isSafeInteger(createdId) || createdId < 0))
        // The list may already exist: report it as unconfirmed (503), never as refused.
        || !/^[A-Za-z0-9_-]{1,64}$/.test(String(createdId))) fail('list_operation_unconfirmed');
    return { action: input.action, createdId: String(createdId) };
  }
  if (input.action === 'rename') {
    if (nativeActions) {
      const label = labels.get(input.id);
      await nativeActions.labelEditAction(input.id, input.name.trim(),
        label.predefinedId ?? 0, label.colorIndex ?? null, label.isActive, label.type);
    } else await lists.rename(input.id, input.name.trim());
  }
  if (input.action === 'remove') {
    if (nativeActions) {
      const label = labels.get(input.id);
      await nativeActions.labelDeleteAction({labelId: input.id, name: label.name,
        color: label.colorIndex ?? null});
    } else await lists.remove(input.id);
  }
  if (input.action === 'addChats') await lists.addChats(input.id, [...new Set(input.chatIds)]);
  if (input.action === 'removeChats') await lists.removeChats(input.id, [...new Set(input.chatIds)]);
  return { action: input.action, id: input.id };
}
