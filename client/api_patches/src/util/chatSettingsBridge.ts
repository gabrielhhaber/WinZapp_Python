import { installMessageStatePageBridge } from './messageStatePageBridge';

/** Account settings only; collection add/remove is never a settings change. */
export function installChatSettingsInPage() {
  const win = globalThis['window'];
  const chats = win?.['WPP']?.whatsapp?.ChatStore;
  const mutes = win?.['WPP']?.whatsapp?.MuteStore;
  const notify = win?.['__winzappOnChatSettingsChanged'];
  if (![chats, mutes].every(store => typeof store?.on === 'function'
      && typeof store?.off === 'function') || typeof notify !== 'function') return false;
  const previous = win['__winzappChatSettingsObserver'];
  if (previous?.revision === 2 && previous?.chats === chats && previous?.mutes === mutes) {
    previous.notify = notify;
    return true;
  }
  previous?.dispose();
  // Object(...) is `any` to strict tsc (a bare literal infers null fields and breaks the build).
  const state = Object({chats, mutes, notify, revision: 2, listeners: [], dispose: null});
  const observe = (store, event, setting, valueOf) => {
    const handler = model => {
      try {
        const chatId = typeof model?.id === 'string' ? model.id : model?.id?._serialized;
        if (typeof chatId !== 'string'
            || !/^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid|g\.us)$/.test(chatId)) return;
        const value = valueOf(model);
        if (value === undefined) return;
        Promise.resolve(state.notify(JSON.stringify({chatId, setting, value}))).catch(() => {});
      } catch (_) { /* observations cannot fail WhatsApp's own action */ }
    };
    store.on(event, handler);
    state.listeners.push([store, event, handler]);
  };
  observe(chats, 'change:archive', 'archive', m => typeof m.archive === 'boolean' ? m.archive : undefined);
  observe(chats, 'change:isLocked', 'lock', m => typeof m.isLocked === 'boolean' ? m.isLocked : undefined);
  observe(chats, 'change:pin', 'pin', m => {
    if (m.pin == null || m.pin === false || m.pin === 0) return false;
    if (m.pin === true || (Number.isSafeInteger(m.pin) && m.pin > 0)) return true;
    return undefined;
  });
  // MuteStore owns the expiry. ChatStore's derived muteExpiration can lag it.
  observe(mutes, 'change:expiration', 'mute', m =>
    Number.isSafeInteger(m.expiration) && m.expiration >= -1 ? m.expiration : undefined);
  state.dispose = () => {
    for (const [store, event, handler] of state.listeners) store.off(event, handler);
    if (win['__winzappChatSettingsObserver'] === state) delete win['__winzappChatSettingsObserver'];
  };
  win['__winzappChatSettingsObserver'] = state;
  return true;
}

export function chatSettingsPayload(raw, session) {
  if (typeof raw !== 'string' || raw.length > 512) return null;
  try {
    const v = JSON.parse(raw);
    if (typeof v?.chatId !== 'string' || v.chatId.length > 128
        || !/^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid|g\.us)$/.test(v.chatId)) return null;
    if (v.setting === 'mute') {
      if (!Number.isSafeInteger(v.value) || v.value < -1) return null;
    } else if (v.setting === 'archive' || v.setting === 'pin' || v.setting === 'lock') {
      if (typeof v.value !== 'boolean') return null;
    } else return null;
    return {session, chatId: v.chatId, setting: v.setting, value: v.value};
  } catch (_) { return null; }
}

export async function installChatSettingsBridge(client, io, logger) {
  return installMessageStatePageBridge(client, io, logger,
    '__winzappOnChatSettingsChanged', 'chat-settings-changed',
    installChatSettingsInPage, chatSettingsPayload);
}
