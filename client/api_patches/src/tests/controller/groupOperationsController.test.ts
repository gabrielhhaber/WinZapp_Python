import QRCode from 'qrcode';
import { getGroupInfo, readInvitation, resetInvitation, joinGroupByCode, getGroupInfoFromInviteLink } from '../../controller/groupOperationsController';

jest.mock('qrcode', () => ({ __esModule: true, default: { toDataURL: jest.fn() } }));
const link = 'https://chat.whatsapp.com/AbCdEfGhIjKlMnOpQrStUv';
function response() {
  const res: any = { statusCode: 0, body: null };
  res.status = (code: number) => { res.statusCode = code; return res; };
  res.json = (body: unknown) => { res.body = body; return res; };
  return res;
}
function request() {
  return { params: { groupId: '123-456@g.us' }, body: { inviteCode: link }, client: {
    getChatById: jest.fn().mockResolvedValue({ name: 'Group', groupMetadata: {
      restrict: false, memberAddMode: 'all_member_add', participants: [{id:'111@lid', isAdmin:true}],
    }}),
    getGroupInviteLink: jest.fn().mockResolvedValue(link),
    revokeGroupInviteLink: jest.fn().mockResolvedValue(link),
    joinGroup: jest.fn().mockResolvedValue({ id:'123-456@g.us', pendingApproval:true }),
    getGroupInfoFromInviteLink: jest.fn().mockResolvedValue({id:'123-456@g.us',subject:'Group',size:7,membershipApprovalMode:true}),
    page: { isClosed: () => false, evaluate: jest.fn().mockResolvedValue(true) },
  }} as any;
}

beforeEach(() => { jest.clearAllMocks(); (QRCode.toDataURL as jest.Mock).mockResolvedValue('data:image/png;base64,cG5n'); });

test('QR encodes the exact provider link; read does not reset', async () => {
  const req=request(), res=response(); await readInvitation(req,res);
  expect(res.statusCode).toBe(200);
  expect(res.body.response.link).toBe(link);
  expect(QRCode.toDataURL).toHaveBeenCalledWith(link, {type:'image/png',width:320,margin:4});
  expect(req.client.revokeGroupInviteLink).not.toHaveBeenCalled();
});
test('reset checks real participant collection admin status, not a readable link', async () => {
  const req=request(), res=response(); req.client.page.evaluate.mockResolvedValue(false);
  await resetInvitation(req,res);
  expect(res.statusCode).toBe(403);
  expect(req.client.revokeGroupInviteLink).not.toHaveBeenCalled();
  const evaluator=req.client.page.evaluate.mock.calls[0][0];
  const previous=(globalThis as any).WPP;
  try {
    const wid={_serialized:'123-456@g.us'};
    const find=jest.fn().mockResolvedValue({participants:{iAmAdmin:()=>true}});
    (globalThis as any).WPP={whatsapp:{WidFactory:{createWid:()=>wid},GroupMetadataStore:{find}}};
    expect(await evaluator('123-456@g.us')).toBe(true);
    expect(find).toHaveBeenCalledWith(wid);
  } finally { (globalThis as any).WPP=previous; }
});
test('confirmed reset generates QR from new link exactly once', async () => {
  const req=request(), res=response(), next='https://chat.whatsapp.com/ZyXwVuTsRqPoNmLkJiHgFe';
  req.client.revokeGroupInviteLink.mockResolvedValue(next);
  await resetInvitation(req,res);
  expect(res.body.response.link).toBe(next);
  expect(req.client.revokeGroupInviteLink).toHaveBeenCalledTimes(1);
  expect(req.client.getGroupInviteLink).not.toHaveBeenCalled();
  expect(QRCode.toDataURL).toHaveBeenCalledWith(next, expect.any(Object));
});
test('QR failure after reset neither retries nor exposes a stale link or error text', async () => {
  const req=request(), res=response(); (QRCode.toDataURL as jest.Mock).mockRejectedValue(new Error('private invite secret'));
  await resetInvitation(req,res);
  expect(res.statusCode).toBe(503);
  expect(req.client.revokeGroupInviteLink).toHaveBeenCalledTimes(1);
  expect(JSON.stringify(res.body)).not.toMatch(/private|secret|chat.whatsapp/);
});
test.each(['bad','123@lid','123@g.us;bad'])('rejects group input %s before provider access', async groupId => {
  const req=request(), res=response(); req.params.groupId=groupId;
  await readInvitation(req,res); expect(res.statusCode).toBe(400);
  expect(req.client.getGroupInviteLink).not.toHaveBeenCalled();
});
test('rejects non-WhatsApp or malformed provider links before QR generation', async () => {
  const req=request(), res=response(); req.client.getGroupInviteLink.mockResolvedValue('https://example.com/private');
  await readInvitation(req,res); expect(res.statusCode).toBe(503);
  expect(QRCode.toDataURL).not.toHaveBeenCalled();
});
test.each([true,false])('join preserves id and pendingApproval=%s', async pendingApproval => {
  const req=request(), res=response(); req.client.joinGroup.mockResolvedValue({id:'123-456@g.us',pendingApproval});
  await joinGroupByCode(req,res);
  expect(res.body.response).toEqual({id:'123-456@g.us',pendingApproval});
  expect(req.client.joinGroup).toHaveBeenCalledWith('AbCdEfGhIjKlMnOpQrStUv');
  expect(req.client.joinGroup).toHaveBeenCalledTimes(1);
});
test.each([null,'123@g.us',{id:'123@g.us'},{id:'1@lid',pendingApproval:false}])('ambiguous join %p is not confirmed or retried', async value => {
  const req=request(), res=response(); req.client.joinGroup.mockResolvedValue(value);
  await joinGroupByCode(req,res); expect(res.statusCode).toBe(503);
  expect(req.client.joinGroup).toHaveBeenCalledTimes(1);
});
test('group info preserves display fields and explicit permissions', async () => {
  const req=request(), res=response(); await getGroupInfo(req,res);
  expect(res.body.response).toMatchObject({id:'123-456@g.us',name:'Group',subject:'Group',
    restrict:false,memberAddMode:'all_member_add',participants:[{id:'111@lid',isAdmin:true}]});
  expect(res.body.response.createdAt).toBeNull();
});
test('unknown permission fields stay unknown, never default to unrestricted', async () => {
  const req=request(), res=response(); req.client.getChatById.mockResolvedValue({groupMetadata:{}});
  await getGroupInfo(req,res); expect(res.body.response.restrict).toBeNull();
  expect(res.body.response.memberAddMode).toBeNull();
});

