import { installMessageStatePageBridge } from './messageStatePageBridge';

/** Observe confirmed delete-for-me actions, never a collection eviction. */
export function installMessageDeleteInPage() {
  const win = globalThis['window'];
  const notify = win?.['__winzappOnMessageDeletedForMe'];
  const chat = win?.['WPP']?.chat;
  const parser = win?.['WPP']?.whatsapp?.MsgKey;
  let sync;
  try {
    sync = win['require']('__debug')?.modulesMap?.WAWebDeleteMessageForMeSync?.defaultExport;
    if (!sync) { win['require']('WAWebDeleteMessageForMeSync'); return false; }
  } catch (_) { return false; }
  if (typeof notify !== 'function' || typeof sync?.applyMutations !== 'function'
      || typeof chat?.deleteMessage !== 'function' || typeof parser?.fromString !== 'function') return false;
  const previous = win['__winzappMessageDeleteObserver'];
  if (previous?.revision === 1 && previous?.sync === sync && previous?.chat === chat
      && sync.applyMutations === previous.syncWrapper && chat.deleteMessage === previous.localWrapper) {
    previous.notify = notify;
    return true;
  }
  previous?.dispose();
  const originalSync = sync.applyMutations, originalLocal = chat.deleteMessage;
  const syncDescriptor = Object.getOwnPropertyDescriptor(sync, 'applyMutations');
  const localDescriptor = Object.getOwnPropertyDescriptor(chat, 'deleteMessage');
  // Object(...) is `any` to strict tsc (a bare literal infers null fields and breaks the build).
  const state = Object({ sync, chat, notify, revision: 1, disposed: false, timers: new Set(),
    syncWrapper: null, localWrapper: null, dispose: null });
  const emit = (chatId, messageId) => {
    if (state.disposed || typeof chatId !== 'string' || typeof messageId !== 'string'
        || !messageId || !/^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid|g\.us)$/.test(chatId)) return;
    try { Promise.resolve(state.notify(JSON.stringify({chatId, messageId}))).catch(() => {}); }
    catch (_) { /* a notification cannot fail an accepted deletion */ }
  };
  const confirmLocal = (key, serialized, attempt = 0) => {
    if (state.disposed) return;
    try {
      const chatId = typeof key.remote === 'string' ? key.remote : key.remote?._serialized;
      const store = win['WPP']?.whatsapp?.MsgStore;
      const local = win['WPP']?.whatsapp?.ChatStore?.get(chatId);
      if (typeof local?.msgs?.get !== 'function' || typeof store?.get !== 'function') return;
      if (!local.msgs.get(key) && !store.get(serialized)) { emit(chatId, key.id); return; }
      if (attempt >= 20) return;
      const timer = setTimeout(() => {
        state.timers.delete(timer);
        confirmLocal(key, serialized, attempt + 1);
      }, 500);
      state.timers.add(timer);
    } catch (_) { /* no evidence is never an instruction to delete */ }
  };
  state.syncWrapper = function (mutations, ...rest) {
    const batch = Array.isArray(mutations) ? mutations : [];
    return Promise.resolve(originalSync.call(this, mutations, ...rest)).then((results) => {
      try {
        batch.forEach((m, i) => {
          const p = m?.indexParts;
          if (m?.operation === 'set' && p?.length === 5 && p[0] === 'deleteMessageForMe'
              && (p[3] === '0' || p[3] === '1') && p[4]
              && results?.[i]?.actionState === 'Success') emit(p[1], p[2]);
        });
      } catch (_) { /* preserve WhatsApp's mutation result */ }
      return results;
    });
  };
  state.localWrapper = function (...args) {
    return Promise.resolve(originalLocal.apply(this, args)).then((result) => {
      for (const item of Array.isArray(result) ? result : [result]) {
        if (item?.isRevoked !== false || typeof item?.id !== 'string') continue;
        try {
          const key = parser.fromString(item.id);
          if (item.isDeleted === true)
            emit(typeof key.remote === 'string' ? key.remote : key.remote?._serialized, key.id);
          else if (item.sendMsgResult === 'OK') confirmLocal(key, item.id);
        } catch (_) { /* a malformed return value is not a deletion notice */ }
      }
      return result;
    });
  };
  state.dispose = () => {
    state.disposed = true;
    state.timers.forEach(timer => clearTimeout(Number(timer)));
    state.timers.clear();
    if (sync.applyMutations === state.syncWrapper) {
      if (syncDescriptor) Object.defineProperty(sync, 'applyMutations', syncDescriptor);
      else delete sync.applyMutations;
    }
    if (chat.deleteMessage === state.localWrapper) {
      if (localDescriptor) Object.defineProperty(chat, 'deleteMessage', localDescriptor);
      else delete chat.deleteMessage;
    }
    if (win['__winzappMessageDeleteObserver'] === state) delete win['__winzappMessageDeleteObserver'];
  };
  try {
    sync.applyMutations = state.syncWrapper;
    // WA-JS (4.7.2 through 4.8.0) exports a configurable getter, with no setter.
    if (localDescriptor?.configurable === false) chat.deleteMessage = state.localWrapper;
    else Object.defineProperty(chat, 'deleteMessage', {
      value: state.localWrapper, writable: true, configurable: true,
      enumerable: localDescriptor?.enumerable ?? true,
    });
    if (sync.applyMutations !== state.syncWrapper || chat.deleteMessage !== state.localWrapper) {
      state.dispose(); return false;
    }
  } catch (_) { state.dispose(); return false; }
  win['__winzappMessageDeleteObserver'] = state;
  return true;
}

export function messageDeletedPayload(raw, session) {
  if (typeof raw !== 'string' || raw.length > 512) return null;
  try {
    const value = JSON.parse(raw);
    if (!value || typeof value.chatId !== 'string' || value.chatId.length > 128
        || !/^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid|g\.us)$/.test(value.chatId)
        || typeof value.messageId !== 'string' || !value.messageId || value.messageId.length > 128) return null;
    return {session, chatId: value.chatId, messageId: value.messageId};
  } catch (_) { return null; }
}

export async function installMessageDeleteBridge(client, io, logger) {
  return installMessageStatePageBridge(client, io, logger,
    '__winzappOnMessageDeletedForMe', 'message-deleted-for-me',
    installMessageDeleteInPage, messageDeletedPayload);
}
