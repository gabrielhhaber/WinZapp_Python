import type { Request, Response } from 'express';

const validGroup = (value: unknown): value is string =>
  typeof value === 'string' && /^\d+(-\d+)?@g\.us$/.test(value);
const validPerson = (value: unknown): value is string =>
  typeof value === 'string' && /^\d+@(c\.us|lid)$/.test(value);

// Make a member of a community one of its admins.
//
// A route of WinZapp's own because the library's cannot do it.
// WPP.community.promoteParticipants() looks the person up in the community's
// own participant list and throws group_participant_not_found when they are
// not in it; and that list holds the community's admins only, also on an
// admin's own account. So it could only ever "promote" someone who already is
// an admin. Run on a real community (WhatsApp Web 2.3000.1049387914, wa-js
// 4.6.1): a member added through the announcement group was in that group's
// list and in getParticipants(), not in the community's, and the library
// function threw.
//
// What WhatsApp Web's own interface calls is the job below, with ids only; run
// on the same community it answered status 207 and code "200" for the person,
// who was an admin on the next reading. Demoting stays with the library: an
// admin is in the list it looks at.
//
// Runs inside the page: self-contained, no closure over anything here.
// Answers WhatsApp's code for the person ("200" when promoted).
export async function promoteInPage(communityId: string, participantId: string): Promise<string> {
  const page = globalThis as any;
  const whatsapp = page.WPP?.whatsapp;
  const parent = whatsapp?.GroupMetadataStore?.get(communityId);
  if (!parent || parent.isParentGroup !== true) return 'not_a_community';
  const job = page.require('WAWebGroupModifyParticipantsJob');
  const result = await job.promoteCommunityParticipants(
    whatsapp.WidFactory.createWid(communityId),
    [whatsapp.WidFactory.createWid(participantId)],
    parent.isLidAddressingMode === true
  );
  const rows = Array.isArray(result?.participants) ? result.participants : [];
  return result?.status === 207 && rows.length === 1 ? String(rows[0]?.code) : 'unknown';
}

export async function promoteAdmin(req: Request, res: Response) {
  const communityId = req.params.communityId;
  const participant = req.body?.participant;
  if (!validGroup(communityId) || !validPerson(participant)) {
    return res.status(400).json({ status: 'error', code: 'invalid_request' });
  }
  try {
    const page: any = (req.client as any)?.page;
    if (!page || page.isClosed?.()) throw new Error('promotion_unavailable');
    const code = await page.evaluate(promoteInPage, communityId, participant);
    if (code === '200') return res.status(200).json({ status: 'success', response: { promoted: true } });
    // WhatsApp answered, and the answer was no: not a fault of the connection.
    return res.status(409).json({ status: 'error', code: 'promotion_refused' });
  } catch (_) {
    // Never the native error: it can carry a participant's id.
    return res.status(503).json({ status: 'error', code: 'promotion_unavailable' });
  }
}
