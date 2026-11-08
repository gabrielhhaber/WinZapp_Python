import {
  channelDirectory, channelInfo, channelKey, followChannel, listChannels, pullChannelHistory,
  readChannels,
} from '../../controller/channelsPanelController';

function response() {
  const res: any = { statusCode: 0, body: null };
  res.status = (code: number) => { res.statusCode = code; return res; };
  res.json = (body: unknown) => { res.body = body; return res; };
  return res;
}

const node = { id: '120363000000000001@newsletter', thread_metadata: { name: { text: 'Notícias' } } };
const calls: any[] = [];
const modules: any = {
  WAWebMexFetchAllNewslettersMetadataJob: {
    mexFetchAllNewsletters: async (o: any) => { calls.push(['list', o]); return { xwa2_newsletter_subscribed: [node] }; },
  },
  WAWebMexFetchNewsletterDirectorySearchResultsJob: {
    mexFetchNewsletterDirectorySearchResults: async (o: any) => {
      calls.push(['search', o]); return { xwa2_newsletters_directory_search: { result: [node] } };
    },
  },
  WAWebMexFetchRecommendedNewslettersJob: {
    mexFetchRecommendedNewsletters: async (limit: number, codes: string[]) => {
      calls.push(['recommended', limit, codes]); return { xwa2_newsletters_recommended: { result: [node] } };
    },
  },
  WAWebNewsletterDirectoryCategoryUtils: { NewsletterDirectoryCategoryType: { Sports: 7, News: 4 } },
  WAWebNewsletterDirectoryFilterUtils: { NewsletterDirectoryListView: { Recommended: 0, Trending: 4 } },
  WAWebMexFetchNewsletterDirectoryListJob: {
    mexFetchNewsletterDirectoryList: async (o: any) => {
      calls.push(['list', o]); return { xwa2_newsletters_directory_list: { result: [node] } };
    },
  },
  WAWebCollections: { WAWebNewsletterCollection: { get: (id: string) => (id === '1@newsletter' ? { id } : undefined) } },
  WAWebNewsletterPinMessageAction: {
    pinNewsletterMessage: async (chat: any, serverId: number) => { calls.push(['pin', chat.id, serverId]); },
    unpinNewsletterMessage: async (chat: any, serverId: number) => { calls.push(['unpin', chat.id, serverId]); },
  },
  WAWebNewsletterPinMessagesJob: {
    pinNewsletterMessages: async (id: string, ids: string[]) => { calls.push(['pin job', id, ids]); },
  },
  WAWebWidFactory: { createWid: (id: string) => ({ wid: id }) },
  WAWebLidMigrationUtils: { toUserLidOrThrow: (wid: any) => ({ toString: () => wid.wid.replace(/@.*/, '@lid') }) },
  WAWebMexClient: {
    fetchQuery: async (req: any, vars: any) => {
      calls.push(['mex', req.params.id, req.params.operationKind, vars]);
      if (req.params.id === '27472091235714801') {
        return { xwa2_newsletter_followers: { followers: { edges: [
          { node: { id: '11111@lid', pn: '5511999998888@c.us', display_name: ' Ana ' }, role: 'SUBSCRIBER' },
          { node: { id: '22222@lid' }, role: 'ADMIN' }, { node: { id: '1203@g.us' }, role: 'ADMIN' }] } } };
      }
      if (req.params.id === '28049623068067370') {
        return { xwa2_newsletter_admin: { pending_admin_invites: [{ user: { id: '55555@lid' } }, { user: null }] } };
      }
      return {};
    },
  },
  WAWebMexFetchNewsletterAdminInfoJob: {
    mexFetchNewsletterAdminInfo: async (id: string) => { calls.push(['admins', id]); return { adminCount: 3 }; },
  },
  WAWebNewsletterSendMsgAction: {
    sendNewsletterAdminInviteMessage: async (chat: any, o: any) => {
      calls.push(['invite', chat, o]); return { messageSendResult: chat.fail ? 'ERROR_UNKNOWN' : 'OK' };
    },
  },
  WAWebMexRevokeNewsletterAdminInviteJob: {
    revokeNewsletterAdminInvite: async (id: string, wid: any) => { calls.push(['revoke', id, wid]); },
  },
  WAWebMexFetchNewsletterJob: {
    mexGetNewsletter: async (key: string, role: string) => { calls.push(['info', key, role]); return { xwa2_newsletter: node }; },
  },
};

