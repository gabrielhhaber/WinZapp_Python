import { installMessageStatePageBridge } from './messageStatePageBridge';

export function installChatTimerInPage() {
  const win = globalThis['window'];
  const store = win?.['WPP']?.whatsapp?.ChatStore;
  const groups = win?.['WPP']?.whatsapp?.GroupMetadataStore;
  const notify = win?.['__winzappOnChatTimerChanged'];
  if (typeof store?.on !== 'function' || typeof store?.off !== 'function'
      || typeof notify !== 'function') return false;
  const previous = win['__winzappChatTimerObserver'];
  if (previous?.revision === 2 && previous?.store === store && previous?.groups === groups) {
    previous.notify = notify; return true;
  }
  previous?.dispose();
  // Object(...) is `any` to strict tsc (a bare literal infers null fields and breaks the build).
  const state = Object({store, groups, notify, revision: 2, pending: new Set(), disposed: false, handler: null, dispose: null});
  state.handler = model => {
    if (state.pending.has(model)) return;
    state.pending.add(model);
    // Duration and timestamp may be assigned in the same model.set batch.
    Promise.resolve().then(() => {
      state.pending.delete(model);
      if (state.disposed) return;
      try {
        const chatId = typeof model?.id === 'string' ? model.id : model?.id?._serialized;
        const duration = model?.ephemeralDuration;
        // GroupMetadata owns the live group timer in Meta's split runtime;
        // it carries no setting timestamp, unlike the conversation model.
        const settingTimestamp = model?.ephemeralSettingTimestamp ?? 0;
        if (typeof chatId !== 'string' || !/^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid|g\.us)$/.test(chatId)
            || !Number.isSafeInteger(duration) || duration < 0
            || !Number.isSafeInteger(settingTimestamp) || settingTimestamp < 0) return;
        Promise.resolve(state.notify(JSON.stringify({chatId, duration, settingTimestamp}))).catch(() => {});
      } catch (_) { /* notification failure cannot fail the native update */ }
    });
  };
  state.dispose = () => {
    state.disposed = true;
    store.off('change:ephemeralDuration', state.handler);
    store.off('change:ephemeralSettingTimestamp', state.handler);
    if (typeof groups?.off === 'function') groups.off('change:ephemeralDuration', state.handler);
    state.pending.clear();
    if (win['__winzappChatTimerObserver'] === state) delete win['__winzappChatTimerObserver'];
  };
  store.on('change:ephemeralDuration', state.handler);
  store.on('change:ephemeralSettingTimestamp', state.handler);
  if (typeof groups?.on === 'function' && typeof groups?.off === 'function')
    groups.on('change:ephemeralDuration', state.handler);
  win['__winzappChatTimerObserver'] = state;
  return true;
}

export function chatTimerPayload(raw, session) {
  if (typeof raw !== 'string' || raw.length > 512) return null;
  try {
    const v = JSON.parse(raw);
    if (typeof v?.chatId !== 'string' || v.chatId.length > 128
        || !/^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid|g\.us)$/.test(v.chatId)
        || !Number.isSafeInteger(v.duration) || v.duration < 0
        || !Number.isSafeInteger(v.settingTimestamp) || v.settingTimestamp < 0) return null;
    return {session, chatId: v.chatId, duration: v.duration, settingTimestamp: v.settingTimestamp};
  } catch (_) { return null; }
}

export async function installChatTimerBridge(client, io, logger) {
  return installMessageStatePageBridge(client, io, logger,
    '__winzappOnChatTimerChanged', 'chat-timer-changed', installChatTimerInPage, chatTimerPayload);
}
