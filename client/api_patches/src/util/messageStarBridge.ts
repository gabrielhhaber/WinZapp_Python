// Star observations only: no writes and no message bodies leave the page.
import { installMessageStatePageBridge } from './messageStatePageBridge';

/** Self-contained: Puppeteer serializes only this function into the page. */
export function installMessageStarInPage() {
  const win = globalThis['window'];
  const store = win?.['WPP']?.whatsapp?.MsgStore;
  const notify = win?.['__winzappOnMessageStarChanged'];
  if (typeof store?.on !== 'function' || typeof store?.off !== 'function'
      || typeof notify !== 'function') return false;
  // A star synced for a message that is not loaded in MsgStore is persisted
  // with no model to emit change:star: observe the accepted sync action too.
  let sync;
  try {
    sync = win['require']('__debug')?.modulesMap?.WAWebStarMessageSync?.defaultExport;
    if (!sync) win['require']('WAWebStarMessageSync');
  } catch (_) { sync = undefined; }
  if (typeof sync?.applyMutations !== 'function') sync = undefined;
  const previous = win['__winzappMessageStarObserver'];
  if (previous?.revision === 2 && previous?.store === store && previous?.sync === sync
      && (!sync || sync.applyMutations === previous.syncWrapper)) {
    previous.notify = notify;
    return !!sync;  // false keeps retrying until the sync module loads
  }
  previous?.dispose();
  // Object(...) is `any` to strict tsc (a bare literal infers null fields and breaks the build).
  const state = Object({ store, sync, notify, revision: 2, disposed: false,
    handler: null, syncWrapper: null, dispose: null });
  const emit = (chatId, messageId, starred) => {
    if (state.disposed || typeof chatId !== 'string' || typeof messageId !== 'string' || !messageId
        || typeof starred !== 'boolean'
        || !/^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid|g\.us)$/.test(chatId)) return;
    try {
      Promise.resolve(state.notify(JSON.stringify({chatId, messageId, starred}))).catch(() => {});
    } catch (_) { /* an observation must never break WhatsApp's state change */ }
  };
  state.handler = (model) => {
    try {
      const key = model?.id;
      const chatId = typeof key?.remote === 'string' ? key.remote : key?.remote?._serialized;
      emit(chatId, key?.id, model?.star);
    } catch (_) { /* an observation must never break WhatsApp's state change */ }
  };
  const descriptor = sync && Object.getOwnPropertyDescriptor(sync, 'applyMutations');
  if (sync) {
    const original = sync.applyMutations;
    state.syncWrapper = function (mutations, ...rest) {
      return Promise.resolve(original.call(this, mutations, ...rest)).then((results) => {
        try {
          if (Array.isArray(mutations)) mutations.forEach((m, i) => {
            // indexParts: ['star', remote, id, fromMe, participant]
            const p = m?.indexParts;
            if (m?.operation === 'set' && p?.length === 5 && p[0] === 'star'
                && (p[3] === '0' || p[3] === '1') && p[4]
                && results?.[i]?.actionState === 'Success')
              emit(p[1], p[2], m?.value?.starAction?.starred);
          });
        } catch (_) { /* preserve WhatsApp's mutation result */ }
        return results;
      });
    };
  }
  state.dispose = () => {
    state.disposed = true;
    store.off('change:star', state.handler);
    if (sync && sync.applyMutations === state.syncWrapper) {
      if (descriptor) Object.defineProperty(sync, 'applyMutations', descriptor);
      else delete sync.applyMutations;
    }
    if (win['__winzappMessageStarObserver'] === state) delete win['__winzappMessageStarObserver'];
  };
  store.on('change:star', state.handler);
  win['__winzappMessageStarObserver'] = state;
  if (!sync) return false;  // keep retrying until the sync module loads
  try { sync.applyMutations = state.syncWrapper; } catch (_) { /* checked below */ }
  // A non-writable handler keeps the loaded-message observation on its own.
  if (sync.applyMutations !== state.syncWrapper) state.syncWrapper = null;
  return true;
}

export function messageStarPayload(raw, session) {
  if (typeof raw !== 'string' || raw.length > 512) return null;
  try {
    const value = JSON.parse(raw);
    if (!value || typeof value.chatId !== 'string' || value.chatId.length > 128
        || !/^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid|g\.us)$/.test(value.chatId)
        || typeof value.messageId !== 'string' || !value.messageId || value.messageId.length > 128
        || typeof value.starred !== 'boolean') return null;
    return { session, chatId: value.chatId, messageId: value.messageId, starred: value.starred };
  } catch (_) { return null; }
}

/** One owner per page, including reloads and a replacement server client. */
export async function installMessageStarBridge(client, io, logger) {
  return installMessageStatePageBridge(client, io, logger,
    '__winzappOnMessageStarChanged', 'message-star-changed',
    installMessageStarInPage, messageStarPayload);
}
