// Self-contained: Puppeteer serializes each exported function on its own into
// the WhatsApp page, so nothing here may reach a helper outside the function
// that uses it. Only type annotations Babel erases are allowed, so the function
// serialised into the page stays plain JavaScript and the Node tests run the
// exact browser function against a fake WPP.
//
// None of these return a native exception's text: WhatsApp's errors can carry
// phone numbers and contact names. They return a stable error code instead.

// Every account-wide privacy setting, plus who can see the account's Status.
//
// WPP.privacy.get() reads the six account settings and then awaits
// getStatusPrivacySetting(). On WhatsApp Web builds where the status module
// stopped wrapping its exports in `default` (>= 2.3000.1048775404), wa-js 4.6.1
// leaves that binding unresolved (fixed by wa-js#3693, in no npm release yet),
// so get() throws "(0 , n.getStatusPrivacySetting) is not a function" although
// the six settings are perfectly readable. So: when get() fails, the six come
// from getUserPrivacySettings() directly, and Status is always read on its own,
// from wherever the status functions really are (WPP.whatsapp.functions when
// wa-js resolved them, otherwise the WhatsApp module that exports them).
export async function readPrivacySettings() {
  const wpp = globalThis['WPP'];
  const fns = wpp?.whatsapp?.functions;
  const STATUS_FNS = ['getStatusPrivacySetting', 'getStatusAllowList',
    'getStatusDenyList', 'setStatusPrivacyConfig'];
  const statusApi = (candidate) => {
    try {
      if (candidate && STATUS_FNS.every((name) => typeof candidate[name] === 'function')) {
        return candidate;
      }
      const inner = candidate?.default;
      if (inner && STATUS_FNS.every((name) => typeof inner[name] === 'function')) {
        return inner;
      }
    } catch {
      // A wa-js binding that cannot resolve may throw on access.
    }
    return null;
  };
  const toIds = (value) => {
    const items = Array.isArray(value)
      ? value
      : typeof value?.toArray === 'function'
        ? value.toArray()
        : typeof value?.getModelsArray === 'function'
          ? value.getModelsArray()
          : null;
    if (!Array.isArray(items)) return null;
    const ids: string[] = [];
    for (const item of items) {
      const wid = item?.id && typeof item.id === 'object' ? item.id : item;
      const id = typeof wid === 'string'
        ? wid
        : wid?._serialized ?? (typeof wid?.toString === 'function' ? wid.toString() : '');
      if (typeof id === 'string' && id.includes('@')) ids.push(id);
    }
    return ids;
  };

  let settings: any = null;
  try {
    settings = await wpp.privacy.get();
  } catch {
    settings = null;
  }
  if (!settings || typeof settings !== 'object') {
    if (typeof fns?.getUserPrivacySettings !== 'function') {
      throw new Error('privacy_unavailable');
    }
    settings = { ...fns.getUserPrivacySettings() };
  }
  settings = { ...settings };
  delete settings.status;

  let api = statusApi(fns);
  if (!api && typeof wpp?.loader?.search === 'function') {
    try {
      api = statusApi(wpp.loader.search((m) => !!statusApi(m)));
    } catch {
      api = null;
    }
  }
  let status: { setting: string; list: string[] | null } | null = null;
  if (api) {
    try {
      const setting = String(await api.getStatusPrivacySetting());
      let list: string[] | null = [];
      if (setting === 'allow-list') list = toIds(await api.getStatusAllowList());
      else if (setting === 'deny-list') list = toIds(await api.getStatusDenyList());
      // list === null: the setting was read but its contact list was not.
      status = { setting, list };
    } catch {
      status = null;
    }
  }
  return { settings, status };
}

// One of the six account settings. Each WPP.privacy setter takes a plain value.
export async function writePrivacySetting({ setting, value }) {
  const SETTERS = {
    lastSeen: 'setLastSeen',
    online: 'setOnline',
    about: 'setAbout',
    profilePicture: 'setProfilePic',
    readReceipts: 'setReadReceipts',
    groupAdd: 'setAddGroup',
  };
  const name = SETTERS[setting];
  const setter = name ? globalThis['WPP']?.privacy?.[name] : undefined;
  if (typeof setter !== 'function') {
    return { ok: false, error: 'privacy_setting_unavailable' };
  }
  try {
    await setter(value);
    return { ok: true };
  } catch (e: any) {
    // The kind of failure helps a bug report ("is not a function" after a
    // WhatsApp Web update); its text could carry an identifier, so only the
    // error's class is kept.
    return { ok: false, error: 'privacy_setting_failed', kind: String(e?.name || 'Error') };
  }
}

// Who can see the account's Status: 'contact' (my contacts), 'deny-list' (my
// contacts except *list*) or 'allow-list' (only *list*). Answers with the
// setting WhatsApp reports afterwards, so the caller can tell a call that
// returned from one that took effect.
export async function writeStatusPrivacy({ setting, list }) {
  const wpp = globalThis['WPP'];
  const fns = wpp?.whatsapp?.functions;
  const STATUS_FNS = ['getStatusPrivacySetting', 'setStatusPrivacyConfig'];
  const statusApi = (candidate) => {
    try {
      if (candidate && STATUS_FNS.every((name) => typeof candidate[name] === 'function')) {
        return candidate;
      }
      const inner = candidate?.default;
      if (inner && STATUS_FNS.every((name) => typeof inner[name] === 'function')) {
        return inner;
      }
    } catch {
      // A wa-js binding that cannot resolve may throw on access.
    }
    return null;
  };
  let api = statusApi(fns);
  // The module scan only runs when wa-js did not bind the status functions.
  if (!api && typeof wpp?.loader?.search === 'function') {
    try {
      api = statusApi(wpp.loader.search((m) => !!statusApi(m)));
    } catch {
      api = null;
    }
  }
  const createWid = wpp?.whatsapp?.WidFactory?.createWid;
  if (!api || typeof createWid !== 'function') {
    return { ok: false, error: 'status_privacy_unavailable' };
  }
  try {
    const wids = (setting === 'contact' ? [] : list).map((id) => createWid(id));
    await api.setStatusPrivacyConfig({ setting, list: wids });
    const now = String(await api.getStatusPrivacySetting());
    return { ok: true, setting: now };
  } catch (e: any) {
    return { ok: false, error: 'status_privacy_failed', kind: String(e?.name || 'Error') };
  }
}
