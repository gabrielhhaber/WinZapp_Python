import {
  approveRequests, listRequests, readRequestsInPage, rejectRequests,
} from '../../controller/groupMembershipRequestsController';

function response() {
  const res: any = { statusCode: 0, body: null };
  res.status = (code: number) => { res.statusCode = code; return res; };
  res.json = (body: unknown) => { res.body = body; return res; };
  return res;
}
const GROUP = '111-0@g.us';
const wid = (value: string) => ({ _serialized: value });

async function inPage(setup: { list?: any; refresh?: jest.Mock | null; phone?: any; contact?: any },
                      run: () => Promise<unknown>) {
  const page = globalThis as any;
  const previous = { WPP: page.WPP, require: page.require };
  page.WPP = {
    group: { getMembershipRequests: jest.fn().mockResolvedValue(setup.list ?? []) },
    whatsapp: {
      WidFactory: { createWid: (id: string) => ({ wid: id }) },
      functions: { getPhoneNumber: () => setup.phone },
      ContactStore: { get: () => setup.contact },
    },
  };
  page.require = (name: string) => {
    if (name !== 'WAWebGroupGetMembershipApprovalRequestsJob' || !setup.refresh) throw new Error('module not found');
    return { queryAndUpdateGroupMembershipApprovalRequests: setup.refresh };
  };
  try { return await run(); } finally { page.WPP = previous.WPP; page.require = previous.require; }
}

test('asks the server first, then reads what the page holds', async () => {
  const refresh = jest.fn().mockResolvedValue(undefined);
  const list = [{ id: wid('222@lid'), addedBy: wid('333@lid'), requestMethod: 'NonAdminAdd', t: 1790000000 }];
  const answer = await inPage({ list, refresh, phone: wid('5511900000002@c.us'), contact: { pushname: 'Bea' } },
    () => readRequestsInPage(GROUP));
  expect(refresh).toHaveBeenCalledWith({ wid: GROUP });
  expect(answer).toEqual({ refreshed: true, requests: [{
    id: '222@lid', addedBy: '333@lid', phone: '5511900000002@c.us', name: 'Bea',
    method: 'NonAdminAdd', t: 1790000000 }] });
});

test('the server refusing with 403 is "not an admin", not an empty list', async () => {
  const refresh = jest.fn().mockRejectedValue(Object.assign(new Error('forbidden'), { status: 403 }));
  expect(await inPage({ refresh }, () => readRequestsInPage(GROUP))).toBe('forbidden');
});

test('when the server cannot be asked the local list is shown, marked as not refreshed', async () => {
  const list = [{ id: wid('5511900000002@c.us'), addedBy: wid('5511900000002@c.us'), requestMethod: 'InviteLink', t: 5 }];
  const offline = jest.fn().mockRejectedValue(new Error('timeout'));
  for (const refresh of [offline, null]) {
    const answer: any = await inPage({ list, refresh }, () => readRequestsInPage(GROUP));
    expect(answer.refreshed).toBe(false);
    expect(answer.requests[0]).toMatchObject({ id: '5511900000002@c.us', phone: '5511900000002@c.us', method: 'InviteLink' });
  }
});

test('what is not a person, a known method or a time never leaves the page', async () => {
  const list = [
    { id: wid('444-0@g.us'), requestMethod: 'InviteLink', t: 1 },
    null,
    { id: wid('222@lid'), addedBy: wid('junk'), requestMethod: 'SomethingNew', t: 'soon' },
  ];
  const answer: any = await inPage({ list, refresh: jest.fn(), phone: wid('not a number'), contact: { name: 7 } },
    () => readRequestsInPage(GROUP));
  expect(answer.requests).toEqual([{ id: '222@lid', addedBy: '', phone: '', name: '', method: '', t: null }]);
});

test('a page without the library answers nothing', async () => {
  const page = globalThis as any;
  const previous = page.WPP;
  page.WPP = undefined;
  try { expect(await readRequestsInPage(GROUP)).toBeNull(); } finally { page.WPP = previous; }
});

