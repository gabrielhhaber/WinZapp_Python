import {
  channelDirectory, channelInfo, channelInsights, channelKey, channelOlderPageIncomplete,
  editChannelUpdate, followChannel,
  listChannels, pullChannelHistory, readNewestChannelUpdates,
  readChannels, setChannelAdminProfiles, setChannelReactions,
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

test('an own channel update is edited through WhatsApp Web\'s own edit', async () => {
  const root = globalThis as any;
  const previous = [root.require, root.WPP];
  const edits: any[] = [];
  const msg = { id: 'true_1@newsletter_3EB0AB' };
  root.require = (name: string) => (name === 'WAWebSendMessageEditAction'
    ? { sendMessageEdit: async (m: any, text: string, options: any) => { edits.push([m, text, options]); } }
    : undefined);
  root.WPP = { chat: { getMessageById: async (id: string) => (id === msg.id ? msg : undefined) } };
  try {
    expect(await editChannelUpdate({ id: msg.id, text: '  novo texto ' })).toEqual({ id: msg.id });
    expect(edits).toEqual([[msg, 'novo texto', {}]]);
    // not a channel's id, an empty text, an update the page does not hold
    expect(await editChannelUpdate({ id: 'true_5511@c.us_3EB0AB', text: 'x' })).toBeNull();
    expect(await editChannelUpdate({ id: msg.id, text: '   ' })).toBeNull();
    expect(await editChannelUpdate({ id: 'true_1@newsletter_FFFF', text: 'x' })).toBeNull();
    root.require = () => undefined;
    expect(await editChannelUpdate({ id: msg.id, text: 'x' })).toBeNull();
    expect(edits.length).toBe(1);
  } finally {
    [root.require, root.WPP] = previous;
  }
});

test('an edit WhatsApp Web would refuse is refused before anything is sent', async () => {
  // past the edit window, or not this account's: canEditMsg says no, so the
  // route can say "Cannot edit this message" and the client rolls back
  const root = globalThis as any;
  const previous = [root.require, root.WPP];
  const edits: any[] = [];
  const msg = { id: 'true_1@newsletter_3EB0AB' };
  let allowed = false;
  root.require = (name: string) => (name === 'WAWebSendMessageEditAction'
    ? { sendMessageEdit: async (m: any) => { edits.push(m); } } : undefined);
  root.WPP = { chat: { getMessageById: async () => msg },
    whatsapp: { functions: { canEditMsg: (m: any) => { expect(m).toBe(msg); return allowed; } } } };
  try {
    expect(await editChannelUpdate({ id: msg.id, text: 'x' })).toEqual({ refused: true });
    expect(edits).toEqual([]);
    allowed = true;
    expect(await editChannelUpdate({ id: msg.id, text: 'x' })).toEqual({ id: msg.id });
    expect(edits).toEqual([msg]);
  } finally {
    [root.require, root.WPP] = previous;
  }
});


function newestPage(held: number[], server: number[], options: any = {}) {
  const root = globalThis as any;
  const models: any[] = held.map((t) => ({ id: { _serialized: `false_1@newsletter_${t}` }, t }));
  const msgLoadState: any = { noEarlierMsgs: false };
  const channel: any = { msgs: { getModelsArray: () => models, get length() { return models.length; }, msgLoadState } };
  const older: number[] = options.older ? [...options.older] : [];
  let earlierRounds = 0;
  const pulls: any[] = [];
  const created: any[] = [];
  const previous = { require: root.require, WAPI: root.WAPI };
  let inCollection = !options.notHeld;
  root.WAPI = { _serializeMessageObj: (m: any) => ({ id: m.id._serialized, t: m.t }) };
  root.require = (name: string) => ({
    WAWebCollections: { WAWebNewsletterCollection: { get: (id: string) => (id === '1@newsletter' && inCollection ? channel : undefined) } },
    WAWebWidFactory: { createWid: (id: string) => ({ id }) },
    WAWebFindChatAction: { findOrCreateLatestChat: async (wid: any, origin: string) => {
      created.push([wid.id, origin]); inCollection = true; return { chat: channel, created: true };
    } },
    WAWebChatLoadMessages: { loadEarlierMsgs: async (o: any) => {
      expect(o.chat).toBe(channel);
      earlierRounds++;
      const batch = older.splice(Math.max(0, older.length - 50));
      for (const t of batch) models.unshift({ id: { _serialized: `false_1@newsletter_${t}` }, t });
      if (!older.length) msgLoadState.noEarlierMsgs = true;
    } },
    WAWebNewsletterPullMessagesFromServerAction: options.noPull ? {} : {
      pullNewsletterMessagesFromServer: async (chat: any, o: any) => {
        expect(chat).toBe(channel);
        pulls.push(o);
        if (options.offline) throw new Error('offline');
        for (const t of server) models.push({ id: { _serialized: `false_1@newsletter_${t}` }, t });
        return [];
      },
    },
  } as any)[name];
  return { pulls, created, rounds: () => earlierRounds, restore: () => { root.require = previous.require; root.WAPI = previous.WAPI; } };
}

test("a channel's first page reads its newest updates from the server", async () => {
  // the page held 2 old ones; the server has the 20 newest
  const page = newestPage([1, 2], Array.from({ length: 20 }, (_, i) => 100 + i));
  try {
    const got: any = await readNewestChannelUpdates({ chatId: '1@newsletter', targetCount: 5 });
    expect(got.map((m: any) => m.t)).toEqual([115, 116, 117, 118, 119]);
    expect(page.pulls).toEqual([{ messageCount: 5, cursor: null, resetUnreadCount: false }]);
  } finally {
    page.restore();
  }
});

test('a whole page of 200 comes in one newest read and older pages after it', async () => {
  // the server's newest 100, then 150 older ones 50 at a time
  const page = newestPage([], Array.from({ length: 100 }, (_, i) => 1000 + i),
    { older: Array.from({ length: 150 }, (_, i) => 1 + i) });
  try {
    const got: any = await readNewestChannelUpdates({ chatId: '1@newsletter', targetCount: 200 });
    expect(page.pulls).toEqual([{ messageCount: 100, cursor: null, resetUnreadCount: false }]);
    expect(page.rounds()).toBe(2);
    expect(got.length).toBe(200);
    expect(got[got.length - 1].t).toBe(1099);
  } finally {
    page.restore();
  }
});

test('a channel shorter than the page stops at its start', async () => {
  const page = newestPage([], [500, 501], { older: [1, 2, 3] });
  try {
    const got: any = await readNewestChannelUpdates({ chatId: '1@newsletter', targetCount: 200 });
    expect(got.map((m: any) => m.t)).toEqual([1, 2, 3, 500, 501]);
    expect(page.rounds()).toBe(1);
  } finally {
    page.restore();
  }
});

test("an admin's own new posts are on the first page", async () => {
  // posted from here, never "received": they are in the collection, newest
  const page = newestPage([10, 30, 20], []);
  try {
    const got: any = await readNewestChannelUpdates({ chatId: '1@newsletter', targetCount: 20 });
    expect(got.map((m: any) => m.t)).toEqual([10, 20, 30]);
  } finally {
    page.restore();
  }
});

test('a channel not followed is found the way WhatsApp Web previews it', async () => {
  const page = newestPage([], [7], { notHeld: true });
  try {
    const got: any = await readNewestChannelUpdates({ chatId: '1@newsletter', targetCount: 20 });
    expect(page.created).toEqual([['1@newsletter', 'createChat']]);
    expect(got.map((m: any) => m.t)).toEqual([7]);
  } finally {
    page.restore();
  }
});

test('offline, the first page is what the page holds', async () => {
  const page = newestPage([3, 4], [9], { offline: true });
  try {
    const got: any = await readNewestChannelUpdates({ chatId: '1@newsletter', targetCount: 20 });
    expect(got.map((m: any) => m.t)).toEqual([3, 4]);
  } finally {
    page.restore();
  }
});

test('anything but a channel, or a page without WAPI, answers null', async () => {
  const page = newestPage([1], []);
  try {
    expect(await readNewestChannelUpdates({ chatId: '5511@c.us', targetCount: 20 })).toBeNull();
    (globalThis as any).WAPI = undefined;
    expect(await readNewestChannelUpdates({ chatId: '1@newsletter', targetCount: 20 })).toBeNull();
  } finally {
    page.restore();
  }
});


test('who may react goes through the update job of the page, with its enum', async () => {
  const root = globalThis as any;
  const previous = root.require;
  const asked: any[] = [];
  root.require = (name: string) => ({
    WAWebCommonNewsletterEnums: { NewsletterReactionCodesSetting: { All: 0, Basic: 1, Blocklist: 2, None: 3 } },
    WAWebMexUpdateNewsletterJob: { mexUpdateNewsletter: async (o: any) => { asked.push(o); return {}; } },
  } as any)[name];
  try {
    expect(await readChannels({ kind: 'reactions', id: '1@newsletter', value: 'none' }))
      .toEqual({ id: '1@newsletter', value: 'NONE' });
    expect(asked).toEqual([{ newsletterID: '1@newsletter', reactionCodesSetting: 3 }]);
    expect(await readChannels({ kind: 'reactions', id: '1@newsletter', value: 'BLOCKLIST' })).toBeNull();
    expect(await readChannels({ kind: 'reactions', id: '5511@c.us', value: 'ALL' })).toBeNull();
    expect(asked.length).toBe(1);
  } finally {
    root.require = previous;
  }
});


test('show admin profile goes through the page job, and the list carries what the page knows', async () => {
  const root = globalThis as any;
  const previous = root.require;
  const asked: any[] = [];
  const meta: any = { adminProfilesSettingEnabled: false, set(o: any) { Object.assign(meta, o); } };
  root.require = (name: string) => ({
    WAWebCollections: { WAWebNewsletterCollection: { get: (id: string) => (id === '1@newsletter' ? { newsletterMetadata: meta } : undefined) } },
    WAWebNewsletterExtendedGatingUtils: { isNewsletterAdminProfilesSettingEnabled: () => true },
    WAWebMexUpdateNewsletterAdminProfileSettingJob: {
      mexUpdateNewsletterAdminProfileSetting: async (o: any) => { asked.push(o); return {}; },
    },
    WAWebMexFetchAllNewslettersMetadataJob: {
      mexFetchAllNewsletters: async () => ({ xwa2_newsletter_subscribed: [{ id: '1@newsletter', thread_metadata: {} }] }),
    },
  } as any)[name];
  try {
    expect(await readChannels({ kind: 'admin-profiles', id: '1@newsletter', enabled: true }))
      .toEqual({ id: '1@newsletter', enabled: true });
    expect(asked).toEqual([{ newsletterID: '1@newsletter', adminProfilesEnabled: true }]);
    const list: any = await readChannels({ kind: 'list' });
    expect(list[0].thread_metadata.settings).toEqual({ admin_profiles_enabled: true });
    expect(await readChannels({ kind: 'admin-profiles', id: '1@newsletter', enabled: 'yes' })).toBeNull();
  } finally {
    root.require = previous;
  }
});


test('show admin profile is not reported where WhatsApp does not offer it', async () => {
  const root = globalThis as any;
  const previous = root.require;
  root.require = (name: string) => ({
    WAWebCollections: { WAWebNewsletterCollection: { get: () => ({ newsletterMetadata: { adminProfilesSettingEnabled: false } }) } },
    WAWebNewsletterExtendedGatingUtils: { isNewsletterAdminProfilesSettingEnabled: () => false },
    WAWebMexFetchAllNewslettersMetadataJob: {
      mexFetchAllNewsletters: async () => ({ xwa2_newsletter_subscribed: [{ id: '1@newsletter', thread_metadata: {} }] }),
    },
  } as any)[name];
  try {
    const list: any = await readChannels({ kind: 'list' });
    expect(list[0].thread_metadata.settings).toBeUndefined();
  } finally {
    root.require = previous;
  }
});

// A channel page whose models carry server ids, as WhatsApp Web's do.
function serverPage(held: number[], server: number[], options: any = {}) {
  const root = globalThis as any;
  const make = (n: number) => ({ id: { _serialized: `false_1@newsletter_${n}` }, t: n, serverId: n });
  const models: any[] = held.map(make);
  const msgLoadState: any = { noEarlierMsgs: !options.older };
  const older: number[] = options.older ? [...options.older] : [];
  const fills: any[] = [];
  const msgs: any = {
    getModelsArray: () => models, get length() { return models.length; }, msgLoadState,
    toArray: () => models.slice(),
    add: (list: any[]) => {
      for (const m of list) if (!models.some((x) => x.serverId === m.serverId)) models.push(m);
    },
    sort: () => models.sort((a, b) => a.serverId - b.serverId),
    remove: (list: any[]) => {
      for (const m of list) models.splice(models.indexOf(m), 1);
    },
  };
  const channel: any = { id: { toJid: () => '1@newsletter', _serialized: '1@newsletter' }, msgs };
  const previous = { require: root.require, WAPI: root.WAPI };
  root.WAPI = { _serializeMessageObj: (m: any) => ({ id: m.id._serialized, t: m.t, serverId: m.serverId }) };
  root.require = (name: string) => ({
    WAWebCollections: { WAWebNewsletterCollection: { get: () => channel } },
    WAWebChatLoadMessages: { loadEarlierMsgs: async () => {
      const batch = older.splice(Math.max(0, older.length - 50));
      for (const n of batch) models.unshift(make(n));
      if (!older.length) msgLoadState.noEarlierMsgs = true;
    } },
    WAWebNewsletterPullMessagesFromServerAction: { pullNewsletterMessagesFromServer: async () => {
      for (const n of server) if (!models.some((m) => m.serverId === n)) models.push(make(n));
      return [];
    } },
    ...(options.fill ? {
      WAWebNewsletterMsgHistoryUtils: { fillMsgHistoryGaps: async (o: any) => {
        fills.push(o);
        if (options.fill === 'offline') throw new Error('offline');
        const ids = o.msgs.map((d: any) => d.serverId).sort((a: number, b: number) => a - b);
        const whole: any[] = [];
        for (let n = ids[0]; n <= ids[ids.length - 1]; n++) whole.push({ serverId: n });
        return whole;
      } },
      WAWebMsgDataFromModel: { msgDataFromMsgModel: (m: any) => ({ serverId: m.serverId }) },
      WAWebMsgModelFromData: { msgModelFromMsgData: (d: any) => make(d.serverId) },
      WAWebNewsletterValidationUtils: { toNewsletterJidOrThrow: (jid: string) => jid },
      WAWebNewsletterViewModeUIUtils: {
        getHiddenMessageServerIdsForChat: () => new Set(options.hidden || []),
      },
    } : {}),
  } as any)[name];
  return { fills, models, restore: () => { root.require = previous.require; root.WAPI = previous.WAPI; } };
}

const range = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, i) => from + i);

