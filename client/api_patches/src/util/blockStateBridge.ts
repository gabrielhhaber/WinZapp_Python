import { installMessageStatePageBridge } from './messageStatePageBridge';

export function installBlockStateInPage() {
  const win = globalThis['window'];
  const contacts = win?.['WPP']?.whatsapp?.ContactStore;
  const blocks = win?.['WPP']?.whatsapp?.BlocklistStore;
  const notify = win?.['__winzappOnBlockStateChanged'];
  if (![contacts, blocks].every(s => typeof s?.on === 'function' && typeof s?.off === 'function')
      || typeof notify !== 'function') return false;
  const previous = win['__winzappBlockStateObserver'];
  if (previous?.contacts === contacts && previous?.blocks === blocks) { previous.notify = notify; return true; }
  previous?.dispose();
  // Object(...) is `any` to strict tsc (a bare literal infers null fields and breaks the build).
  const state = Object({contacts, blocks, notify, handler: null, refresh: null, dispose: null});
  const emit = value => {
    try { Promise.resolve(state.notify(JSON.stringify(value))).catch(() => {}); }
    catch (_) { /* no side effect on native block operations */ }
  };
  state.handler = model => {
    const contactId = typeof model?.id === 'string' ? model.id : model?.id?._serialized;
    if (typeof contactId !== 'string' || !/^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid)$/.test(contactId)
        || typeof model?.isContactBlocked !== 'boolean') return;
    emit({kind: 'contact', contactId, blocked: model.isContactBlocked});
  };
  state.refresh = () => emit({kind: 'refresh'});
  contacts.on('change:isContactBlocked', state.handler);
  // Reset names no individual delta. Never infer unblocks from eviction.
  blocks.on('reset', state.refresh);
  state.dispose = () => {
    contacts.off('change:isContactBlocked', state.handler);
    blocks.off('reset', state.refresh);
    if (win['__winzappBlockStateObserver'] === state) delete win['__winzappBlockStateObserver'];
  };
  win['__winzappBlockStateObserver'] = state;
  return true;
}

export function blockStatePayload(raw, session) {
  if (typeof raw !== 'string' || raw.length > 512) return null;
  try {
    const v = JSON.parse(raw);
    if (v?.kind === 'refresh') return {session, kind: 'refresh'};
    if (v?.kind !== 'contact' || typeof v.contactId !== 'string' || v.contactId.length > 128
        || !/^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid)$/.test(v.contactId)
        || typeof v.blocked !== 'boolean') return null;
    return {session, kind: 'contact', contactId: v.contactId, blocked: v.blocked};
  } catch (_) { return null; }
}

export async function installBlockStateBridge(client, io, logger) {
  return installMessageStatePageBridge(client, io, logger,
    '__winzappOnBlockStateChanged', 'block-state-changed', installBlockStateInPage, blockStatePayload);
}