function withPage<T>(run: () => Promise<T>): Promise<T> {
  const root = globalThis as any;
  const previous = [root.require, root.WPP];
  root.require = (name: string) => modules[name];
  root.WPP = { chat: { find: async (id: string) => ({ chat: id, fail: id.startsWith('99999'), msgs: { getModelsArray: () => [
    { type: 'newsletter_admin_invite', newsletterAdminInviteInfo: { newsletterId: { toString: () => '2@newsletter' }, inviteExpiration: 5 } },
    { type: 'newsletter_admin_invite', newsletterAdminInviteInfo: { newsletterId: { toString: () => '1@newsletter' }, inviteExpiration: 1790000000 } }] } }) }, newsletter: { follow: async (id: string) => calls.push(['follow', id]),
    mute: async (id: string, value: boolean) => calls.push(['mute', id, value]),
    unfollow: async (id: string) => calls.push(['unfollow', id]) } };
  calls.length = 0;
  return run().finally(() => { [root.require, root.WPP] = previous; });
}

test('each read uses WhatsApp Web\'s own job and returns its nodes', () => withPage(async () => {
  expect(await readChannels({ kind: 'list' })).toEqual([node]);
  expect(await readChannels({ kind: 'directory', query: '  futebol ', limit: 500 })).toEqual([node]);
  expect(await readChannels({ kind: 'directory', query: '' })).toEqual([node]);
  expect(await readChannels({ kind: 'info', key: '0029VaAbCdEfGhIjKlMnOp' })).toEqual(node);
  expect(calls).toEqual([
    ['list', { fetchWamoSub: false, fetchStatusMetadata: false }],
    ['search', { searchText: 'futebol', categories: [], limit: 50, cursorToken: null }],
    ['recommended', 20, []],
    ['info', '0029VaAbCdEfGhIjKlMnOp', 'GUEST'],
  ]);
}));

test('a category and a view use the directory list, with the page\'s own enums', () => withPage(async () => {
  expect(await readChannels({ kind: 'directory', query: 'gol', category: 'sports' })).toEqual([node]);
  expect(await readChannels({ kind: 'directory', category: 'NEWS', view: 'trending' })).toEqual([node]);
  expect(await readChannels({ kind: 'directory', category: 'special_events' })).toEqual([node]);
  expect(calls).toEqual([
    ['search', { searchText: 'gol', categories: [7], limit: 20, cursorToken: null }],
    ['list', { view: 4, categories: [4], countryCodes: [], limit: 20, cursorToken: null }],
    ['recommended', 20, []],
  ]);
}));

test('the directory lists are of the account\'s own country', () => withPage(async () => {
  // A view or a category with no country is refused "Bad Request" by WhatsApp.
  modules.WAWebCountryCodeUtils = { getMyCountryCode: async () => 'BR' };
  try {
    expect(await readChannels({ kind: 'directory', view: 'trending' })).toEqual([node]);
    expect(await readChannels({ kind: 'directory' })).toEqual([node]);
    modules.WAWebCountryCodeUtils = { getMyCountryCode: async () => { throw new Error('offline'); } };
    expect(await readChannels({ kind: 'directory', category: 'NEWS' })).toEqual([node]);
  } finally {
    delete modules.WAWebCountryCodeUtils;
  }
  expect(calls).toEqual([
    ['list', { view: 4, categories: [], countryCodes: ['BR'], limit: 20, cursorToken: null }],
    ['recommended', 20, ['BR']],
    ['list', { view: 0, categories: [4], countryCodes: [], limit: 20, cursorToken: null }],
  ]);
}));

test('an update is pinned by its server id, through the page\'s action', () => withPage(async () => {
  const done = { id: '1@newsletter', serverId: 4977, pinned: true };
  expect(await readChannels({ kind: 'pin', id: '1@newsletter', serverId: 4977, pin: true })).toEqual(done);
  expect(await readChannels({ kind: 'pin', id: '1@newsletter', serverId: 4977, pin: false }))
    .toEqual({ ...done, pinned: false });
  expect(await readChannels({ kind: 'pin', id: '2@newsletter', serverId: 5, pin: true }))
    .toEqual({ id: '2@newsletter', serverId: 5, pinned: true });
  expect(await readChannels({ kind: 'pin', id: '1@newsletter', serverId: '4977', pin: true })).toBeNull();
  expect(calls).toEqual([['pin', '1@newsletter', 4977], ['unpin', '1@newsletter', 4977],
    ['pin job', '2@newsletter', ['5']]]);
}));