test('updates held from an earlier visit never make a page with a hole', async () => {
  // held 281..300 from yesterday; today the newest 100 are 701..800 and the
  // hole can not be filled (offline): only the run ending at the newest
  const page = serverPage(range(281, 300), range(701, 800), { fill: 'offline' });
  try {
    const got: any = await readNewestChannelUpdates({ chatId: '1@newsletter', targetCount: 200 });
    expect(got.map((m: any) => m.serverId)).toEqual(range(701, 800));
    expect(page.fills.length).toBe(1);
  } finally {
    page.restore();
  }
});

test('the hole is filled the way WhatsApp Web repairs a channel', async () => {
  const page = serverPage(range(281, 300), range(401, 500), { fill: true, hidden: [] });
  try {
    const got: any = await readNewestChannelUpdates({ chatId: '1@newsletter', targetCount: 200 });
    expect(got.map((m: any) => m.serverId)).toEqual(range(301, 500));
    expect(page.fills[0].jid).toBe('1@newsletter');
  } finally {
    page.restore();
  }
});

test('hidden updates and a post still sending do not break the run', async () => {
  const page = serverPage([95, 97, 98], [99, 100], { fill: 'offline', hidden: [96] });
  page.models.push({ id: { _serialized: 'true_1@newsletter_X' }, t: 101, serverId: 9007199254730993 });
  try {
    const got: any = await readNewestChannelUpdates({ chatId: '1@newsletter', targetCount: 20 });
    expect(got.map((m: any) => m.serverId)).toEqual([95, 97, 98, 99, 100, 9007199254730993]);
  } finally {
    page.restore();
  }
});

