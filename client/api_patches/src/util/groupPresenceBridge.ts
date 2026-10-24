// Group activity has a page-owned local binding, independent of the provider's
// first-client exposed callback. No private snapshot or last-seen data here.
export const GROUP_PRESENCE_SOURCE = String.raw`function (generation) {
  const win = window, wpp = win.WPP;
  const store = wpp && wpp.isFullReady && wpp.whatsapp && wpp.whatsapp.PresenceStore;
  if (!store || typeof store.on !== 'function' || typeof store.off !== 'function'
      || typeof store.get !== 'function' || typeof win.__winzappGroupPresenceChanged !== 'function') return false;
  const previous = win.__winzappGroupPresenceBridge;
  if (previous && previous.generation > generation) return false;
  if (previous && previous.generation === generation && previous.store === store) return true;
  if (previous) previous.dispose();
  const pending = new Map(), last = new Map();
  const idOf = value => String(value && (value._serialized || value) || '');
  const groupId = id => /^[0-9]+(?:-[0-9]+)?@g\.us$/.test(id);
  const personId = id => /^[0-9]+@(c\.us|s\.whatsapp\.net|lid)$/.test(id);
  let disposed = false, queued = false;
  const invalidate = fields => {
    try {
      const result = win.__winzappGroupPresenceChanged({generation, ...fields});
      if (result && typeof result.catch === 'function') result.catch(() => {});
    } catch (_) {}
  };
  const current = () => !disposed && win.__winzappGroupPresenceBridge === bridge
    && win.WPP && win.WPP.whatsapp && win.WPP.whatsapp.PresenceStore === store;
  const flush = () => {
    queued = false;
    const models = Array.from(pending.values()); pending.clear();
    if (!current()) {if (!disposed) reset(); return;}
    for (const model of models) {
      try {
        if (store.get(model.id) !== model) continue;
        if (!Array.isArray(model.typingUserIds) || !Array.isArray(model.recordingUserIds)) {
          last.delete(idOf(model.id)); invalidate({invalidated: idOf(model.id)}); continue;
        }
        const id = idOf(model.id), now = new Map();
        for (const person of model.typingUserIds) {
          const key = idOf(person); if (personId(key)) now.set(key, 'typing');
        }
        for (const person of model.recordingUserIds) {
          const key = idOf(person); if (personId(key)) now.set(key, 'recording_audio');
        }
        const before = last.get(id) || new Map();
        if (now.size === before.size && Array.from(now).every(([key, value]) => before.get(key) === value)) continue;
        const participants = [];
        now.forEach((state, key) => participants.push({id: key, state, shortName: ''}));
        before.forEach((_state, key) => {if (!now.has(key)) participants.push({id: key, state: 'paused', shortName: ''});});
        if (now.size) {last.delete(id); last.set(id, now);} else last.delete(id);
        if (last.size > 512) last.delete(last.keys().next().value);
        if (!participants.length) continue;
        const event = {id, isOnline: false, isGroup: true, isUser: false, shortName: '',
          state: participants[0].state, t: Date.now(), participants};
        const delivery = win.__winzappGroupPresenceChanged({generation, event});
        if (delivery && typeof delivery.catch === 'function') delivery.catch(() => {});
        // Preserve the prior group activity shape/source for the legacy path.
        // Never route a private native snapshot through this exposed function.
        if (typeof win.onPresenceChanged === 'function') {
          try {
            const legacy = win.onPresenceChanged(event);
            if (legacy && typeof legacy.catch === 'function') legacy.catch(() => {});
          } catch (_) {}
        }
      } catch (_) {}
    }
  };
  const changed = model => {
    try {
      if (!current() || !model || !groupId(idOf(model.id))) return;
      pending.set(idOf(model.id), model);
      if (pending.size > 512) pending.delete(pending.keys().next().value);
      if (!queued) {queued = true; queueMicrotask(flush);}
    } catch (_) {}
  };
  const removed = model => {
    const id = idOf(model && model.id); pending.delete(id); last.delete(id);
    if (groupId(id)) invalidate({invalidated: id});
  };
  const reset = () => {pending.clear(); last.clear(); invalidate({reset: true});};
  const fields = ['add', 'change:typingUserIds', 'change:recordingUserIds'];
  const bridge = {store, generation, dispose() {
    disposed = true;
    fields.forEach(name => {try {store.off(name, changed);} catch (_) {}});
    try {store.off('remove', removed); store.off('reset', reset);} catch (_) {}
    reset();
    if (win.__winzappGroupPresenceBridge === bridge) delete win.__winzappGroupPresenceBridge;
  }};
  win.__winzappGroupPresenceBridge = bridge;
  try {
    fields.forEach(name => store.on(name, changed));
    store.on('remove', removed); store.on('reset', reset);
  } catch (_) {bridge.dispose(); return false;}
  return true;
}`;

