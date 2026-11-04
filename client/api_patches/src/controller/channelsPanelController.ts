import type { Request, Response } from 'express';

// WinZapp's channels window (Alt+9): the channels the account follows, the
// channel directory (search and recommended), one channel by id or invite
// link, follow, unfollow, mute and unmute. Creating, editing and deleting a
// channel are the upstream newsletter routes, used as they are.
//
// Every read asks WhatsApp the way WhatsApp Web's own Channels tab does, with
// its MEX jobs (read over CDP on 2026-11-04, 2.3000.x):
// - WAWebMexFetchAllNewslettersMetadataJob.mexFetchAllNewsletters, query
//   25399611239711790, answer xwa2_newsletter_subscribed;
// - WAWebMexFetchNewsletterDirectorySearchResultsJob, query 26301059626252132,
//   answer xwa2_newsletters_directory_search.result;
// - WAWebMexFetchRecommendedNewslettersJob, query 25806748772361516, answer
//   xwa2_newsletters_recommended.result;
// - WAWebMexFetchNewsletterJob.mexGetNewsletter, query 27456920720571478
//   (type JID or INVITE), answer xwa2_newsletter;
// - WAWebMexFetchNewsletterDirectoryListJob, query 26125047313831973 (by view
//   and category), answer xwa2_newsletters_directory_list.result;
// - WAWebNewsletterPinMessageAction (pin and unpin an update by server id,
//   queries 27165709459706559 and 28007176042216937);
// - WAWebMexFetchNewsletterAdminInfoJob (the admin count, 26278439461859188),
//   WAWebNewsletterSendMsgAction.sendNewsletterAdminInviteMessage (creates the
//   invite, 9387141988078609, and sends it to the person's chat) and the owner's
//   WAWebMexRevokeNewsletterAdminInviteJob (9656078347839416).
// The answers go to the client as WhatsApp gives them (XWA2Newsletter nodes):
// the Neonize bridge sends the same queries and answers the same nodes, and the
// client reads them once (client/core/channels.py).
//
// Runs inside the page: self-contained, no closure over anything here.
export async function readChannels(request: any): Promise<unknown> {
  const root = globalThis as any;
  const load = (name: string): any => {
    try { return typeof root.require === 'function' ? root.require(name) : null; } catch (_) { return null; }
  };
  const kind = request?.kind;
  if (kind === 'list') {
    const job = load('WAWebMexFetchAllNewslettersMetadataJob');
    if (typeof job?.mexFetchAllNewsletters !== 'function') return null;
    const data = await job.mexFetchAllNewsletters({ fetchWamoSub: false, fetchStatusMetadata: false });
    const list = data?.xwa2_newsletter_subscribed;
    return Array.isArray(list) ? list : null;
  }
  if (kind === 'directory') {
    const query = typeof request.query === 'string' ? request.query.trim().slice(0, 100) : '';
    const limit = Math.max(1, Math.min(50, Number(request.limit) || 20));
    // The directory's own enums: categories (WAWebNewsletterDirectoryCategoryUtils,
    // without the seasonal "special events") and list views (FilterUtils).
    const categoryEnum = load('WAWebNewsletterDirectoryCategoryUtils')?.NewsletterDirectoryCategoryType;
    const viewEnum = load('WAWebNewsletterDirectoryFilterUtils')?.NewsletterDirectoryListView;
    const categoryNames: any = { BUSINESS: 'Business', ENTERTAINMENT: 'Entertainment',
      LIFESTYLE: 'Lifestyle', NEWS: 'News', ORGANIZATIONS: 'Organizations', PEOPLE: 'People',
      SPORTS: 'Sports' };
    const viewNames: any = { RECOMMENDED: 'Recommended', TRENDING: 'Trending', POPULAR: 'Popular',
      NEW: 'New' };
    const categoryName = categoryNames[String(request.category || '').toUpperCase()];
    const categories = categoryName && categoryEnum?.[categoryName] != null ? [categoryEnum[categoryName]] : [];
    const view = viewNames[String(request.view || '').toUpperCase()] ? String(request.view).toUpperCase() : '';
    if (query) {
      const job = load('WAWebMexFetchNewsletterDirectorySearchResultsJob');
      if (typeof job?.mexFetchNewsletterDirectorySearchResults !== 'function') return null;
      const data = await job.mexFetchNewsletterDirectorySearchResults({
        searchText: query, categories, limit, cursorToken: null });
      const result = data?.xwa2_newsletters_directory_search?.result;
      return Array.isArray(result) ? result : null;
    }
    if (categories.length || (view && view !== 'RECOMMENDED')) {
      const job = load('WAWebMexFetchNewsletterDirectoryListJob');
      const listView = viewEnum?.[viewNames[view] || 'Recommended'];
      if (typeof job?.mexFetchNewsletterDirectoryList !== 'function' || listView == null) return null;
      const data = await job.mexFetchNewsletterDirectoryList({
        view: listView, categories, countryCodes: [], limit, cursorToken: null });
      const result = data?.xwa2_newsletters_directory_list?.result;
      return Array.isArray(result) ? result : null;
    }
    const job = load('WAWebMexFetchRecommendedNewslettersJob');
    if (typeof job?.mexFetchRecommendedNewsletters !== 'function') return null;
    const data = await job.mexFetchRecommendedNewsletters(limit, []);
    const result = data?.xwa2_newsletters_recommended?.result;
    return Array.isArray(result) ? result : null;
  }
  if (kind === 'info') {
    const key = typeof request.key === 'string' ? request.key.trim() : '';
    if (!/^(?:\d+@newsletter|[A-Za-z0-9_-]{10,64})$/.test(key)) return null;
    const job = load('WAWebMexFetchNewsletterJob');
    if (typeof job?.mexGetNewsletter !== 'function') return null;
    const data = await job.mexGetNewsletter(key, 'GUEST', {
      fetchViewerMetadata: true, fetchCreationTime: true, fetchWamoSub: false, fetchStatusMetadata: false });
    return data?.xwa2_newsletter ?? null;
  }
  if (kind === 'follow' || kind === 'unfollow') {
    const id = typeof request.id === 'string' ? request.id : '';
    const action = root.WPP?.newsletter?.[kind];
    if (!/^\d+@newsletter$/.test(id) || typeof action !== 'function') return null;
    await action(id);
    return { id };
  }
  if (kind === 'pin') {
    // WhatsApp Web's own action (it also updates the page's pinned list), on
    // the page's channel; the job alone when the page has not loaded it.
    const id = typeof request.id === 'string' ? request.id : '';
    const serverId = request.serverId;
    if (!/^\d+@newsletter$/.test(id) || !Number.isInteger(serverId) || serverId <= 0
        || typeof request.pin !== 'boolean') return null;
    const channel = load('WAWebCollections')?.WAWebNewsletterCollection?.get?.(id);
    const action = load('WAWebNewsletterPinMessageAction');
    const run = request.pin ? action?.pinNewsletterMessage : action?.unpinNewsletterMessage;
    if (channel && typeof run === 'function') {
      await run(channel, serverId);
    } else {
      const job = request.pin
        ? load('WAWebNewsletterPinMessagesJob')?.pinNewsletterMessages
        : load('WAWebNewsletterUnpinMessagesJob')?.unpinNewsletterMessages;
      if (typeof job !== 'function') return null;
      await job(id, [String(serverId)]);
    }
    return { id, serverId, pinned: request.pin };
  }
  const person = (text: unknown): string => {
    const match = String(text ?? '').trim().match(/^(\d{5,20})@(lid|c\.us|s\.whatsapp\.net)$/);
    return match ? `${match[1]}@${match[2] === 'lid' ? 'lid' : 'c.us'}` : '';
  };
  if (kind === 'admins') {
    const id = typeof request.id === 'string' ? request.id : '';
    const job = load('WAWebMexFetchNewsletterAdminInfoJob');
    if (!/^\d+@newsletter$/.test(id) || typeof job?.mexFetchNewsletterAdminInfo !== 'function') return null;
    const info = await job.mexFetchNewsletterAdminInfo(id);
    const count = Number(info?.adminCount);
    return { id, adminCount: Number.isInteger(count) && count >= 0 ? count : null };
  }
  if (kind === 'admin-invite') {
    const id = typeof request.id === 'string' ? request.id : '';
    const user = person(request.user);
    const caption = String(request.message ?? '').trim().slice(0, 500);
    const factory = load('WAWebWidFactory');
    const send = load('WAWebNewsletterSendMsgAction')?.sendNewsletterAdminInviteMessage;
    if (!/^\d+@newsletter$/.test(id) || !user || typeof factory?.createWid !== 'function'
        || typeof send !== 'function' || typeof root.WPP?.chat?.find !== 'function') return null;
    // The person's chat, which the invite message goes to (created when new).
    const chat = await root.WPP.chat.find(user);
    if (!chat) return null;
    const result = await send(chat, { base64Thumb: null, invitee: factory.createWid(user),
      inviteMessage: caption, newsletterWid: factory.createWid(id) });
    if (/^ERROR/.test(String(result?.messageSendResult ?? ''))) return null;
    return { id, user: request.user, expiration: 0 };
  }
  if (kind === 'admin-revoke') {
    const id = typeof request.id === 'string' ? request.id : '';
    const user = person(request.user);
    const factory = load('WAWebWidFactory');
    const job = load('WAWebMexRevokeNewsletterAdminInviteJob');
    if (!/^\d+@newsletter$/.test(id) || !user || typeof factory?.createWid !== 'function'
        || typeof job?.revokeNewsletterAdminInvite !== 'function') return null;
    await job.revokeNewsletterAdminInvite(id, factory.createWid(user));
    // The page's own list of pending admins, when it has the channel.
    const channel = load('WAWebCollections')?.WAWebNewsletterCollection?.get?.(id);
    channel?.newsletterMetadata?.pendingAdmins?.remove?.(factory.createWid(user));
    return { id, user: request.user };
  }
  if (kind === 'mute') {
    // Both ways: the upstream mute-newsletter route can only mute.
    const id = typeof request.id === 'string' ? request.id : '';
    const mute = root.WPP?.newsletter?.mute;
    if (!/^\d+@newsletter$/.test(id) || typeof request.mute !== 'boolean' || typeof mute !== 'function') return null;
    await mute(id, request.mute);
    return { id, muted: request.mute };
  }
  return null;
}

