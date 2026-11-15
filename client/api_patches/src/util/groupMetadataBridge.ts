import { installMessageStatePageBridge } from './messageStatePageBridge';

export function installGroupMetadataInPage() {
  const win = globalThis['window'];
  const store = win?.['WPP']?.whatsapp?.GroupMetadataStore;
  const notify = win?.['__winzappOnGroupMetadataChanged'];
  if (typeof store?.on !== 'function' || typeof store?.off !== 'function'
      || typeof store?.getModelsArray !== 'function' || typeof notify !== 'function') return false;
  const previous = win['__winzappGroupMetadataObserver'];
  if (previous?.revision === 2 && previous?.store === store) { previous.notify = notify; return true; }
  previous?.dispose();
  // Object(...) is `any` to strict tsc (a bare literal infers null fields and breaks the build).
  const state = Object({store, notify, revision: 2, handlers: new Map(), members: new Map(), attach: null, detach: null, dispose: null});
  const emit = value => {
    try { Promise.resolve(state.notify(JSON.stringify(value))).catch(() => {}); }
    catch (_) { /* native metadata updates must remain unaffected */ }
  };
  const idOf = m => typeof m?.id === 'string' ? m.id : m?.id?._serialized;
  const groupId = m => { const id = idOf(m); return typeof id === 'string' && /^[\w.:-]+@g\.us$/.test(id) ? id : null; };
  for (const field of ['subject', 'desc', 'announce', 'restrict']) {
    const handler = m => {
      const id = groupId(m);
      const value = field === 'desc' && m?.desc == null ? '' : m?.[field];
      if (!id || ((field === 'subject' || field === 'desc')
        ? typeof value !== 'string' : typeof value !== 'boolean')) return;
      emit({id, [field]: value});
    };
    const event = 'change:' + field;
    state.handlers.set(event, handler);
    store.on(event, handler);
  }
  state.attach = m => {
    if (!groupId(m) || state.members.has(m) || typeof m?.on !== 'function' || typeof m?.off !== 'function') return;
    // triggerParticipantsChange() emits no model argument: capture its owner.
    const handler = () => {
      try {
        const models = m.participants?.getModelsArray?.();
        if (!Array.isArray(models) || !models.length || models.length > 2048) return;
        const participants = models.map(p => ({id: idOf(p), isAdmin: p.isAdmin, isSuperAdmin: p.isSuperAdmin}));
        if (participants.some(p => typeof p.id !== 'string' || !/^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid)$/.test(p.id)
            || typeof p.isAdmin !== 'boolean' || typeof p.isSuperAdmin !== 'boolean')) return;
        emit({id: groupId(m), participants, participantsComplete: true});
      } catch (_) { /* incomplete participant hydration is not an empty group */ }
    };
    m.on('change:participants', handler);
    state.members.set(m, handler);
  };
  state.detach = m => {
    const handler = state.members.get(m);
    if (handler) m.off('change:participants', handler);
    state.members.delete(m);
  };
  store.on('add', state.attach);
  store.on('remove', state.detach);
  store.getModelsArray().forEach(state.attach);
  state.dispose = () => {
    for (const [event, handler] of state.handlers) store.off(event, handler);
    store.off('add', state.attach);
    store.off('remove', state.detach);
    for (const model of [...state.members.keys()]) state.detach(model);
    if (win['__winzappGroupMetadataObserver'] === state) delete win['__winzappGroupMetadataObserver'];
  };
  win['__winzappGroupMetadataObserver'] = state;
  return true;
}

export function groupMetadataPayload(raw, session) {
  if (typeof raw !== 'string' || raw.length > 262144) return null;
  try {
    const v = JSON.parse(raw);
    if (typeof v?.id !== 'string' || v.id.length > 128 || !/^[\w.:-]+@g\.us$/.test(v.id)) return null;
    const item = {id: v.id};
    for (const name of ['subject', 'desc']) if (typeof v[name] === 'string' && v[name].length <= 32768) item[name] = v[name];
    for (const name of ['announce', 'restrict']) if (typeof v[name] === 'boolean') item[name] = v[name];
    if (v.participantsComplete === true && Array.isArray(v.participants) && v.participants.length && v.participants.length <= 2048) {
      if (v.participants.some(p => typeof p?.id !== 'string' || p.id.length > 128
          || !/^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid)$/.test(p.id)
          || typeof p.isAdmin !== 'boolean' || typeof p.isSuperAdmin !== 'boolean')) return null;
      item['participants'] = v.participants.map(p => ({id: p.id, isAdmin: p.isAdmin, isSuperAdmin: p.isSuperAdmin}));
      item['participantsComplete'] = true;
    }
    if (Object.keys(item).length === 1) return null;
    return {session, data: [item]};
  } catch (_) { return null; }
}

export async function installGroupMetadataBridge(client, io, logger) {
  return installMessageStatePageBridge(client, io, logger,
    '__winzappOnGroupMetadataChanged', 'groups.update', installGroupMetadataInPage, groupMetadataPayload);
}