test('an older page fills the holes before it is read', async () => {
  const page = serverPage([10, 11, 50, 51], [], { fill: true });
  try {
    expect(await pullChannelHistory({ chatId: '1@newsletter', anchorId: 'false_1@newsletter_50',
      targetCount: 2 })).toBe(38);
    expect(page.models.map((m: any) => m.serverId).sort((a, b) => a - b)).toEqual(range(10, 51));
  } finally {
    page.restore();
  }
});

test('an older page whose hole cannot be filled offline is marked incomplete', async () => {
  // the first page was cut to 701..800; scrolling up while still offline
  // must not answer yesterday's 281..300 under it
  const page = serverPage([...range(281, 300), ...range(701, 800)], [], { fill: 'offline' });
  try {
    await pullChannelHistory({ chatId: '1@newsletter', anchorId: 'false_1@newsletter_701', targetCount: 20 });
    expect(channelOlderPageIncomplete({ chatId: '1@newsletter' })).toBe(true);
    expect(page.fills[0].msgs.map((d: any) => d.serverId)[0]).toBe(300);   // only from the hole up
  } finally {
    page.restore();
  }
});

test('a filled hole, or none, leaves the older page complete', async () => {
  const page = serverPage([...range(281, 300), ...range(701, 800)], [], { fill: true });
  try {
    await pullChannelHistory({ chatId: '1@newsletter', anchorId: 'false_1@newsletter_701', targetCount: 20 });
    expect(channelOlderPageIncomplete({ chatId: '1@newsletter' })).toBe(false);
    expect(page.models.length).toBe(520);
  } finally {
    page.restore();
  }
});

