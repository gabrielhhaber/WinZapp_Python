import { promoteAdmin, promoteInPage } from '../../controller/communityAdminController';

function response() {
  const res: any = { statusCode: 0, body: null };
  res.status = (code: number) => { res.statusCode = code; return res; };
  res.json = (body: unknown) => { res.body = body; return res; };
  return res;
}
const COMMUNITY = '111-0@g.us';
const PERSON = '222@lid';

async function inPage(parent: any, job: any, run: () => Promise<string>) {
  const page = globalThis as any;
  const previous = { WPP: page.WPP, require: page.require };
  page.WPP = { whatsapp: {
    GroupMetadataStore: { get: (id: string) => (id === COMMUNITY ? parent : undefined) },
    WidFactory: { createWid: (id: string) => ({ wid: id }) },
  } };
  page.require = (name: string) => {
    if (name !== 'WAWebGroupModifyParticipantsJob' || !job) throw new Error('module not found');
    return job;
  };
  try { return await run(); } finally { page.WPP = previous.WPP; page.require = previous.require; }
}

test('promotes through the job WhatsApp Web itself uses, with ids only', async () => {
  const promote = jest.fn().mockResolvedValue({ status: 207, participants: [{ code: '200' }] });
  const code = await inPage({ isParentGroup: true, isLidAddressingMode: true },
    { promoteCommunityParticipants: promote }, () => promoteInPage(COMMUNITY, PERSON));
  expect(code).toBe('200');
  expect(promote).toHaveBeenCalledWith({ wid: COMMUNITY }, [{ wid: PERSON }], true);
});

test('a group that is not a community is never sent to WhatsApp', async () => {
  const promote = jest.fn();
  const job = { promoteCommunityParticipants: promote };
  expect(await inPage({ isParentGroup: false }, job, () => promoteInPage(COMMUNITY, PERSON))).toBe('not_a_community');
  expect(await inPage(null, job, () => promoteInPage(COMMUNITY, PERSON))).toBe('not_a_community');
  expect(promote).not.toHaveBeenCalled();
});

test.each([
  ['a refusal for the person', { status: 207, participants: [{ code: '403' }] }, '403'],
  ['an answer for nobody', { status: 207, participants: [] }, 'unknown'],
  ['another status', { status: 500, participants: [{ code: '200' }] }, 'unknown'],
  ['no answer', undefined, 'unknown'],
])('%s is not a promotion', async (_name, answer, expected) => {
  const job = { promoteCommunityParticipants: jest.fn().mockResolvedValue(answer) };
  expect(await inPage({ isParentGroup: true }, job, () => promoteInPage(COMMUNITY, PERSON))).toBe(expected);
});

test('a WhatsApp Web without the job fails instead of guessing another way', async () => {
  await expect(inPage({ isParentGroup: true }, null, () => promoteInPage(COMMUNITY, PERSON))).rejects.toThrow();
});

function request(evaluate: jest.Mock, body: unknown = { participant: PERSON }, communityId = COMMUNITY): any {
  return { params: { communityId }, body, client: { page: { isClosed: () => false, evaluate } } };
}

test('the route answers success only for code 200', async () => {
  const evaluate = jest.fn().mockResolvedValue('200');
  const res = response();
  await promoteAdmin(request(evaluate), res);
  expect(res.statusCode).toBe(200);
  expect(res.body).toEqual({ status: 'success', response: { promoted: true } });
  expect(evaluate).toHaveBeenCalledWith(promoteInPage, COMMUNITY, PERSON);
});

test.each(['403', 'unknown'])('WhatsApp answering %s is a refusal', async (code) => {
  const res = response();
  await promoteAdmin(request(jest.fn().mockResolvedValue(code)), res);
  expect(res.statusCode).toBe(409);
  expect(res.body).toEqual({ status: 'error', code: 'promotion_refused' });
});

test('a group the page does not hold as a community is not called a refusal', async () => {
  const res = response();
  await promoteAdmin(request(jest.fn().mockResolvedValue('not_a_community')), res);
  expect(res.statusCode).toBe(404);
  expect(res.body).toEqual({ status: 'error', code: 'not_a_community' });
});

test.each([
  ['a group id as the person', { participant: '333-0@g.us' }, COMMUNITY],
  ['a device id', { participant: '222:3@lid' }, COMMUNITY],
  ['no body', null, COMMUNITY],
  ['a person as the community', { participant: PERSON }, '5511@c.us'],
])('%s never reaches the page', async (_name, body, communityId) => {
  const evaluate = jest.fn();
  const res = response();
  await promoteAdmin(request(evaluate, body, communityId), res);
  expect(res.statusCode).toBe(400);
  expect(evaluate).not.toHaveBeenCalled();
});

test('a failure never carries native text', async () => {
  const res = response();
  await promoteAdmin(request(jest.fn().mockRejectedValue(new Error("Participant '222@lid' not found"))), res);
  expect(res.statusCode).toBe(503);
  expect(res.body).toEqual({ status: 'error', code: 'promotion_unavailable' });
  const closed: any = { params: { communityId: COMMUNITY }, body: { participant: PERSON },
    client: { page: { isClosed: () => true, evaluate: jest.fn() } } };
  const second = response();
  await promoteAdmin(closed, second);
  expect(second.statusCode).toBe(503);
});
