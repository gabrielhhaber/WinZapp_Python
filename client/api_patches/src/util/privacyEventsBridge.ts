import { installMessageStatePageBridge } from './messageStatePageBridge';

export function installPrivacyEventsInPage() {
  const win = globalThis['window'];
  const notify = win?.['__winzappOnPrivacySettingsChanged'];
  const read = win?.['WPP']?.whatsapp?.functions?.getUserPrivacySettings;
  let prefs, key, hashed;
  try {
    prefs = win['require']('WAWebUserPrefsIndexedDBStorage').userPrefsIdb;
    key = win['require']('WAWebUserPrefsKeys').HASHED_KEYS.USER_PRIVACY_SETTINGS;
    hashed = win['require']('WAWebUserPrefsStorageUtils').hashUserPrefKey(key);
  } catch (_) { return false; }
  const names = ['set', 'applySyncSet', 'bulkSetItemsToIndexedDB'];
  if (typeof notify !== 'function' || typeof read !== 'function'
      || !names.every(name => typeof prefs?.[name] === 'function')) return false;
  const previous = win['__winzappPrivacyEventsObserver'];
  if (previous?.prefs === prefs && names.every(name => prefs[name] === previous.wrappers.get(name))) {
    previous.notify = notify; return true;
  }
  previous?.dispose();
  const state = {prefs, notify, wrappers: new Map(), originals: new Map(), descriptors: new Map(),
    pending: false, disposed: false, changed: null, dispose: null};
  const fields = ['lastSeen', 'online', 'about', 'profilePicture', 'readReceipts', 'groupAdd'];
  state.changed = () => {
    if (state.pending || state.disposed) return;
    state.pending = true;
    Promise.resolve().then(() => {
      state.pending = false;
      if (state.disposed) return;
      try {
        const current = read(), settings = {};
        for (const name of fields) if (typeof current?.[name] === 'string') settings[name] = current[name];
        if (Object.keys(settings).length)
          Promise.resolve(state.notify(JSON.stringify({settings}))).catch(() => {});
      } catch (_) { /* preference observation cannot fail a native write */ }
    });
  };
  const watched = value => value === key || value === hashed;
  for (const name of names) {
    const original = prefs[name];
    state.originals.set(name, original);
    state.descriptors.set(name, Object.getOwnPropertyDescriptor(prefs, name));
    const wrapper = function (...args) {
      const interested = name === 'bulkSetItemsToIndexedDB'
        ? Array.isArray(args[0]) && args[0].some(v => watched(v?.key)) : watched(args[0]);
      const result = original.apply(this, args);
      if (!interested) return result;
      if (result && typeof result.then === 'function')
        return result.then(value => { state.changed(); return value; });
      state.changed();
      return result;
    };
    state.wrappers.set(name, wrapper);
  }
  state.dispose = () => {
    state.disposed = true;
    for (const name of names) if (prefs[name] === state.wrappers.get(name)) {
      const descriptor = state.descriptors.get(name);
      if (descriptor) Object.defineProperty(prefs, name, descriptor);
      else delete prefs[name];
    }
    if (win['__winzappPrivacyEventsObserver'] === state) delete win['__winzappPrivacyEventsObserver'];
  };
  try {
    for (const name of names) prefs[name] = state.wrappers.get(name);
    if (!names.every(name => prefs[name] === state.wrappers.get(name))) { state.dispose(); return false; }
  } catch (_) { state.dispose(); return false; }
  win['__winzappPrivacyEventsObserver'] = state;
  return true;
}

export function privacyEventPayload(raw, session) {
  if (typeof raw !== 'string' || raw.length > 2048) return null;
  try {
    const settings = JSON.parse(raw)?.settings;
    if (!settings || typeof settings !== 'object' || Array.isArray(settings)) return null;
    const allowed = {
      lastSeen: ['all', 'contacts', 'contact_blacklist', 'none'], online: ['all', 'match_last_seen'],
      about: ['all', 'contacts', 'contact_blacklist', 'none'],
      profilePicture: ['all', 'contacts', 'contact_blacklist', 'none'],
      readReceipts: ['all', 'none'], groupAdd: ['all', 'contacts', 'contact_blacklist', 'known'],
    };
    const filtered = {};
    for (const [field, values] of Object.entries(allowed)) if (values.includes(settings[field])) filtered[field] = settings[field];
    return Object.keys(filtered).length ? {session, settings: filtered} : null;
  } catch (_) { return null; }
}

export async function installPrivacyEventsBridge(client, io, logger) {
  return installMessageStatePageBridge(client, io, logger,
    '__winzappOnPrivacySettingsChanged', 'privacy-settings-changed', installPrivacyEventsInPage, privacyEventPayload);
}
