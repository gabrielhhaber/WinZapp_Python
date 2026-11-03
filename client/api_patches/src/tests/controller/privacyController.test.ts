import {
  getKeepChatsArchived,
  getPrivacySettings,
  setKeepChatsArchived,
  setPrivacySetting,
  setStatusPrivacy,
} from '../../controller/privacyController';
import {
  readKeepChatsArchived,
  readPrivacySettings,
  writeKeepChatsArchived,
  writePrivacySetting,
  writeStatusPrivacy,
} from '../../util/privacyRuntime';

function response() {
  const res: any = { statusCode: 0, body: null };
  res.status = (code: number) => { res.statusCode = code; return res; };
  res.json = (body: unknown) => { res.body = body; return res; };
  return res;
}

function request(body: unknown = {}, answer: unknown = { ok: true }) {
  return {
    body,
    client: { page: { isClosed: () => false, evaluate: jest.fn().mockResolvedValue(answer) } },
  } as any;
}

const SIX = {
  lastSeen: 'contacts', online: 'all', about: 'all',
  profilePicture: 'contacts', readReceipts: 'all', groupAdd: 'contacts',
};

function wid(id: string) {
  return { _serialized: id, toString: () => id };
}

describe('privacy routes', () => {
  it('reads the settings from the page', async () => {
    const req = request({}, { settings: SIX, status: null });
    const res = response();
    await getPrivacySettings(req, res);
    expect(req.client.page.evaluate).toHaveBeenCalledWith(readPrivacySettings);
    expect(res.statusCode).toBe(200);
    expect(res.body.response.settings).toEqual(SIX);
  });

  it('never exposes the text of a native failure', async () => {
    const req = request();
    req.client.page.evaluate.mockRejectedValue(new Error('private contact 5551999@c.us'));
    const res = response();
    await getPrivacySettings(req, res);
    expect(res.statusCode).toBe(503);
    expect(JSON.stringify(res.body)).not.toContain('5551999');
  });

  it('a dead page is reported without touching WhatsApp', async () => {
    const req = request();
    req.client.page.isClosed = () => true;
    const res = response();
    await setPrivacySetting({ ...req, body: { setting: 'about', value: 'all' } } as any, res);
    expect(res.statusCode).toBe(503);
    expect(req.client.page.evaluate).not.toHaveBeenCalled();
  });

  it('sends one account setting', async () => {
    const req = request({ setting: 'readReceipts', value: 'none' });
    const res = response();
    await setPrivacySetting(req, res);
    expect(req.client.page.evaluate).toHaveBeenCalledWith(
      writePrivacySetting, { setting: 'readReceipts', value: 'none' });
    expect(res.statusCode).toBe(200);
  });

  it.each([
    { setting: 'status', value: 'contacts' },
    { setting: 'online', value: 'contacts' },
    { setting: 'about', value: '' },
    { setting: 'about' },
    {},
  ])('rejects %p before reaching the page', async (body) => {
    const req = request(body);
    const res = response();
    await setPrivacySetting(req, res);
    expect(res.statusCode).toBe(400);
    expect(req.client.page.evaluate).not.toHaveBeenCalled();
  });

  it('a setter that failed is a failure, with its code', async () => {
    const req = request({ setting: 'about', value: 'none' },
      { ok: false, error: 'privacy_setting_failed', kind: 'TypeError' });
    const res = response();
    await setPrivacySetting(req, res);
    expect(res.statusCode).toBe(502);
    expect(res.body).toEqual({ status: 'error', code: 'privacy_setting_failed', kind: 'TypeError' });
  });

  it('sends the status audience and answers with what WhatsApp now reports', async () => {
    const req = request({ setting: 'deny-list', list: ['5551999998888@c.us', '123456789012345@lid'] },
      { ok: true, setting: 'deny-list' });
    const res = response();
    await setStatusPrivacy(req, res);
    expect(req.client.page.evaluate).toHaveBeenCalledWith(writeStatusPrivacy, {
      setting: 'deny-list', list: ['5551999998888@c.us', '123456789012345@lid'],
    });
    expect(res.body).toEqual({ status: 'success', response: { setting: 'deny-list' } });
  });

  it.each([
    { setting: 'everyone', list: [] },
    { setting: 'allow-list', list: ['123@g.us'] },
    { setting: 'allow-list', list: ['status@broadcast'] },
    { setting: 'allow-list', list: ['5551999998888@c.us;x'] },
    { setting: 'allow-list', list: 'x@c.us' },
  ])('rejects the status audience %p before reaching the page', async (body) => {
    const req = request(body);
    const res = response();
    await setStatusPrivacy(req, res);
    expect(res.statusCode).toBe(400);
    expect(req.client.page.evaluate).not.toHaveBeenCalled();
  });
});