test('admins: the count, an invite to the person\'s chat, and taking it back', () => withPage(async () => {
  expect(await readChannels({ kind: 'admins', id: '1@newsletter' })).toEqual({ id: '1@newsletter', adminCount: 3 });
  expect(await readChannels({ kind: 'admin-invite', id: '1@newsletter', user: '5511999998888@s.whatsapp.net',
    message: ' oi ' })).toEqual({ id: '1@newsletter', user: '5511999998888@s.whatsapp.net', expiration: 1790000000 });
  expect(await readChannels({ kind: 'admin-invite', id: '1@newsletter', user: '99999123@lid' })).toBeNull();
  expect(await readChannels({ kind: 'admin-invite', id: '1@newsletter', user: '1203@g.us' })).toBeNull();
  expect(await readChannels({ kind: 'admin-revoke', id: '1@newsletter', user: '77712345@lid' }))
    .toEqual({ id: '1@newsletter', user: '77712345@lid' });
  expect(calls).toEqual([
    ['admins', '1@newsletter'],
    ['invite', expect.objectContaining({ chat: '5511999998888@c.us' }), { base64Thumb: null,
      invitee: { wid: '5511999998888@c.us' }, inviteMessage: 'oi', newsletterWid: { wid: '1@newsletter' } }],
    ['invite', expect.objectContaining({ chat: '99999123@lid' }), { base64Thumb: null, invitee: { wid: '99999123@lid' },
      inviteMessage: '', newsletterWid: { wid: '1@newsletter' } }],
    ['revoke', '1@newsletter', { wid: '77712345@lid' }],
  ]);
}));

test('the owner\'s people: followers, pending invites, demote and owner change', () => withPage(async () => {
  expect(await readChannels({ kind: 'followers', id: '1@newsletter' })).toEqual({ id: '1@newsletter', followers: [
    { id: '22222@lid', phone: '', name: '', role: 'admin' },
    { id: '11111@lid', phone: '5511999998888@s.whatsapp.net', name: 'Ana', role: 'subscriber' }] });
  expect(await readChannels({ kind: 'admin-pending', id: '1@newsletter' })).toEqual({ id: '1@newsletter', pending: ['55555@lid'] });
  expect(await readChannels({ kind: 'admin-demote', id: '1@newsletter', user: '22222@lid' }))
    .toEqual({ id: '1@newsletter', user: '22222@lid' });
  expect(await readChannels({ kind: 'owner-change', id: '1@newsletter', user: '5511999998888@s.whatsapp.net' }))
    .toEqual({ id: '1@newsletter', user: '5511999998888@s.whatsapp.net' });
  expect(await readChannels({ kind: 'owner-change', id: '1@newsletter', user: '1203@g.us' })).toBeNull();
  expect(calls.slice(2)).toEqual([
    ['mex', '9880997548630971', 'mutation', { newsletter_id: '1@newsletter', user_id: '22222@lid' }],
    ['mex', '9546742745432473', 'mutation', { newsletter_id: '1@newsletter', user_id: '5511999998888@lid' }],
  ]);
}));

test('follow and unfollow go through wa-js, only for a channel id', () => withPage(async () => {
  expect(await readChannels({ kind: 'follow', id: '1@newsletter' })).toEqual({ id: '1@newsletter' });
  expect(await readChannels({ kind: 'unfollow', id: '5511@c.us' })).toBeNull();
  expect(await readChannels({ kind: 'info', key: 'x y' })).toBeNull();
  expect(await readChannels({ kind: 'mute', id: '1@newsletter', mute: false })).toEqual(
    { id: '1@newsletter', muted: false });
  expect(await readChannels({ kind: 'mute', id: '1@newsletter', mute: 'yes' })).toBeNull();
  expect(calls).toEqual([['follow', '1@newsletter'], ['mute', '1@newsletter', false]]);
}));