test('a hole too wide to ask id by id drops the old updates below it', async () => {
  const page = serverPage([...range(1, 20), ...range(5001, 5100)], [], { fill: true });
  try {
    await pullChannelHistory({ chatId: '1@newsletter', anchorId: 'false_1@newsletter_5001', targetCount: 20 });
    expect(page.fills).toEqual([]);
    expect(page.models.map((m: any) => m.serverId)).toEqual(range(5001, 5100));
    expect(channelOlderPageIncomplete({ chatId: '1@newsletter' })).toBe(false);
  } finally {
    page.restore();
  }
});

test("a channel's statistics are WhatsApp Web's query, sent by id", () => withPage(async () => {
  const metrics = [{ id: 6, type: 'FOLLOWER', group_by: { number_of_days: 30 } }, { id: 7 }, null];
  const insights = { metrics_status: 'OK', result: [{ id: 6, values: [{ value: 120 }] }] };
  const previous = modules.WAWebMexClient;
  modules.WAWebMexClient = { fetchQuery: async (req: any, vars: any) => {
    calls.push([req.params.id, req.params.name, req.params.operationKind, vars]);
    return { xwa2_newsletter_admin_insights: insights };
  } };
  try {
    expect(await readChannels({ kind: 'insights', id: '1@newsletter', metrics })).toEqual(insights);
    expect(calls).toEqual([['9853618868050977', 'WAWebMexFetchNewsletterInsightsJobQuery', 'query',
      { input: { newsletter_id: '1@newsletter', metrics: [metrics[0]] } }]]);
    expect(await readChannels({ kind: 'insights', id: '1@newsletter', metrics: [{ id: 7 }] })).toBeNull();
    expect(await readChannels({ kind: 'insights', id: 'x', metrics })).toBeNull();
    modules.WAWebMexClient = { fetchQuery: async () => ({}) };
    expect(await readChannels({ kind: 'insights', id: '1@newsletter', metrics })).toBeNull();
  } finally {
    modules.WAWebMexClient = previous;
  }
}));