describe('privacy runtime (the code that runs in WhatsApp Web)', () => {
  afterEach(() => { delete (globalThis as any).WPP; });

  function statusModule(setting = 'deny-list') {
    return {
      getStatusPrivacySetting: jest.fn().mockResolvedValue(setting),
      getStatusAllowList: jest.fn().mockResolvedValue([wid('1111111111@c.us')]),
      getStatusDenyList: jest.fn().mockResolvedValue([wid('2222222222@c.us'), { id: wid('3333333333@lid') }]),
      setStatusPrivacyConfig: jest.fn().mockResolvedValue(undefined),
    };
  }

  it('falls back to the six account settings when WPP.privacy.get() throws', async () => {
    (globalThis as any).WPP = {
      privacy: { get: jest.fn().mockRejectedValue(new TypeError('getStatusPrivacySetting is not a function')) },
      whatsapp: { functions: { getUserPrivacySettings: () => ({ ...SIX }) } },
      loader: { search: () => null },
    };
    const answer = await readPrivacySettings();
    expect(answer).toEqual({ settings: SIX, status: null });
  });

  it('reads Status from the WhatsApp module when wa-js could not bind it', async () => {
    const module = { default: statusModule('deny-list') };
    (globalThis as any).WPP = {
      privacy: { get: jest.fn().mockResolvedValue({ ...SIX, status: 'ignored' }) },
      whatsapp: { functions: {} },
      loader: { search: (condition: (m: unknown) => boolean) => (condition(module) ? module : null) },
    };
    const answer = await readPrivacySettings();
    expect(answer.settings).toEqual(SIX);
    expect(answer.status).toEqual({ setting: 'deny-list', list: ['2222222222@c.us', '3333333333@lid'] });
  });

  it('a list it cannot read is null, never an empty list', async () => {
    const module = statusModule('allow-list');
    module.getStatusAllowList.mockResolvedValue({ weird: true } as any);
    (globalThis as any).WPP = {
      privacy: { get: jest.fn().mockResolvedValue({ ...SIX }) },
      whatsapp: { functions: module },
    };
    const answer = await readPrivacySettings();
    expect(answer.status).toEqual({ setting: 'allow-list', list: null });
  });

  it('writes a setter by its WPP.privacy name and hides the error text', async () => {
    const setAbout = jest.fn().mockRejectedValue(new TypeError('5551999 private'));
    (globalThis as any).WPP = { privacy: { setAbout } };
    const answer = await writePrivacySetting({ setting: 'about', value: 'none' });
    expect(setAbout).toHaveBeenCalledWith('none');
    expect(answer).toEqual({ ok: false, error: 'privacy_setting_failed', kind: 'TypeError' });
  });

  it('builds WhatsApp ids for the status audience and reads the setting back', async () => {
    const module = statusModule('allow-list');
    const createWid = jest.fn((id: string) => wid(id));
    (globalThis as any).WPP = { whatsapp: { functions: module, WidFactory: { createWid } } };
    const answer = await writeStatusPrivacy({ setting: 'allow-list', list: ['1111111111@c.us'] });
    expect(module.setStatusPrivacyConfig).toHaveBeenCalledWith({
      setting: 'allow-list', list: [expect.objectContaining({ _serialized: '1111111111@c.us' })],
    });
    expect(answer).toEqual({ ok: true, setting: 'allow-list' });
  });

  it('"my contacts" never sends a list', async () => {
    const module = statusModule('contact');
    (globalThis as any).WPP = { whatsapp: { functions: module, WidFactory: { createWid: wid } } };
    await writeStatusPrivacy({ setting: 'contact', list: ['1111111111@c.us'] });
    expect(module.setStatusPrivacyConfig).toHaveBeenCalledWith({ setting: 'contact', list: [] });
  });
});