/** The channel id, the invite code of a link, or '' (the route's input). */
export function channelKey(body: any): string {
  const id = typeof body?.id === 'string' ? body.id.trim() : '';
  if (/^\d+@newsletter$/.test(id)) return id;
  const invite = typeof body?.invite === 'string' ? body.invite.trim() : '';
  const match = invite.match(/(?:whatsapp\.com\/channel\/)?([A-Za-z0-9_-]{10,64})\/?$/);
  return match ? match[1] : '';
}

async function answer(req: Request, res: Response, request: any, code = 'channels_unavailable') {
  try {
    const page: any = (req.client as any)?.page;
    if (!page || page.isClosed?.()) throw new Error(code);
    const result = await page.evaluate(readChannels, request);
    if (result === null || result === undefined) {
      return res.status(503).json({ status: 'error', code });
    }
    return res.status(200).json({ status: 'success', response: result });
  } catch (_) {
    // Never the native error: it can carry channel names and ids. A fixed
    // line, so wppconnect.log shows which read failed.
    (req as any).logger?.warn?.(`channels ${String(request?.kind)} failed`);
    return res.status(503).json({ status: 'error', code });
  }
}

export async function listChannels(req: Request, res: Response) {
  return answer(req, res, { kind: 'list' });
}

export async function channelDirectory(req: Request, res: Response) {
  const body = req.body || {};
  return answer(req, res, { kind: 'directory', query: body.query, limit: body.limit,
    category: body.category, view: body.view });
}