function listing(evaluate: jest.Mock, groupId = GROUP): any {
  return { params: { groupId }, client: { page: { isClosed: () => false, evaluate } } };
}

test('the route wraps the page answer', async () => {
  const evaluate = jest.fn().mockResolvedValue({ refreshed: true, requests: [] });
  const res = response();
  await listRequests(listing(evaluate), res);
  expect(res.statusCode).toBe(200);
  expect(res.body).toEqual({ status: 'success', response: { refreshed: true, requests: [] } });
  expect(evaluate).toHaveBeenCalledWith(readRequestsInPage, GROUP);
});

test('not an admin, unavailable and malformed are told apart', async () => {
  const forbidden = response();
  await listRequests(listing(jest.fn().mockResolvedValue('forbidden')), forbidden);
  expect(forbidden.statusCode).toBe(403);
  expect(forbidden.body).toEqual({ status: 'error', code: 'not_admin' });
  for (const evaluate of [jest.fn().mockResolvedValue(null), jest.fn().mockResolvedValue({ requests: 'x' }),
                          jest.fn().mockRejectedValue(new Error('222@lid is private'))]) {
    const res = response();
    await listRequests(listing(evaluate), res);
    expect(res.statusCode).toBe(503);
    expect(res.body).toEqual({ status: 'error', code: 'membership_requests_unavailable' });
  }
  const invalid = response();
  const evaluate = jest.fn();
  await listRequests(listing(evaluate, '5511@c.us'), invalid);
  expect(invalid.statusCode).toBe(400);
  expect(evaluate).not.toHaveBeenCalled();
});

function answering(participants: unknown, groupId = GROUP) {
  const client = { approveGroupMembershipRequest: jest.fn().mockResolvedValue([{ wid: wid('222@lid'), error: null }]),
                   rejectGroupMembershipRequest: jest.fn().mockResolvedValue(undefined) };
  return { req: { params: { groupId }, body: { participants }, client } as any, client };
}

test('approve and reject call the library functions and promise nothing about the result', async () => {
  const approve = answering(['222@lid']);
  const approved = response();
  await approveRequests(approve.req, approved);
  expect(approve.client.approveGroupMembershipRequest).toHaveBeenCalledWith(GROUP, ['222@lid']);
  expect(approve.client.rejectGroupMembershipRequest).not.toHaveBeenCalled();
  expect(approved.body).toEqual({ status: 'success', response: { accepted: true } });
  const reject = answering(['222@lid', '5511900000002@c.us']);
  const rejected = response();
  await rejectRequests(reject.req, rejected);
  expect(reject.client.rejectGroupMembershipRequest).toHaveBeenCalledWith(GROUP, ['222@lid', '5511900000002@c.us']);
  expect(reject.client.approveGroupMembershipRequest).not.toHaveBeenCalled();
  expect(rejected.statusCode).toBe(200);
});

test.each([
  ['nobody', []], ['not a list', '222@lid'], ['a group', ['444-0@g.us']], ['a device id', ['222:1@lid']],
  ['too many', Array.from({ length: 51 }, (_v, index) => `${1000 + index}@lid`)],
])('%s is refused before the library is called', async (_name, participants) => {
  const { req, client } = answering(participants);
  const res = response();
  await approveRequests(req, res);
  expect(res.statusCode).toBe(400);
  expect(client.approveGroupMembershipRequest).not.toHaveBeenCalled();
});

test('a library failure never carries native text', async () => {
  const { req, client } = answering(['222@lid']);
  client.rejectGroupMembershipRequest.mockRejectedValue(new Error('Error on reject member 222@lid'));
  const res = response();
  await rejectRequests(req, res);
  expect(res.statusCode).toBe(503);
  expect(res.body).toEqual({ status: 'error', code: 'membership_action_failed' });
});
