// Star observations only: no writes and no message bodies leave the page.
import { installMessageStatePageBridge } from './messageStatePageBridge';

/** Self-contained: Puppeteer serializes only this function into the page. */
export function installMessageStarInPage() {
  const win = globalThis['window'];
  const store = win?.['WPP']?.whatsapp?.MsgStore;
  const notify = win?.['__winzappOnMessageStarChanged'];
  if (typeof store?.on !== 'function' || typeof store?.off !== 'function'
      || typeof notify !== 'function') return false;
  const previous = win['__winzappMessageStarObserver'];
  if (previous?.store === store) {
    previous.notify = notify;
    return true;
  }
  previous?.dispose();
  const state = { store, notify, handler: null, dispose: null };
  state.handler = (model) => {
    try {
      const key = model?.id;
      const chatId = typeof key?.remote === 'string' ? key.remote : key?.remote?._serialized;
      if (typeof key?.id !== 'string' || typeof model?.star !== 'boolean'
          || typeof chatId !== 'string'
          || !/^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid|g\.us)$/.test(chatId)) return;
      Promise.resolve(state.notify(JSON.stringify({
        chatId, messageId: key.id, starred: model.star,
      }))).catch(() => {});
    } catch (_) { /* an observation must never break WhatsApp's state change */ }
  };
  state.dispose = () => {
    store.off('change:star', state.handler);
    if (win['__winzappMessageStarObserver'] === state) delete win['__winzappMessageStarObserver'];
  };
  store.on('change:star', state.handler);
  win['__winzappMessageStarObserver'] = state;
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