test('preview preserves provider metadata and never joins', async () => {
  const req=request(), res=response(); req.body={invitecode:'AbCdEfGhIjKlMnOpQrStUv'};
  await getGroupInfoFromInviteLink(req,res);
  expect(res.statusCode).toBe(200);
  expect(res.body.response).toMatchObject({id:'123-456@g.us',subject:'Group',size:7,membershipApprovalMode:true});
  expect(req.client.getGroupInfoFromInviteLink).toHaveBeenCalledTimes(1);
  expect(req.client.getGroupInfoFromInviteLink).toHaveBeenCalledWith(req.body.invitecode);
  expect(req.client.joinGroup).not.toHaveBeenCalled();
});
test.each([undefined,'bad','https://example.com/private'])('invalid preview %p never reaches provider', async invitecode => {
  const req=request(), res=response(); req.body={invitecode};
  await getGroupInfoFromInviteLink(req,res);
  expect(res.statusCode).toBe(400);
  expect(req.client.getGroupInfoFromInviteLink).not.toHaveBeenCalled();
});
test('preview error does not log or expose the private code or native error', async () => {
  const req=request(), res=response(); req.body={invitecode:'AbCdEfGhIjKlMnOpQrStUv'};
  req.logger={error:jest.fn()};
  req.client.getGroupInfoFromInviteLink.mockRejectedValue(new Error('private AbCdEfGhIjKlMnOpQrStUv'));
  await getGroupInfoFromInviteLink(req,res);
  expect(res.statusCode).toBe(503);
  expect(JSON.stringify(res.body)).not.toMatch(/private|AbCdEfGhIjKlMnOpQrStUv/);
  expect(req.logger.error).not.toHaveBeenCalled();
  expect(req.client.joinGroup).not.toHaveBeenCalled();
});

// The forms WhatsApp really uses: the web link with its "?mode=" query (seen on
// real messages), a trailing slash or fragment, and the whatsapp: link the
// invitation page's own button opens. Same list as the client's
// tests/test_group_join.py, so the two readings cannot drift apart unnoticed.
test.each([
  link + '?mode=ems_copy_t', link + '/', link + '#anchor', link + '?x=1&y=2',
  'whatsapp://chat?code=AbCdEfGhIjKlMnOpQrStUv', 'whatsapp://chat/?code=AbCdEfGhIjKlMnOpQrStUv',
  'whatsapp://chat?foo=1&code=AbCdEfGhIjKlMnOpQrStUv&bar=2',
])('preview and join read only the code out of %s', async invite => {
  const req=request(), preview=response(), joined=response();
  req.body={invitecode:invite,inviteCode:invite};
  await getGroupInfoFromInviteLink(req,preview);
  await joinGroupByCode(req,joined);
  expect(preview.statusCode).toBe(200);
  expect(joined.statusCode).toBe(201);
  expect(req.client.getGroupInfoFromInviteLink).toHaveBeenCalledWith('AbCdEfGhIjKlMnOpQrStUv');
  expect(req.client.joinGroup).toHaveBeenCalledWith('AbCdEfGhIjKlMnOpQrStUv');
});
test.each([
  'http://chat.whatsapp.com/AbCdEfGhIjKlMnOpQrStUv', link + '/extra',
  'https://chat.whatsapp.com.evil/AbCdEfGhIjKlMnOpQrStUv', 'https://evil.example/?next=' + link,
  'whatsapp://send?phone=5511999999999', 'whatsapp://chat?code=short',
  'whatsapp://chat?mycode=AbCdEfGhIjKlMnOpQrStUv', link + ' and more text',
  link + '?' + 'a'.repeat(600), 42, null,
])('anything else (%p) is refused by preview and join before provider access', async invite => {
  const req=request(), preview=response(), joined=response();
  req.body={invitecode:invite,inviteCode:invite};
  await getGroupInfoFromInviteLink(req,preview);
  await joinGroupByCode(req,joined);
  expect(preview.statusCode).toBe(400);
  expect(joined.statusCode).toBe(400);
  expect(req.client.getGroupInfoFromInviteLink).not.toHaveBeenCalled();
  expect(req.client.joinGroup).not.toHaveBeenCalled();
});

test.each([link,link.replace('com/','com/invite/')])('preview and join retain full WhatsApp link support: %s', async invite => {
  const req=request(), preview=response(), joined=response();
  req.body={invitecode:invite,inviteCode:invite};
  await getGroupInfoFromInviteLink(req,preview);
  await joinGroupByCode(req,joined);
  expect(preview.statusCode).toBe(200);
  expect(joined.statusCode).toBe(201);
  expect(req.client.getGroupInfoFromInviteLink).toHaveBeenCalledWith('AbCdEfGhIjKlMnOpQrStUv');
  expect(req.client.joinGroup).toHaveBeenCalledWith('AbCdEfGhIjKlMnOpQrStUv');
});
