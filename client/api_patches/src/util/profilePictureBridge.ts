import { installMessageStatePageBridge } from './messageStatePageBridge';

/** Invalidate cached URLs; a missing thumbnail is not proof of removal. */
export function installProfilePictureInPage() {
  const win = globalThis['window'];
  const store = win?.['WPP']?.whatsapp?.ProfilePicThumbStore;
  const notify = win?.['__winzappOnProfilePictureChanged'];
  if (typeof store?.on !== 'function' || typeof store?.off !== 'function'
      || typeof notify !== 'function') return false;
  const previous = win['__winzappProfilePictureObserver'];
  if (previous?.store === store) { previous.notify = notify; return true; }
  previous?.dispose();
  const events = ['change:tag', 'change:eurl', 'change:fullDirectPath', 'change:previewDirectPath'];
  // Object(...) is `any` to strict tsc (a bare literal infers null fields and breaks the build).
  const state = Object({store, notify, pending: new Set(), disposed: false, handler: null, dispose: null});
  state.handler = model => {
    if (state.pending.has(model)) return;
    state.pending.add(model);
    Promise.resolve().then(() => {
      state.pending.delete(model);
      if (state.disposed) return;
      try {
        const chatId = typeof model?.id === 'string' ? model.id : model?.id?._serialized;
        if (typeof chatId !== 'string' || !/^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid|g\.us)$/.test(chatId)) return;
        Promise.resolve(state.notify(JSON.stringify({chatId}))).catch(() => {});
      } catch (_) { /* no signed URL or media content leaves this observer */ }
    });
  };
  state.dispose = () => {
    state.disposed = true;
    state.pending.clear();
    for (const event of events) store.off(event, state.handler);
    if (win['__winzappProfilePictureObserver'] === state) delete win['__winzappProfilePictureObserver'];
  };
  for (const event of events) store.on(event, state.handler);
  win['__winzappProfilePictureObserver'] = state;
  return true;
}

export function profilePicturePayload(raw, session) {
  if (typeof raw !== 'string' || raw.length > 512) return null;
  try {
    const v = JSON.parse(raw);
    if (typeof v?.chatId !== 'string' || v.chatId.length > 128
        || !/^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid|g\.us)$/.test(v.chatId)) return null;
    return {session, chatId: v.chatId};
  } catch (_) { return null; }
}

export async function installProfilePictureBridge(client, io, logger) {
  return installMessageStatePageBridge(client, io, logger,
    '__winzappOnProfilePictureChanged', 'profile-picture-changed', installProfilePictureInPage, profilePicturePayload);
}
