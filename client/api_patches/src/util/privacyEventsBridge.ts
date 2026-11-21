import { installMessageStatePageBridge } from './messageStatePageBridge';

export function installPrivacyEventsInPage() {
  const win = globalThis['window'];
  const notify = win?.['__winzappOnPrivacySettingsChanged'];
  const read = win?.['WPP']?.whatsapp?.functions?.getUserPrivacySettings;
  let prefs, key, hashed, backendKeys;
  try {
    prefs = win['require']('WAWebUserPrefsIndexedDBStorage').userPrefsIdb;
    const keys = win['require']('WAWebUserPrefsKeys');
    key = keys.HASHED_KEYS.USER_PRIVACY_SETTINGS;
    backendKeys = keys.BACKEND_ONLY_KEYS || {};
    hashed = win['require']('WAWebUserPrefsStorageUtils').hashUserPrefKey(key);
  } catch (_) { return false; }
  const names = ['set', 'applySyncSet', 'bulkSetItemsToIndexedDB'];
  if (typeof notify !== 'function' || typeof read !== 'function'
      || !names.every(name => typeof prefs?.[name] === 'function')) return false;
  const statusFunctions = ['getStatusPrivacySetting', 'getStatusAllowList', 'getStatusDenyList'];
  const statusApi = candidate => {
    try {
      if (candidate && statusFunctions.every(name => typeof candidate[name] === 'function')) return candidate;
      const inner = candidate?.default;
      if (inner && statusFunctions.every(name => typeof inner[name] === 'function')) return inner;
    } catch (_) { /* a lazy wa-js binding may throw while resolving */ }
    return null;
  };
  let status;
  try { status = statusApi(win['require']('WAWebUserPrefsStatus')); } catch (_) { /* optional */ }
  status = status || statusApi(win['WPP']?.whatsapp?.functions);
  let archivePrefs;
  try { archivePrefs = win['require']('WAWebUserPrefsMultiDevice'); } catch (_) { /* optional */ }
  const watched = new Set([key, hashed]);
  for (const name of ['STATUS_PRIVACY_SETTING', 'STATUS_ALLOW_LIST', 'STATUS_DENY_LIST',
      'ARCHIVE_V2_ENABLED_SETTING', 'UNARCHIVE_CHATS_SETTING']) {
    const stored = backendKeys[name];
    if (typeof stored !== 'string') continue;
    watched.add(stored);
    try { watched.add(win['require']('WAWebUserPrefsStorageUtils').hashUserPrefKey(stored)); } catch (_) { /* raw key remains */ }
  }
  const previous = win['__winzappPrivacyEventsObserver'];
  if (previous?.prefs === prefs && names.every(name => prefs[name] === previous.wrappers.get(name))) {
    previous.notify = notify; return true;
  }
  previous?.dispose();
  // Object(...) is `any` to strict tsc (a bare literal infers null fields and breaks the build).
  const state = Object({prefs, notify, wrappers: new Map(), originals: new Map(), descriptors: new Map(),
    pending: false, disposed: false, revision: 0, changed: null, dispose: null});
  const fields = ['lastSeen', 'online', 'about', 'profilePicture', 'readReceipts', 'groupAdd'];
  const toIds = value => {
    const items = Array.isArray(value) ? value
      : typeof value?.toArray === 'function' ? value.toArray()
        : typeof value?.getModelsArray === 'function' ? value.getModelsArray() : null;
    if (!Array.isArray(items)) return null;
    const ids = Object([]);
    for (const item of items) {
      const wid = item?.id && typeof item.id === 'object' ? item.id : item;
      const id = typeof wid === 'string' ? wid
        : wid?._serialized ?? (typeof wid?.toString === 'function' ? wid.toString() : '');
      if (typeof id !== 'string' || !/^\d{5,20}(?::\d+)?@(c\.us|s\.whatsapp\.net|lid)$/.test(id)) return null;
      ids.push(id);
    }
    return ids;
  };
  state.changed = () => {
    if (state.disposed) return;
    state.revision++;
    if (state.pending) return;
    state.pending = true;
    Promise.resolve().then(async () => {
      state.pending = false;
      const revision = state.revision;
      if (state.disposed) return;
      try {
        const current = read(), settings = {};
        if (!status) {
          try { status = statusApi(win['require']('WAWebUserPrefsStatus')); } catch (_) { /* lazy module */ }
        }
        if (!archivePrefs) {
          try { archivePrefs = win['require']('WAWebUserPrefsMultiDevice'); } catch (_) { /* lazy module */ }
        }
        for (const name of fields) if (typeof current?.[name] === 'string') settings[name] = current[name];
        const message = Object.create(null);
        if (Object.keys(settings).length) message.settings = settings;
        if (status) {
          try {
            const setting = String(await status.getStatusPrivacySetting());
            let list = Object([]);
            if (setting === 'allow-list') list = await toIds(await status.getStatusAllowList());
            else if (setting === 'deny-list') list = await toIds(await status.getStatusDenyList());
            if (Array.isArray(list) && ['contact', 'allow-list', 'deny-list'].includes(setting))
              message.status = {setting, list};
          } catch (_) { /* unavailable list data must not impersonate an empty audience */ }
        }
        try {
          const enabled = await archivePrefs?.getArchiveV2EnabledSetting?.();
          const unarchive = await archivePrefs?.getUnarchiveChatsSetting?.();
          if (typeof enabled === 'boolean' && typeof unarchive === 'boolean')
            message.keepArchived = enabled && !unarchive;
        } catch (_) { /* an unavailable archive preference does not suppress privacy */ }
        if (!state.disposed && revision === state.revision && Object.keys(message).length)
          Promise.resolve(state.notify(JSON.stringify(message))).catch(() => {});
      } catch (_) { /* preference observation cannot fail a native write */ }
    });
  };
  for (const name of names) {
    const original = prefs[name];
    state.originals.set(name, original);
    state.descriptors.set(name, Object.getOwnPropertyDescriptor(prefs, name));
    // Built on an untyped holder so tsc types the receiver `this` as any,
    // while the file still runs as plain JavaScript (no annotations).
    const built = Object({});
    built.wrapper = function (...args) {
      const interested = name === 'bulkSetItemsToIndexedDB'
        ? Array.isArray(args[0]) && args[0].some(v => watched.has(v?.key)) : watched.has(args[0]);
      const result = original.apply(this, args);
      if (!interested) return result;
      if (result && typeof result.then === 'function')
        return result.then(value => { state.changed(); return value; });
      state.changed();
      return result;
    };
    state.wrappers.set(name, built.wrapper);
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
  if (typeof raw !== 'string' || raw.length > 131072) return null;
  try {
    const value = JSON.parse(raw);
    const settings = value?.settings ?? {};
    if (!settings || typeof settings !== 'object' || Array.isArray(settings)) return null;
    const allowed = {
      lastSeen: ['all', 'contacts', 'contact_blacklist', 'none'], online: ['all', 'match_last_seen'],
      about: ['all', 'contacts', 'contact_blacklist', 'none'],
      profilePicture: ['all', 'contacts', 'contact_blacklist', 'none'],
      readReceipts: ['all', 'none'], groupAdd: ['all', 'contacts', 'contact_blacklist', 'known'],
    };
    const filtered = {};
    for (const [field, values] of Object.entries(allowed)) if (values.includes(settings[field])) filtered[field] = settings[field];
    const keepArchived = typeof value.keepArchived === 'boolean' ? value.keepArchived : undefined;
    let status;
    const incoming = value.status;
    const aliases = {contact: 'contact', contacts: 'contact', 'deny-list': 'deny-list', denylist: 'deny-list',
      blacklist: 'deny-list', contact_blacklist: 'deny-list', 'allow-list': 'allow-list',
      allowlist: 'allow-list', whitelist: 'allow-list'};
    if (incoming && typeof incoming === 'object' && !Array.isArray(incoming)
        && typeof incoming.setting === 'string' && Array.isArray(incoming.list)
        && incoming.list.length <= 2000) {
      const rawSetting = incoming.setting.toLowerCase();
      const setting = Object.prototype.hasOwnProperty.call(aliases, rawSetting) ? aliases[rawSetting] : null;
      const normalized = incoming.list.map(id => {
        if (typeof id !== 'string') return '';
        const match = /^(\d{5,20})(?::\d+)?@(c\.us|s\.whatsapp\.net|lid)$/.exec(id);
        if (!match) return '';
        return `${match[1]}@${match[2] === 'lid' ? 'lid' : 'c.us'}`;
      });
      if (setting && normalized.every(Boolean)
          && (setting !== 'contact' || normalized.length === 0)) {
        status = {setting, list: [...new Set(normalized)]};
      }
    }
    return Object.keys(filtered).length || keepArchived !== undefined || status
      ? {session, settings: filtered, ...(keepArchived === undefined ? {} : {keepArchived}),
        ...(status ? {status} : {})} : null;
  } catch (_) { return null; }
}

export async function installPrivacyEventsBridge(client, io, logger) {
  return installMessageStatePageBridge(client, io, logger,
    '__winzappOnPrivacySettingsChanged', 'privacy-settings-changed', installPrivacyEventsInPage, privacyEventPayload);
}
