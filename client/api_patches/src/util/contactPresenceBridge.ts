// Private model fields can change without chatstate.type changing. This bridge
// is independent of WPPConnect's Promise.all subscription/listener bootstrap.
import { CONTACT_PRESENCE_SOURCE } from './contactPresenceRuntime';
import { forwardLegacyGroupPresence } from './groupPresenceBridge';

export const CONTACT_PRESENCE_BRIDGE_SOURCE = String.raw`function (generation) {
  const win = window, wpp = win.WPP;
  const store = wpp && wpp.isFullReady && wpp.whatsapp && wpp.whatsapp.PresenceStore;
  if (!store || typeof store.on !== 'function' || typeof store.off !== 'function'
      || typeof store.get !== 'function' || typeof store.getModelsArray !== 'function'
      || typeof win.__winzappContactPresenceChanged !== 'function') return false;
  const previous = win.__winzappContactPresenceBridge;
  if (previous && previous.generation > generation) return false;
  if (previous && previous.store === store && previous.generation === generation) return true;
  if (previous) previous.dispose();
  const read = (${CONTACT_PRESENCE_SOURCE});
  const pending = new Map(), last = new Map();
  let queued = false, disposed = false;
  const idOf = model => String(model && model.id && (model.id._serialized || model.id) || '');
  const privateId = id => /^[0-9]+@(c\.us|s\.whatsapp\.net|lid)$/.test(id);
  const current = () => !disposed && win.__winzappContactPresenceBridge === bridge
    && win.WPP && win.WPP.whatsapp && win.WPP.whatsapp.PresenceStore === store;
  const flush = () => {
    queued = false;
    if (!current()) { pending.clear(); return; }
    const models = Array.from(pending.values());
    pending.clear();
    for (const model of models) {
      try {
        if (store.get(model.id) !== model) continue;
        const id = idOf(model), snapshot = read(id);
        if (!snapshot || !['ready', 'pending'].includes(snapshot.status)) continue;
        const event = snapshot.status === 'ready'
          ? {id, isGroup: false, isUser: true, snapshotStatus: 'ready',
             state: snapshot.state, isOnline: snapshot.isOnline,
             lastSeen: snapshot.lastSeen, restricted: snapshot.restricted}
          : {id, isGroup: false, isUser: true, snapshotStatus: 'pending',
             state: '', isOnline: null, lastSeen: null,
             restricted: snapshot.restricted === true
               || model.chatstate && model.chatstate.deny === true ? true : null};
        const signature = JSON.stringify(event);
        if (last.get(id) === signature) continue;
        last.delete(id);
        last.set(id, signature);
        if (last.size > 512) last.delete(last.keys().next().value);
        // t orders emitted events; only snapshot.lastSeen is last-seen data.
        const delivery = win.__winzappContactPresenceChanged({generation, event: {...event, t: Date.now()}});
        if (delivery && typeof delivery.catch === 'function') delivery.catch(() => {});
      } catch (_) { /* Never throw into WhatsApp's event loop. */ }
    }
  };
  const changed = candidate => {
    try {
      if (!current()) return;
      // Nested field events supply Chatstate, not the enclosing PresenceModel.
      const model = candidate && store.get(candidate.id) === candidate ? candidate
        : store.getModelsArray().find(item => item.chatstate === candidate);
      const id = idOf(model);
      if (!model || model.isGroup === true || !privateId(id)) return;
      pending.set(id, model);
      if (!queued) { queued = true; queueMicrotask(flush); }
    } catch (_) {}
  };
  const removed = model => { pending.delete(idOf(model)); last.delete(idOf(model)); };
  const reset = () => { pending.clear(); last.clear(); };
  const fields = ['add', 'change:chatstate.type', 'change:chatstate.t', 'change:chatstate.deny',
    'change:isOnline', 'change:hasData', 'change:chatstate'];
  const bridge = {store, generation, dispose() {
    disposed = true;
    fields.forEach(name => { try { store.off(name, changed); } catch (_) {} });
    try { store.off('remove', removed); store.off('reset', reset); } catch (_) {}
    reset();
    if (win.__winzappContactPresenceBridge === bridge) delete win.__winzappContactPresenceBridge;
  }};
  win.__winzappContactPresenceBridge = bridge;
  try {
    fields.forEach(name => store.on(name, changed));
    store.on('remove', removed); store.on('reset', reset);
  } catch (_) { bridge.dispose(); return false; }
  return true;
}`;

