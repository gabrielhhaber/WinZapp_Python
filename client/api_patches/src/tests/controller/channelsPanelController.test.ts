import {
  channelDirectory, channelInfo, channelKey, followChannel, listChannels, readChannels,
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
  WAWebMexFetchNewsletterJob: {
    mexGetNewsletter: async (key: string, role: string) => { calls.push(['info', key, role]); return { xwa2_newsletter: node }; },
  },
};

function withPage<T>(run: () => Promise<T>): Promise<T> {
  const root = globalThis as any;
  const previous = [root.require, root.WPP];
  root.require = (name: string) => modules[name];
  root.WPP = { newsletter: { follow: async (id: string) => calls.push(['follow', id]),
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
