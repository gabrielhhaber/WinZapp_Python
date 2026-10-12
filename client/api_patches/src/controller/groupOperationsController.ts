import type { Request, Response } from 'express';
import QRCode from 'qrcode';

const validGroup = (value: unknown): value is string =>
  typeof value === 'string' && /^\d+(-\d+)?@g\.us$/.test(value);
const validLink = (value: unknown): value is string =>
  typeof value === 'string' && /^https:\/\/chat\.whatsapp\.com\/[A-Za-z0-9]{10,64}$/.test(value);

// Preserve the existing group-info contract, adding only known permission fields.
export async function getGroupInfo(req: Request, res: Response) {
  const groupId = req.params.groupId;
  if (!validGroup(groupId)) return res.status(400).json({ status: 'error' });
  try {
    const chat: any = await req.client.getChatById(groupId);
    const meta = chat?.groupMetadata;
    if (!meta) throw new Error('group_unavailable');
    const date = (value: unknown) => typeof value === 'number' && Number.isFinite(value) && value > 0
      ? new Date(value * 1000).toISOString() : null;
    const id = (value: any) => value?._serialized || (typeof value === 'string' ? value : null);
    const name = chat.name || chat.formattedTitle || '';
    return res.status(200).json({ status: 'success', response: {
      id: id(chat.id) || groupId, name, subject: meta.subject || name,
      description: meta.desc || chat.description || null,
      subjectUpdatedAt: date(meta.subjectTime || meta.subjectUpdatedAt),
      subjectUpdatedBy: id(meta.subjectOwner) || id(meta.subjectUpdatedBy),
      descriptionUpdatedAt: date(meta.descTime || meta.descriptionTime),
      descriptionUpdatedBy: id(meta.descOwner) || id(meta.descriptionUpdatedBy),
      createdAt: date(meta.creation), lastActivityAt: date(chat.t || chat.lastActivity),
      participants: Array.isArray(meta.participants) ? meta.participants.map((p: any) => ({
        id: id(p?.id) || '', isAdmin: Boolean(p?.isAdmin || p?.isSuperAdmin || p?.admin),
      })) : [],
      restrict: typeof meta.restrict === 'boolean' ? meta.restrict : null,
      memberAddMode: ['admin_add', 'all_member_add'].includes(meta.memberAddMode) ? meta.memberAddMode : null,
    } });
  } catch (_) {
    return res.status(503).json({ status: 'error', code: 'group_unavailable' });
  }
}

async function invitation(req: Request, res: Response, reset: boolean) {
  const groupId = req.params.groupId;
  if (!validGroup(groupId)) return res.status(400).json({ status: 'error' });
  try {
    // Provider enforces current link permissions. Reset additionally requires
    // confirmed admin metadata; never infer it from a readable invite link.
    if (reset) {
      const page = (req.client as any).page;
      const isAdmin = page && !page.isClosed() && await page.evaluate(async (jid: string) => {
        const WPP = (globalThis as any).WPP;
        const wid = WPP.whatsapp.WidFactory.createWid(jid);
        const meta = await WPP.whatsapp.GroupMetadataStore.find(wid);
        return meta?.participants?.iAmAdmin() === true;
      }, groupId);
      if (isAdmin !== true) {
        return res.status(403).json({ status: 'error', code: 'admin_required' });
      }
    }
    const link = reset ? await req.client.revokeGroupInviteLink(groupId)
      : await req.client.getGroupInviteLink(groupId);
    if (!validLink(link)) throw new Error('invalid_link');
    const qr = await QRCode.toDataURL(link, { type: 'image/png', width: 320, margin: 4 });
    return res.status(200).json({ status: 'success', response: { link, qr } });
  } catch (_) {
    // Reset may already have happened. Do not return an old link or retry it.
    return res.status(503).json({ status: 'error', code: 'invitation_unconfirmed' });
  }
}

export async function readInvitation(req: Request, res: Response) {
  return invitation(req, res, false);
}

export async function resetInvitation(req: Request, res: Response) {
  return invitation(req, res, true);
}

export async function joinGroupByCode(req: Request, res: Response) {
  const value = req.body?.inviteCode;
  const code = typeof value === 'string' && validLink(value) ? value.split('/').pop() : value;
  if (typeof code !== 'string' || !/^[A-Za-z0-9]{10,64}$/.test(code)) {
    return res.status(400).json({ status: 'error', code: 'invalid_invite' });
  }
  try {
    const result = await req.client.joinGroup(code);
    if (!result || !validGroup(result.id) || typeof result.pendingApproval !== 'boolean') {
      throw new Error('join_unconfirmed');
    }
    return res.status(201).json({ status: 'success', response: {
      id: result.id, pendingApproval: result.pendingApproval,
    } });
  } catch (_) {
    return res.status(503).json({ status: 'error', code: 'join_unconfirmed' });
  }
}