test('"Show admin profile" goes by id when the page has no job for it', () => withPage(async () => {
  const previous = modules.WAWebMexClient;
  modules.WAWebMexClient = { fetchQuery: async (req: any, vars: any) => {
    calls.push([req.params.id, req.params.operationKind, vars]); return {};
  } };
  try {
    expect(await readChannels({ kind: 'admin-profiles', id: '1@newsletter', enabled: true }))
      .toEqual({ id: '1@newsletter', enabled: true });
    expect(calls).toEqual([['28226671310350649', 'mutation',
      { newsletter_id: '1@newsletter', updates: { settings: { admin_profiles_enabled: true } } }]]);
    expect(await readChannels({ kind: 'admin-profiles', id: '1@newsletter', enabled: 'yes' })).toBeNull();
  } finally {
    modules.WAWebMexClient = previous;
  }
}));

test('the new routes refuse a malformed body before the page is asked', async () => {
  const req: any = { body: {}, client: { page: { evaluate: async () => { throw new Error('asked'); } } } };
  for (const [route, body] of [
    [setChannelReactions, { id: '1@newsletter', value: 'some' }],
    [setChannelReactions, { id: 'x', value: 'ALL' }],
    [channelInsights, { id: '1@newsletter', metrics: [] }],
    [channelInsights, { id: '1@newsletter' }],
    [setChannelAdminProfiles, { id: '1@newsletter', enabled: 1 }],
  ] as any[]) {
    const res = response();
    await route({ ...req, body }, res);
    expect([res.statusCode, res.body.code]).toEqual([400, 'invalid_channel']);
  }
  const res = response();
  await channelInsights({ ...req, body: { id: '1@newsletter', metrics: [{ id: 1, type: 'FOLLOWER' }] } }, res);
  expect([res.statusCode, res.body.code]).toEqual([503, 'channel_insights_unavailable']);
});