const pages = new WeakMap<any, any>();
let generation = 0;
const groupId = (id: any) => typeof id === 'string' && /^[0-9]+(?:-[0-9]+)?@g\.us$/.test(id);
const personId = (id: any) => typeof id === 'string' && /^[0-9]+@(c\.us|s\.whatsapp\.net|lid)$/.test(id);
const states = new Set(['typing', 'recording_audio', 'paused']);

function validEvent(event: any): boolean {
  return !!event && groupId(event.id) && event.isGroup === true && event.isUser === false
    && event.isOnline === false && states.has(event.state)
    && typeof event.t === 'number' && Number.isFinite(event.t) && event.t > 0
    && Array.isArray(event.participants) && event.participants.length > 0
    && event.state === event.participants[0]?.state
    && event.participants.length <= 4096 && event.participants.every((p: any) =>
      p && personId(p.id) && states.has(p.state) && p.shortName === '');
}

export function installGroupPresenceBridge(client: any, req: any): void {
  const page = client.page;
  if (!page) return;
  let state = pages.get(page);
  if (!state) {
    state = {owner: null, binding: null}; pages.set(page, state);
    page.on('load', () => state.owner && state.owner.reload());
    page.on('close', () => {
      if (state.owner) {state.owner.closed = true; clearTimeout(state.owner.timer); state.owner.groups.clear();}
    });
  }
  if (state.owner) {state.owner.closed = true; clearTimeout(state.owner.timer);}
  const owner = {client, req, generation: ++generation, closed: false, groups: new Set<string>(),
    timer: null as any, reload: null as any};
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
        state.binding = page.exposeFunction('__winzappGroupPresenceChanged', (envelope: any) => {
          const active = state.owner;
          if (!active || active.closed || active.client.page !== page
              || (typeof page.isClosed === 'function' && page.isClosed())
              || !envelope || envelope.generation !== active.generation) return;
          // Internal invalidation is local-only and emits no synthetic activity.
          if (envelope.reset === true) {active.groups.clear(); return;}
          if (groupId(envelope.invalidated)) {active.groups.delete(envelope.invalidated); return;}
          if (!validEvent(envelope.event)) return;
          const {id, state: status, t, participants} = envelope.event;
          try {
            active.req.io.emit('onpresencechanged', {id, state: status, t, isOnline: false,
              isGroup: true, isUser: false, shortName: '', session: active.client.session,
              participants: participants.map((p: any) => ({id: p.id, state: p.state, shortName: ''}))});
            active.groups.delete(id); active.groups.add(id);
            if (active.groups.size > 512) active.groups.delete(active.groups.values().next().value);
          } catch (_) {
            active.groups.delete(id); // Failed native delivery cannot claim legacy suppression.
            active.req.logger.warn('[groupPresenceBridge] local delivery failed');
          }
        }).catch(() => {state.binding = null; throw new Error('Group presence binding unavailable');});
      }
      await state.binding;
      if (!current() || sequence !== owner.generation) return;
      installed = (await page.evaluate(`(${GROUP_PRESENCE_SOURCE})(${sequence})`)) === true;
    } catch (_) {}
    if (!current() || sequence !== owner.generation) return;
    // Keep the old 60-second readiness allowance, but only one owned chain.
    if (!installed && number < 120) owner.timer = setTimeout(() => {void attempt(number + 1);}, 500);
    else if (!installed) req.logger.warn('[groupPresenceBridge] unavailable after bounded readiness attempts');
  };
  owner.reload = () => {clearTimeout(owner.timer); owner.groups.clear(); owner.generation = ++generation; void attempt();};
  void attempt();
}

export function forwardLegacyGroupPresence(client: any, event: any): boolean {
  const id = event && event.id && String(event.id._serialized || event.id);
  if (!groupId(id)) return true;
  const state = pages.get(client.page);
  return !state || !!state.owner && !state.owner.closed && state.owner.client === client
    && !state.owner.groups.has(id);
}