describe('keep chats archived', () => {
  afterEach(() => { delete (globalThis as any).WPP; });

  function whatsappWeb(enabled: boolean | null, unarchive: boolean | null) {
    const state = { enabled, unarchive };
    const prefs = {
      getArchiveV2EnabledSetting: jest.fn(async () => state.enabled),
      getUnarchiveChatsSetting: jest.fn(async () => state.unarchive),
      setArchiveV2EnabledSetting: jest.fn(async (v: boolean) => { state.enabled = v; }),
      setUnarchiveChatsSetting: jest.fn(async (v: boolean) => { state.unarchive = v; }),
    };
    const sync = {
      collectionName: 'regular_low', getVersion: () => 4, getAction: () => 'setting_unarchiveChats',
    };
    const modules: Record<string, any> = {
      WAWebUserPrefsMultiDevice: prefs,
      WAWebArchiveSettingSync: { default: sync },
      WAWebSyncdActionUtils: { buildPendingMutation: jest.fn((m: any) => m) },
      WAWebSyncdCoreApi: { lockForSync: jest.fn(async () => undefined) },
      'WAWebProtobufsServerSync.pb': { SyncdMutation$SyncdOperation: { SET: 0 } },
      WATimeUtils: { unixTimeMs: () => 1700000000000 },
    };
    (globalThis as any).WPP = { loader: { moduleRequire: (name: string) => modules[name] } };
    return { prefs, modules };
  }

  it('reads kept only with archive v2 on and unarchive off', async () => {
    whatsappWeb(true, false);
    expect(await readKeepChatsArchived()).toEqual({ ok: true, keepArchived: true });
    whatsappWeb(true, true);
    expect(await readKeepChatsArchived()).toEqual({ ok: true, keepArchived: false });
    whatsappWeb(null, null);
    expect(await readKeepChatsArchived()).toEqual({ ok: true, keepArchived: false });
  });

  it('sends the mutation WhatsApp Web sends and keeps the preferences', async () => {
    const { prefs, modules } = whatsappWeb(true, true);
    const answer = await writeKeepChatsArchived({ keepArchived: true });
    expect(modules.WAWebSyncdActionUtils.buildPendingMutation).toHaveBeenCalledWith(
      expect.objectContaining({
        collection: 'regular_low', indexArgs: [], version: 4, action: 'setting_unarchiveChats',
        value: { unarchiveChatsSetting: { unarchiveChats: false } },
      }));
    expect(modules.WAWebSyncdCoreApi.lockForSync).toHaveBeenCalled();
    expect(prefs.setUnarchiveChatsSetting).toHaveBeenCalledWith(false);
    expect(answer).toEqual({ ok: true, keepArchived: true });
  });

  it('an older WhatsApp Web says unsupported, a failure keeps its text out', async () => {
    (globalThis as any).WPP = { loader: { moduleRequire: () => undefined } };
    expect(await writeKeepChatsArchived({ keepArchived: true }))
      .toEqual({ ok: false, error: 'keep_archived_unsupported' });
    const { modules } = whatsappWeb(true, false);
    modules.WAWebSyncdCoreApi.lockForSync.mockRejectedValue(new TypeError('5551999 private'));
    expect(await writeKeepChatsArchived({ keepArchived: false }))
      .toEqual({ ok: false, error: 'keep_archived_failed', kind: 'TypeError' });
  });

  it('the routes validate and answer what the page holds', async () => {
    const bad = response();
    await setKeepChatsArchived(request({ keepArchived: 'yes' }), bad);
    expect(bad.statusCode).toBe(400);

    const req = request({ keepArchived: false }, { ok: true, keepArchived: false });
    const res = response();
    await setKeepChatsArchived(req, res);
    expect(req.client.page.evaluate).toHaveBeenCalledWith(
      writeKeepChatsArchived, { keepArchived: false });
    expect(res.body).toEqual({ status: 'success', response: { keepArchived: false } });

    const read = request({}, { ok: false, error: 'keep_archived_unsupported' });
    const readRes = response();
    await getKeepChatsArchived(read, readRes);
    expect(read.client.page.evaluate).toHaveBeenCalledWith(readKeepChatsArchived);
    expect(readRes.statusCode).toBe(501);
  });
});
