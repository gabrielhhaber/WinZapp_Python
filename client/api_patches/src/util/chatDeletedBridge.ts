import { installMessageStatePageBridge } from './messageStatePageBridge';

/** Explicit accepted deleteChat sync actions, never ChatStore.remove. */
export function installChatDeletedInPage() {
  const win = globalThis['window'];
  const notify = win?.['__winzappOnChatDeleted'];
  let api;
  try {
    api = win['require']('__debug')?.modulesMap?.WAWebDeleteChatSync?.defaultExport;
    if (!api) { win['require']('WAWebDeleteChatSync'); return false; }
  } catch (_) { return false; }
  if (typeof notify !== 'function' || typeof api?.applyMutations !== 'function') return false;
  const previous = win['__winzappChatDeletedObserver'];
  if (previous?.api === api && api.applyMutations === previous.wrapper) {
    previous.notify = notify; return true;
  }
  previous?.dispose();
  const original = api.applyMutations;
  const descriptor = Object.getOwnPropertyDescriptor(api, 'applyMutations');
  // Object(...) is `any` to strict tsc (a bare literal infers null fields and breaks the build).
  const state = Object({api, notify, wrapper: null, dispose: null, disposed: false});
  const seconds = value => {
    const n = Number(value && typeof value === 'object' && 'low' in value
      ? (value.low >>> 0) + value.high * 4294967296 : value);
    if (!Number.isFinite(n) || n <= 0) return 0;
    return Math.floor(n > 1e11 ? n / 1000 : n);
  };
  state.wrapper = function (mutations, ...rest) {
    return Promise.resolve(original.call(this, mutations, ...rest)).then(results => {
      if (!state.disposed && Array.isArray(mutations)) mutations.forEach((m, index) => {
        try {
          const p = m?.indexParts;
          const until = seconds(m?.value?.deleteChatAction?.messageRange?.lastMessageTimestamp);
          if (m?.operation !== 'set' || p?.length !== 3 || p[0] !== 'deleteChat'
              || (p[2] !== '0' && p[2] !== '1') || typeof p[1] !== 'string'
              || !/^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid|g\.us)$/.test(p[1])
              || !until || results?.[index]?.actionState !== 'Success') return;
          Promise.resolve(state.notify(JSON.stringify({chatId: p[1], until}))).catch(() => {});
        } catch (_) { /* preserve the accepted native result */ }
      });
      return results;
    });
  };
  state.dispose = () => {
    state.disposed = true;
    if (api.applyMutations === state.wrapper) {
      if (descriptor) Object.defineProperty(api, 'applyMutations', descriptor);
      else delete api.applyMutations;
    }
    if (win['__winzappChatDeletedObserver'] === state) delete win['__winzappChatDeletedObserver'];
  };
  try {
    api.applyMutations = state.wrapper;
    if (api.applyMutations !== state.wrapper) { state.dispose(); return false; }
  } catch (_) { state.dispose(); return false; }
  win['__winzappChatDeletedObserver'] = state;
  return true;
}

export function chatDeletedPayload(raw, session) {
  if (typeof raw !== 'string' || raw.length > 512) return null;
  try {
    const v = JSON.parse(raw);
    if (typeof v?.chatId !== 'string' || v.chatId.length > 128
        || !/^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid|g\.us)$/.test(v.chatId)
        || !Number.isSafeInteger(v.until) || v.until <= 0) return null;
    return {session, chatId: v.chatId, until: v.until};
  } catch (_) { return null; }
}

export async function installChatDeletedBridge(client, io, logger) {
  return installMessageStatePageBridge(client, io, logger,
    '__winzappOnChatDeleted', 'chat-deleted', installChatDeletedInPage, chatDeletedPayload);
}
