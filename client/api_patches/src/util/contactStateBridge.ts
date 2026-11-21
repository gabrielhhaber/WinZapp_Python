import { installMessageStatePageBridge } from './messageStatePageBridge';

export function installContactStateInPage() {
  const win = globalThis['window'];
  const store = win?.['WPP']?.whatsapp?.ContactStore;
  const notify = win?.['__winzappOnContactStateChanged'];
  if (typeof store?.on !== 'function' || typeof store?.off !== 'function'
      || typeof notify !== 'function') return false;
  const previous = win['__winzappContactStateObserver'];
  if (previous?.store === store) { previous.notify = notify; return true; }
  previous?.dispose();
  // Object(...) is `any` to strict tsc (a bare literal infers null fields and breaks the build).
  const state = Object({store, notify, handlers: new Map(), pending: new Map(), disposed: false, dispose: null});
  const observe = (field, kind) => {
    const handler = model => {
      let kinds = state.pending.get(model);
      if (!kinds) { kinds = new Set(); state.pending.set(model, kinds); }
      if (kinds.has(kind)) return;
      kinds.add(kind);
      Promise.resolve().then(() => {
        kinds.delete(kind);
        if (!kinds.size) state.pending.delete(model);
        if (state.disposed) return;
        try {
          const contactId = typeof model?.id === 'string' ? model.id : model?.id?._serialized;
          if (typeof contactId !== 'string' || !/^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid)$/.test(contactId)) return;
          let value;
          if (kind === 'profile') {
            if (typeof model.pushname !== 'string' || !model.pushname.trim()) return;
            value = {contactId, kind, name: model.pushname};
          } else {
            if (typeof model.isMyContact !== 'boolean' || typeof model.syncToAddressbook !== 'boolean') return;
            const name = model.isMyContact ? model.name : '';
            if (typeof name !== 'string' || (model.isMyContact && !name.trim())) return;
            value = {contactId, kind, name, saved: model.isMyContact,
              syncedToPhone: model.isMyContact && model.syncToAddressbook};
          }
          Promise.resolve(state.notify(JSON.stringify(value))).catch(() => {});
        } catch (_) { /* contact observations cannot fail the native change */ }
      });
    };
    const event = 'change:' + field;
    state.handlers.set(event, handler);
    store.on(event, handler);
  };
  for (const field of ['name', 'isMyContact', 'syncToAddressbook']) observe(field, 'address-book');
  observe('pushname', 'profile');
  state.dispose = () => {
    state.disposed = true;
    state.pending.clear();
    for (const [event, handler] of state.handlers) store.off(event, handler);
    if (win['__winzappContactStateObserver'] === state) delete win['__winzappContactStateObserver'];
  };
  win['__winzappContactStateObserver'] = state;
  return true;
}

export function contactStatePayload(raw, session) {
  if (typeof raw !== 'string' || raw.length > 8192) return null;
  try {
    const v = JSON.parse(raw);
    if (typeof v?.contactId !== 'string' || v.contactId.length > 128
        || !/^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid)$/.test(v.contactId)
        || typeof v.name !== 'string' || v.name.length > 4096) return null;
    if (v.kind === 'profile') {
      return v.name.trim() ? {session, contactId: v.contactId, kind: v.kind, name: v.name} : null;
    }
    if (v.kind !== 'address-book' || typeof v.saved !== 'boolean' || (v.saved && !v.name.trim())
        || ('syncedToPhone' in v && typeof v.syncedToPhone !== 'boolean')) return null;
    const value = {session, contactId: v.contactId, kind: v.kind, name: v.saved ? v.name : '', saved: v.saved};
    if ('syncedToPhone' in v) value['syncedToPhone'] = v.saved && v.syncedToPhone;
    return value;
  } catch (_) { return null; }
}

export async function installContactStateBridge(client, io, logger) {
  return installMessageStatePageBridge(client, io, logger,
    '__winzappOnContactStateChanged', 'contact-state-changed', installContactStateInPage, contactStatePayload);
}
