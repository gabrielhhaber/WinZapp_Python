import { listCommunities, readCommunities } from '../../controller/communityOverviewController';

function response() {
  const res: any = { statusCode: 0, body: null };
  res.status = (code: number) => { res.statusCode = code; return res; };
  res.json = (body: unknown) => { res.body = body; return res; };
  return res;
}
const wid = (value: string) => ({ _serialized: value, toString: () => value });
function participants(list: any[], me?: { admin?: boolean; superAdmin?: boolean }) {
  return {
    getModelsArray: () => list,
    iAmAdmin: () => !!me?.admin,
    iAmSuperAdmin: () => !!me?.superAdmin,
  };
}
function store(models: any[]) {
  const byId = new Map(models.map((m) => [m.id._serialized, m]));
  return { getModelsArray: () => models, get: (id: string) => byId.get(id) };
}
function withPage<T>(models: any[] | null, run: () => T): T {
  const previous = (globalThis as any).WPP;
  (globalThis as any).WPP = models ? { whatsapp: { GroupMetadataStore: store(models) } } : undefined;
  try { return run(); } finally { (globalThis as any).WPP = previous; }
}

const announcements = { id: wid('111-1@g.us'), subject: 'Avisos', defaultSubgroup: true };
const general = { id: wid('111-2@g.us'), subject: 'Geral', generalSubgroup: true };
const plain = { id: wid('111-3@g.us'), subject: 'Estudos' };
const community = {
  id: wid('111-0@g.us'), isParentGroup: true, subject: 'Escola', desc: 'Turma',
  isParentGroupClosed: true,
  joinedSubgroups: [announcements.id, general.id, plain.id, wid('999@lid'), wid('111-9@g.us')],
  participants: participants([
    { id: wid('5511@c.us'), isAdmin: true, isSuperAdmin: true },
    { id: wid('5522@c.us'), isAdmin: true },
    { id: wid('5533@c.us'), isAdmin: false },
  ], { admin: true }),
};
const ordinaryGroup = { id: wid('222-0@g.us'), subject: 'Família', participants: participants([]) };

test('lists each community with its admins and the groups joined', () => {
  const result: any = withPage([community, announcements, general, plain, ordinaryGroup], readCommunities);
  expect(result).toHaveLength(1);
  expect(result[0]).toMatchObject({
    id: '111-0@g.us', subject: 'Escola', description: 'Turma', closed: true,
    iAmAdmin: true, iAmSuperAdmin: false,
  });
  expect(result[0].admins).toEqual([
    { id: '5511@c.us', superAdmin: true },
    { id: '5522@c.us', superAdmin: false },
  ]);
});

test('the announcement group is the default subgroup, not a groupType WhatsApp Web no longer fills', () => {
  const result: any = withPage([community, announcements, general, plain], readCommunities);
  expect(result[0].subgroups).toEqual([
    { id: '111-1@g.us', subject: 'Avisos', announcement: true, general: false },
    { id: '111-2@g.us', subject: 'Geral', announcement: false, general: true },
    { id: '111-3@g.us', subject: 'Estudos', announcement: false, general: false },
    // Joined but not loaded yet: listed, with no name to show.
    { id: '111-9@g.us', subject: '', announcement: false, general: false },
  ]);
});

test('an id that is not a group never reaches the client', () => {
  const odd = { ...community, id: wid('not-a-group@lid') };
  expect(withPage([odd], readCommunities)).toEqual([]);
  const result: any = withPage([community, announcements], readCommunities);
  expect(result[0].subgroups.map((s: any) => s.id)).not.toContain('999@lid');
});

test('admin status that cannot be read is "not an admin", never a guess', () => {
  const broken = { ...community, participants: { getModelsArray: () => { throw new Error('private'); } } };
  const result: any = withPage([broken], readCommunities);
  expect(result[0]).toMatchObject({ iAmAdmin: false, iAmSuperAdmin: false, admins: [] });
});

test('a page with no store answers nothing rather than an empty list', () => {
  expect(withPage(null, readCommunities)).toBeNull();
});

test('the route wraps the page answer and never joins, creates or changes anything', async () => {
  const evaluate = jest.fn().mockResolvedValue([{ id: '111-0@g.us' }]);
  const req: any = { client: { page: { isClosed: () => false, evaluate }, createCommunity: jest.fn() } };
  const res = response();
  await listCommunities(req, res);
  expect(res.statusCode).toBe(200);
  expect(res.body).toEqual({ status: 'success', response: [{ id: '111-0@g.us' }] });
  expect(evaluate).toHaveBeenCalledWith(readCommunities);
  expect(req.client.createCommunity).not.toHaveBeenCalled();
});

test.each([
  ['a closed page', { isClosed: () => true, evaluate: jest.fn() }],
  ['no store in the page', { isClosed: () => false, evaluate: jest.fn().mockResolvedValue(null) }],
  ['a native failure', { isClosed: () => false, evaluate: jest.fn().mockRejectedValue(new Error('private group name')) }],
])('%s is unavailable, with no native text in the answer', async (_name, page) => {
  const res = response();
  await listCommunities({ client: { page } } as any, res);
  expect(res.statusCode).toBe(503);
  expect(res.body).toEqual({ status: 'error', code: 'communities_unavailable' });
  expect(JSON.stringify(res.body)).not.toMatch(/private/);
});
