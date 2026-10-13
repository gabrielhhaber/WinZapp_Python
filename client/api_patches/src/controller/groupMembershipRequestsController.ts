import type { Request, Response } from 'express';

const validGroup = (value: unknown): value is string =>
  typeof value === 'string' && /^\d+(-\d+)?@g\.us$/.test(value);
const validPerson = (value: unknown): value is string =>
  typeof value === 'string' && /^\d+@(c\.us|lid)$/.test(value);

// Pending requests to join a group ("approve new participants"), and the
// admin's answer to them.
//
// Routes of WinZapp's own because wppconnect-server has none; the library
// functions behind them are used as they are (getGroupMembershipRequests,
// approveGroupMembershipRequest, rejectGroupMembershipRequest), with one
// addition when reading.
//
// WPP.group.getMembershipRequests() reads the page's local table, which is
// only as fresh as the last time WhatsApp Web asked the server. So the server
// is asked first, with the job WhatsApp Web's own interface uses: read over
// CDP (WhatsApp Web 2.3000.1049387914) it answered for groups the account
// administers and refused with status 403 for one it does not. No group of
// the account read had a pending request, so the shape of an entry comes
// from the library's own typing and WhatsApp Web's program text, not from a
// run; everything read from one is checked before it leaves the page.
//
// Runs inside the page: self-contained, no closure over anything here.
export async function readRequestsInPage(groupId: string): Promise<unknown> {
  const page = globalThis as any;
  const WPP = page.WPP;
  if (typeof WPP?.group?.getMembershipRequests !== 'function') return null;
  let refreshed = false;
  try {
    const job = page.require('WAWebGroupGetMembershipApprovalRequestsJob');
    await job.queryAndUpdateGroupMembershipApprovalRequests(WPP.whatsapp.WidFactory.createWid(groupId));
    refreshed = true;
  } catch (error: any) {
    if (error?.status === 403) return 'forbidden';
    // Any other failure: the local table is still worth showing, marked stale.
  }
  const text = (wid: any) =>
    typeof wid === 'string' ? wid : typeof wid?._serialized === 'string' ? wid._serialized : '';
  const person = (value: string) => (/^\d+@(c\.us|lid)$/.test(value) ? value : '');
  const list = await WPP.group.getMembershipRequests(groupId);
  const requests: unknown[] = [];
  for (const entry of Array.isArray(list) ? list : []) {
    const id = person(text(entry?.id));
    if (!id) continue;
    let phone = '';
    let name = '';
    try {
      // What lets the admin tell who is asking: a requester is not a member,
      // so the client may know neither the number behind a @lid nor a name.
      phone = id.endsWith('@c.us') ? id : person(text(WPP.whatsapp.functions?.getPhoneNumber?.(entry.id)));
      const contact = WPP.whatsapp.ContactStore?.get?.(entry.id);
      const known = contact?.name || contact?.pushname || contact?.notifyName;
      name = typeof known === 'string' ? known : '';
    } catch (_) {
      // Unknown stays unknown.
    }
    const method = entry?.requestMethod;
    requests.push({
      id,
      addedBy: person(text(entry?.addedBy)),
      phone,
      name,
      method: ['InviteLink', 'LinkedGroupJoin', 'NonAdminAdd'].includes(method) ? method : '',
      t: typeof entry?.t === 'number' && Number.isFinite(entry.t) && entry.t > 0 ? entry.t : null,
    });
  }
  return { refreshed, requests };
}

export async function listRequests(req: Request, res: Response) {
  const groupId = req.params.groupId;
  if (!validGroup(groupId)) return res.status(400).json({ status: 'error', code: 'invalid_request' });
  try {
    const page: any = (req.client as any)?.page;
    if (!page || page.isClosed?.()) throw new Error('membership_requests_unavailable');
    const answer: any = await page.evaluate(readRequestsInPage, groupId);
    if (answer === 'forbidden') return res.status(403).json({ status: 'error', code: 'not_admin' });
    if (!answer || !Array.isArray(answer.requests)) throw new Error('membership_requests_unavailable');
    return res.status(200).json({ status: 'success', response: answer });
  } catch (_) {
    // Never the native error: it can carry a requester's id.
    return res.status(503).json({ status: 'error', code: 'membership_requests_unavailable' });
  }
}

// The library's answer is not interpreted: it could not be read from a real
// request. "Not refused" is all a 200 says; the client reads the pending list
// again and believes the action when the requester has left it.
async function answerRequests(req: Request, res: Response, approve: boolean) {
  const groupId = req.params.groupId;
  const participants = req.body?.participants;
  if (!validGroup(groupId) || !Array.isArray(participants) || participants.length < 1
      || participants.length > 50 || !participants.every(validPerson)) {
    return res.status(400).json({ status: 'error', code: 'invalid_request' });
  }
  try {
    const client: any = req.client;
    if (approve) await client.approveGroupMembershipRequest(groupId, participants);
    else await client.rejectGroupMembershipRequest(groupId, participants);
    return res.status(200).json({ status: 'success', response: { accepted: true } });
  } catch (_) {
    return res.status(503).json({ status: 'error', code: 'membership_action_failed' });
  }
}

export const approveRequests = (req: Request, res: Response) => answerRequests(req, res, true);
export const rejectRequests = (req: Request, res: Response) => answerRequests(req, res, false);