test('a channel key is an id or the code of an invite link', () => {
  expect(channelKey({ id: '123@newsletter' })).toBe('123@newsletter');
  expect(channelKey({ invite: 'https://whatsapp.com/channel/0029VaAbCdEfGhIjKlMnOp' })).toBe('0029VaAbCdEfGhIjKlMnOp');
  expect(channelKey({ invite: '0029VaAbCdEfGhIjKlMnOp/' })).toBe('0029VaAbCdEfGhIjKlMnOp');
  expect(channelKey({ id: '5511@c.us' })).toBe('');
  expect(channelKey({ invite: 'short' })).toBe('');
});

test('a route never answers the native error, and refuses a bad key', async () => {
  const failing = { client: { page: { evaluate: async () => { throw new Error('secret 5511@c.us'); } } } } as any;
  const res = response();
  await listChannels(failing, res);
  expect(res.statusCode).toBe(503);
  expect(JSON.stringify(res.body)).not.toContain('5511');
  const empty = { client: { page: { evaluate: async () => null } }, body: {} } as any;
  const second = response();
  await channelDirectory(empty, second);
  expect(second.statusCode).toBe(503);
  const bad = response();
  await channelInfo({ body: { invite: '???' } } as any, bad);
  expect(bad.statusCode).toBe(400);
  const notChannel = response();
  await followChannel({ body: { id: '1@g.us' } } as any, notChannel);
  expect(notChannel.statusCode).toBe(400);
});

function channelPage(total: number, held: number, perRound = 50) {
  // The server holds updates 1..total; the page starts with the newest `held`.
  const ids = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, i) => from + i);
  const state = { rounds: 0, list: ids(total - held + 1, total) };
  const msgs: any = {
    msgLoadState: { noEarlierMsgs: false },
    get length() { return state.list.length; },
    getModelsArray: () => state.list.map((n) => ({ id: { _serialized: `false_1@newsletter_${n}` } })),
  };
  const channel = { msgs };
  const root = globalThis as any;
  const previous = root.require;
  root.require = (name: string) => ({
    WAWebCollections: { WAWebNewsletterCollection: { get: (id: string) => (id === '1@newsletter' ? channel : undefined) } },
    WAWebChatLoadMessages: { loadEarlierMsgs: async (o: any) => {
      expect(o.chat).toBe(channel);
      state.rounds++;
      const first = state.list[0];
      const from = Math.max(1, first - perRound);
      state.list = ids(from, first - 1).concat(state.list);
      if (from === 1) msgs.msgLoadState.noEarlierMsgs = true;
    } },
  } as any)[name];
  return { state, restore: () => { root.require = previous; } };
}

test('a channel\'s older updates are pulled from the server before a page', async () => {
  const page = channelPage(120, 16);
  try {
    // The anchor is the oldest held update (105): 30 before it take one round.
    expect(await pullChannelHistory({ chatId: '1@newsletter', anchorId: 'false_1@newsletter_105', targetCount: 30 })).toBe(50);
    expect(page.state.rounds).toBe(1);
    // Asking for more than the channel has stops at its start.
    expect(await pullChannelHistory({ chatId: '1@newsletter', anchorId: 'false_1@newsletter_105', targetCount: 500 })).toBe(54);
    expect(page.state.list[0]).toBe(1);
    expect(await pullChannelHistory({ chatId: '1@newsletter', anchorId: 'false_1@newsletter_105', targetCount: 500 })).toBe(0);
  } finally {
    page.restore();
  }
});

test('an anchor the page does not hold yet keeps loading until it appears', async () => {
  const page = channelPage(400, 10);
  try {
    expect(await pullChannelHistory({ chatId: '1@newsletter', anchorId: 'false_1@newsletter_300', targetCount: 20 })).toBe(150);
    expect(page.state.list.indexOf(300)).toBeGreaterThanOrEqual(20);
  } finally {
    page.restore();
  }
});

test('anything but a channel, or a page without the loader, loads nothing', async () => {
  const page = channelPage(50, 5);
  try {
    expect(await pullChannelHistory({ chatId: '5511@c.us', anchorId: 'x', targetCount: 10 })).toBe(0);
    expect(await pullChannelHistory({ chatId: '2@newsletter', anchorId: 'x', targetCount: 10 })).toBe(0);
    expect(await pullChannelHistory({ chatId: '1@newsletter', anchorId: '', targetCount: 10 })).toBe(0);
    expect(page.state.rounds).toBe(0);
  } finally {
    page.restore();
  }
});
