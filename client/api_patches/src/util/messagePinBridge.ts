import { installMessageStatePageBridge } from './messageStatePageBridge';

/** Meta persists add-ons without hydrating the frontend PinInChatStore. */
export function installMessagePinInPage() {
  const win = globalThis['window'];
  const notify = win?.['__winzappOnMessagePinChanged'];
  const parser = win?.['WPP']?.whatsapp?.MsgKey;
  let api;
  try { api = win['require']('WAWebDBCRUDOperationsPinInChat'); } catch (_) { return false; }
  if (typeof notify !== 'function' || typeof api?.createOrUpdatePinInChat !== 'function'
      || typeof parser?.fromString !== 'function') return false;
  const previous = win['__winzappMessagePinObserver'];
  if (previous?.api === api && api.createOrUpdatePinInChat === previous.wrapper) {
    previous.notify = notify; return true;
  }
  previous?.dispose();
  const descriptor = Object.getOwnPropertyDescriptor(api, 'createOrUpdatePinInChat');
  const original = api.createOrUpdatePinInChat;
  // Object(...) is `any` to strict tsc (a bare literal infers null fields and breaks the build).
  const state = Object({api, notify, wrapper: null, dispose: null, disposed: false});
  state.wrapper = function (...args) {
    return Promise.resolve(original.apply(this, args)).then(rows => {
      if (!state.disposed && Array.isArray(rows)) for (const row of rows) {
        try {
          const key = typeof row?.parentMsgKey === 'string' ? parser.fromString(row.parentMsgKey) : row?.parentMsgKey;
          const chatId = typeof key?.remote === 'string' ? key.remote : key?.remote?._serialized;
          if (typeof chatId !== 'string' || !/^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid|g\.us)$/.test(chatId)
              || typeof key?.id !== 'string' || !key.id || (row.pinType !== 1 && row.pinType !== 2)
              || !Number.isSafeInteger(row.senderTimestampMs) || row.senderTimestampMs <= 0
              || !Number.isSafeInteger(row.pinExpiryDuration) || row.pinExpiryDuration < 0) continue;
          const value = {chatId, messageId: key.id, pinned: row.pinType === 1,
            timestamp: row.senderTimestampMs, duration: row.pinExpiryDuration};
          Promise.resolve(state.notify(JSON.stringify(value))).catch(() => {});
        } catch (_) { /* preserve the committed native result */ }
      }
      return rows;
    });
  };
  state.dispose = () => {
    state.disposed = true;
    if (api.createOrUpdatePinInChat === state.wrapper) {
      if (descriptor) Object.defineProperty(api, 'createOrUpdatePinInChat', descriptor);
      else delete api.createOrUpdatePinInChat;
    }
    if (win['__winzappMessagePinObserver'] === state) delete win['__winzappMessagePinObserver'];
  };
  try {
    if (descriptor?.configurable === false) api.createOrUpdatePinInChat = state.wrapper;
    else Object.defineProperty(api, 'createOrUpdatePinInChat', {
      value: state.wrapper, writable: true, configurable: true, enumerable: descriptor?.enumerable ?? false});
    if (api.createOrUpdatePinInChat !== state.wrapper) { state.dispose(); return false; }
  } catch (_) { state.dispose(); return false; }
  win['__winzappMessagePinObserver'] = state;
  return true;
}

export function messagePinPayload(raw, session) {
  if (typeof raw !== 'string' || raw.length > 768) return null;
  try {
    const v = JSON.parse(raw);
    if (typeof v?.chatId !== 'string' || v.chatId.length > 128
        || !/^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid|g\.us)$/.test(v.chatId)
        || typeof v.messageId !== 'string' || !v.messageId || v.messageId.length > 128
        || typeof v.pinned !== 'boolean' || !Number.isSafeInteger(v.timestamp) || v.timestamp <= 0
        || !Number.isSafeInteger(v.duration) || v.duration < 0) return null;
    return {session, chatId: v.chatId, messageId: v.messageId, pinned: v.pinned,
      timestamp: v.timestamp, duration: v.duration};
  } catch (_) { return null; }
}

export async function installMessagePinBridge(client, io, logger) {
  return installMessageStatePageBridge(client, io, logger,
    '__winzappOnMessagePinChanged', 'message-pin-changed', installMessagePinInPage, messagePinPayload);
}