export async function channelInfo(req: Request, res: Response) {
  const key = channelKey(req.body || {});
  if (!key) return res.status(400).json({ status: 'error', code: 'invalid_channel' });
  return answer(req, res, { kind: 'info', key }, 'channel_not_found');
}

export async function followChannel(req: Request, res: Response) {
  const id = channelKey({ id: (req.body || {}).id });
  if (!id) return res.status(400).json({ status: 'error', code: 'invalid_channel' });
  return answer(req, res, { kind: 'follow', id }, 'channel_follow_failed');
}

export async function muteChannel(req: Request, res: Response) {
  const body = req.body || {};
  const id = channelKey({ id: body.id });
  if (!id || typeof body.mute !== 'boolean') return res.status(400).json({ status: 'error', code: 'invalid_channel' });
  return answer(req, res, { kind: 'mute', id, mute: body.mute }, 'channel_mute_failed');
}

export async function pinChannelUpdate(req: Request, res: Response) {
  const body = req.body || {};
  const id = channelKey({ id: body.id });
  if (!id || !Number.isInteger(body.serverId) || body.serverId <= 0 || typeof body.pin !== 'boolean') {
    return res.status(400).json({ status: 'error', code: 'invalid_channel' });
  }
  return answer(req, res, { kind: 'pin', id, serverId: body.serverId, pin: body.pin },
    'channel_pin_failed');
}

export async function channelAdmins(req: Request, res: Response) {
  const id = channelKey({ id: (req.body || {}).id });
  if (!id) return res.status(400).json({ status: 'error', code: 'invalid_channel' });
  return answer(req, res, { kind: 'admins', id }, 'channel_admins_unavailable');
}

export async function inviteChannelAdmin(req: Request, res: Response) {
  const body = req.body || {};
  const id = channelKey({ id: body.id });
  if (!id || typeof body.user !== 'string') return res.status(400).json({ status: 'error', code: 'invalid_channel' });
  return answer(req, res, { kind: 'admin-invite', id, user: body.user, message: body.message },
    'channel_admin_invite_failed');
}

export async function revokeChannelAdminInvite(req: Request, res: Response) {
  const body = req.body || {};
  const id = channelKey({ id: body.id });
  if (!id || typeof body.user !== 'string') return res.status(400).json({ status: 'error', code: 'invalid_channel' });
  return answer(req, res, { kind: 'admin-revoke', id, user: body.user }, 'channel_admin_revoke_failed');
}

export async function unfollowChannel(req: Request, res: Response) {
  const id = channelKey({ id: (req.body || {}).id });
  if (!id) return res.status(400).json({ status: 'error', code: 'invalid_channel' });
  return answer(req, res, { kind: 'unfollow', id }, 'channel_unfollow_failed');
}