const pages = new WeakMap<any, any>();
const BINDING = '__winzappContactPresenceChanged';
let generation = 0;
const privateId = (id: unknown) => typeof id === 'string' && /^[0-9]+@(c\.us|s\.whatsapp\.net|lid)$/.test(id);

function validEvent(event: any): boolean {
  return !!event && privateId(event.id) && event.isGroup === false
    && typeof event.t === 'number' && Number.isFinite(event.t)
    && (event.snapshotStatus === 'pending'
      ? event.lastSeen === null && event.isOnline === null && event.state === ''
        && (event.restricted === null || event.restricted === true)
      : event.snapshotStatus === 'ready' && typeof event.isOnline === 'boolean'
        && typeof event.restricted === 'boolean' && typeof event.state === 'string'
        && (event.lastSeen === null || typeof event.lastSeen === 'number'
          && Number.isFinite(event.lastSeen) && event.lastSeen > 0));
}

/** Repeated calls replace ownership, never the exposed function or load hook. */
export function installContactPresenceBridge(client: any, req: any): void {
  const page = client.page;
  if (!page) return;
  let state = pages.get(page);
  if (!state) {
    state = {owner: null, binding: null};
    pages.set(page, state);
    page.on('load', () => state.owner && state.owner.reload());
    page.on('close', () => {
      if (state.owner) {
        state.owner.closed = true;
        clearTimeout(state.owner.timer);
      }
    });
  }
  if (state.owner) { state.owner.closed = true; clearTimeout(state.owner.timer); }
  const owner = {client, req, generation: ++generation, installed: false,
    closed: false, timer: null as any, reload: null as any};
  state.owner = owner;
  const current = () => state.owner === owner && !owner.closed
    && owner.client.page === page
    && !(typeof page.isClosed === 'function' && page.isClosed());
  const attempt = async (number = 0) => {
    if (!current()) return;
    const sequence = owner.generation;
    let installed = false;
    try {
      if (!state.binding) {
        state.binding = page.exposeFunction(BINDING, (envelope: any) => {
          const active = state.owner;
          if (!active || active.closed || !active.installed || active.client.page !== page
              || (typeof page.isClosed === 'function' && page.isClosed())
              || !envelope || envelope.generation !== active.generation
              || !validEvent(envelope.event)) return;
          // Copy the known shape only; never forward arbitrary page properties.
          const {id, state: status, isOnline, lastSeen, restricted, snapshotStatus, t} = envelope.event;
          try {
            active.req.io.emit('onpresencechanged', {id, state: status, isOnline,
              lastSeen, restricted, snapshotStatus, t, isGroup: false, isUser: true,
              session: active.client.session});
          } catch (_) { active.req.logger.warn('[contactPresenceBridge] local delivery failed'); }
        }).catch(() => { state.binding = null; throw new Error('Presence binding unavailable'); });
      }
      await state.binding;
      if (!current() || sequence !== owner.generation) return;
      installed = await page.evaluate(`(${CONTACT_PRESENCE_BRIDGE_SOURCE})(${sequence})`);
    } catch (_) { /* Retry a bounded readiness/install failure without raw data. */ }
    if (!current() || sequence !== owner.generation) return;
    owner.installed = installed === true;
    if (!owner.installed && number < 20) {
      owner.timer = setTimeout(() => { void attempt(number + 1); }, 500);
    } else if (!owner.installed) {
      req.logger.warn('[contactPresenceBridge] unavailable after bounded readiness attempts');
    }
  };
  owner.reload = () => {
    clearTimeout(owner.timer);
    owner.installed = false;
    owner.generation = ++generation;
    void attempt();
  };
  void attempt(); // A hung provider/bootstrap cannot hold up session listeners.
}

export function forwardLegacyContactPresence(client: any, event: any): boolean {
  const id = event && event.id && String(event.id._serialized || event.id);
  if (!privateId(id)) return forwardLegacyGroupPresence(client, event);
  const state = pages.get(client.page);
  return !state || !!state.owner && !state.owner.closed
    && state.owner.client === client && !state.owner.installed;
}

/** Stale provider callbacks cannot deliver to an old socket/webhook context. */
export function legacyPresenceOwnerCurrent(client: any, req: any, page = client.page): boolean {
  const state = pages.get(page);
  return client.page === page && !(typeof page?.isClosed === 'function' && page.isClosed())
    && (!state || !!state.owner && !state.owner.closed
      && state.owner.client === client && state.owner.req === req);
}
