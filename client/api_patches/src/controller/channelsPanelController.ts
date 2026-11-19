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
//   WAWebMexRevokeNewsletterAdminInviteJob (9656078347839416);
// - the owner's people, which WhatsApp Web loads only with a channel's info
//   open, so they are sent by id through WAWebMexClient.fetchQuery as wa-js
//   does: followers 27472091235714801, pending admin invites
//   28049623068067370, demoting an admin 9880997548630971 and changing the
//   owner 9546742745432473.
// The answers go to the client as WhatsApp gives them (XWA2Newsletter nodes):
// the Neonize bridge sends the same queries and answers the same nodes, and the
// client reads them once (client/core/channels.py).
//
// Editing an update this account posted in a channel. wa-js's editMessage()
// builds a chat's protocol edit and sends it down the chat pipeline
// (sendRawMessage), which a channel does not use; WhatsApp Web's own edit,
// WAWebSendMessageEditAction.sendMessageEdit(msg, text, options), sends a
// channel's edit with WAWebNewsletterSendMsgAction.sendNewsletterEditMsg
// (read on 2026-11-08, 2.3000.x). It checks itself whether the update can be
// edited (its own, text or a caption, inside the edit window) and throws
// otherwise; WhatsApp Web's canEditMsg (wa-js's WPP.whatsapp.functions) is
// asked first, so a refusal is known to be one: nothing was sent. Answers
// { id } once sent, { refused: true } when canEditMsg says no, or null when
// the page cannot do it (the update is not held, or the module is missing).
//
// Runs inside the page: self-contained, no closure over anything here.
export async function editChannelUpdate(request: any): Promise<unknown> {
  const root = globalThis as any;
  const id = typeof request?.id === 'string' ? request.id : '';
  const text = typeof request?.text === 'string' ? request.text.trim() : '';
  if (!/^(true|false)_\d+@newsletter_[A-Za-z0-9]+$/.test(id) || !text
      || typeof root.require !== 'function') return null;
  let edit: any = null;
  try { edit = root.require('WAWebSendMessageEditAction')?.sendMessageEdit; } catch (_) { edit = null; }
  const find = root.WPP?.chat?.getMessageById;
  if (typeof edit !== 'function' || typeof find !== 'function') return null;
  const msg = await find(id);
  if (!msg) return null;
  const canEdit = root.WPP?.whatsapp?.functions?.canEditMsg;
  if (typeof canEdit === 'function') {
    let allowed = true;
    try { allowed = !!canEdit(msg); } catch (_) { allowed = true; }
    if (!allowed) return { refused: true };
  }
  await edit(msg, text, {});
  return { id };
}

// A channel's older updates, pulled into WhatsApp Web before a `before` page
// of get-messages is read. They are on WhatsApp's server, not on the phone,
// and the page holds only the newest few: measured on 2026-11-08, 16 of a
// channel's 120, with noEarlierMsgs false, and every `before` page answered
// empty, so scrolling up in a channel stopped at those 16. WhatsApp Web's own
// scroll-up pulls them with WAWebChatLoadMessages.loadEarlierMsgs({ chat }),
// 50 at a time from the server; the same here, until the anchor has
// `targetCount` updates before it or the channel's start is reached. Bounded,
// since the page also serves live traffic. Answers how many were loaded.
//
// Runs inside the page: self-contained, no closure over anything here.
export async function pullChannelHistory(request: any): Promise<number> {
  const root = globalThis as any;
  const chatId = String(request?.chatId || '');
  const anchorId = String(request?.anchorId || '');
  const wanted = Math.max(1, Number(request?.targetCount) || 50);
  if (!/^\d+@newsletter$/.test(chatId) || !anchorId || typeof root.require !== 'function') return 0;
  let loader: any = null;
  let channel: any = null;
  try {
    loader = root.require('WAWebChatLoadMessages');
    channel = root.require('WAWebCollections')?.WAWebNewsletterCollection?.get?.(chatId);
  } catch (_) {
    return 0;
  }
  const msgs = channel?.msgs;
  if (!msgs || typeof loader?.loadEarlierMsgs !== 'function') return 0;
  const start = msgs.length;
  msgs.__winzappOlderIncomplete = false;
  // A hole right below the anchor (updates held from an earlier visit, under
  // newer ones a pull ADDED) is dealt with first, or loadEarlierMsgs, which
  // continues below the collection's oldest update, pages below those old
  // ones and the client keeps the hole for good:
  // - a hole of at most FILL_SPAN ids is filled the way WhatsApp Web's
  //   repairNewsletterMsgHistory does it, without its gate
  //   (WAWebNewsletterGatingUtils.isProactiveGapFillingEnabled(), false on
  //   this account on 2026-11-10): WAWebNewsletterMsgHistoryUtils.
  //   fillMsgHistoryGaps asks the server for the missing server ids, 100 at a
  //   time, and answers the whole list (measured: an 11-update hole filled).
  //   It is handed only the updates down to the first one below the hole, so
  //   older holes never cost requests here. When it cannot run (offline, the
  //   modules missing) the page is marked incomplete and get-messages answers
  //   503: the client tries again later instead of storing the old updates.
  // - a wider hole is not asked id by id (hundreds of requests inside the one
  //   page that serves everything): the old updates below it are dropped from
  //   the collection, so loadEarlierMsgs pages on from the anchor.
  const FILL_SPAN = 500;
  let hidden: any = null;
  try {
    hidden = root.require('WAWebNewsletterViewModeUIUtils')
      ?.getHiddenMessageServerIdsForChat?.(channel.id) || null;
  } catch (_) { hidden = null; }
  const real = (m: any) => {
    const id = Number(m?.serverId);
    return Number.isSafeInteger(id) && id > 0 && id < 1e15 ? id : 0;
  };
  const missingBetween = (low: number, high: number) => {
    const span = high - low - 1;
    if (span <= 0) return 0;
    if (span > (Number(hidden?.size) || 0)) return span;
    let missing = 0;
    for (let id = low + 1; id < high; id++) {
      if (!(hidden && typeof hidden.has === 'function' && hidden.has(id))) missing++;
    }
    return missing;
  };
  const anchor = msgs.getModelsArray().find((m: any) => m?.id?._serialized === anchorId);
  const anchorServerId = real(anchor);
  let dropped = false;
  if (anchorServerId) {
    const below = msgs.getModelsArray().map(real).filter((id: number) => id && id < anchorServerId)
      .sort((a: number, b: number) => b - a);
    let above = anchorServerId;
    let staleTop = 0;
    for (const id of below) {
      if (missingBetween(id, above) > 0) { staleTop = id; break; }
      above = id;
    }
    if (staleTop) {
      const hole = missingBetween(staleTop, above);
      if (hole <= FILL_SPAN) {
        let filled = false;
        try {
          const gaps = root.require('WAWebNewsletterMsgHistoryUtils');
          const toData = root.require('WAWebMsgDataFromModel')?.msgDataFromMsgModel;
          const toModel = root.require('WAWebMsgModelFromData')?.msgModelFromMsgData;
          const jid = root.require('WAWebNewsletterValidationUtils')
            ?.toNewsletterJidOrThrow?.(channel.id.toJid());
          if (typeof gaps?.fillMsgHistoryGaps === 'function' && typeof toData === 'function'
              && typeof toModel === 'function' && jid && typeof msgs.toArray === 'function') {
            const near = msgs.toArray().filter((m: any) => real(m) >= staleTop);
            const whole = await gaps.fillMsgHistoryGaps({
              jid, msgs: near.map(toData), serverIdsToSkip: hidden || new Set() });
            if (Array.isArray(whole)) {
              filled = true;
              if (whole.length > near.length) {
                msgs.add(whole.map(toModel), { silent: true });
                msgs.sort?.({ silent: true });
              }
            }
          }
        } catch (_) {
          filled = false;
        }
        msgs.__winzappOlderIncomplete = !filled;
      } else if (typeof msgs.remove === 'function') {
        const stale = msgs.getModelsArray().filter((m: any) => {
          const id = real(m);
          return id && id <= staleTop;
        });
        msgs.remove(stale, { silent: true });
        dropped = true;
        // The visit that held them may have paged to the channel's start:
        // "no earlier messages" is not true below the anchor any more.
        const state = msgs.msgLoadState;
        if (state && typeof state.set === 'function') state.set({ noEarlierMsgs: false });
        else if (state) state.noEarlierMsgs = false;
      }
    }
  }
  if (msgs.__winzappOlderIncomplete) return msgs.length - start;
  for (let round = 0; round < 20; round++) {
    if (msgs.msgLoadState?.noEarlierMsgs) break;
    const before = msgs.getModelsArray().findIndex((m: any) => m?.id?._serialized === anchorId);
    if (before >= wanted) break;
    const held = msgs.length;
    try {
      await loader.loadEarlierMsgs({ chat: channel });
    } catch (_) {
      break;
    }
    if (msgs.length === held) break;
  }
  if (dropped && anchorServerId
      && !msgs.getModelsArray().some((m: any) => real(m) && real(m) < anchorServerId)) {
    // nothing came below the anchor (offline): an empty page would read as
    // the channel's start, and the store may still hold the old updates
    msgs.__winzappOlderIncomplete = true;
  }
  return msgs.length - start;
}

// Whether the last pullChannelHistory() of *chatId* left a hole right below
// its anchor that it could not fill (offline): get-messages then answers 503
// for that older page, and the client asks again later.
//
// Runs inside the page: self-contained, no closure over anything here.
export function channelOlderPageIncomplete(request: any): boolean {
  const root = globalThis as any;
  const chatId = String(request?.chatId || '');
  if (!/^\d+@newsletter$/.test(chatId) || typeof root.require !== 'function') return false;
  try {
    const channel = root.require('WAWebCollections')?.WAWebNewsletterCollection?.get?.(chatId);
    return !!channel?.msgs?.__winzappOlderIncomplete;
  } catch (_) {
    return false;
  }
}

// A channel's newest updates, for get-messages' first page. The plain first
// page (WAPI.getMessages without an anchor) answers what the page already
// holds, before the chat's lastReceivedKey: a channel never opened on this
// WhatsApp Web held 2 messages and The New York Times showed "no updates",
// and the updates an admin posts are never "received", so a channel's own
// new posts were left out (measured 2026-11-09). WhatsApp Web opens a
// channel with WAWebNewsletterPullMessagesFromServerAction.
// pullNewsletterMessagesFromServer(chat, { messageCount, cursor }), which reads
// the newest updates from WhatsApp's server into the channel's collection; the
// same here (the same channel: 20 asked, 20 updates, the newest of that
// day). The server answers at most 100 at a time (WAWebNewsletterGatingUtils.
// getMaxMsgCountFromServer()) and WinZapp asks a whole page, 200 by default
// (Settings, messages_page_size): the rest come from older pages, as a scroll
// up loads them (WAWebChatLoadMessages.loadEarlierMsgs, see
// pullChannelHistory), until the page is full or the channel starts. Then the
// newest `targetCount` of the collection, oldest first, as WAPI serializes
// them. A channel the page does not hold yet (one not followed) is found or
// created the way WhatsApp Web previews it. Unread counts are left alone.
// Answers null when the page cannot do it (get-messages then reads as before).
//
// Runs inside the page: self-contained, no closure over anything here.
export async function readNewestChannelUpdates(request: any): Promise<unknown[] | null> {
  const root = globalThis as any;
  const chatId = String(request?.chatId || '');
  const wanted = Math.max(1, Math.min(500, Number(request?.targetCount) || 20));
  const serialize = root.WAPI?._serializeMessageObj;
  if (!/^\d+@newsletter$/.test(chatId) || typeof root.require !== 'function'
      || typeof serialize !== 'function') return null;
  let channel: any = null;
  let pull: any = null;
  let loader: any = null;
  try {
    const collection = root.require('WAWebCollections')?.WAWebNewsletterCollection;
    channel = collection?.get?.(chatId);
    if (!channel) {
      const wid = root.require('WAWebWidFactory').createWid(chatId);
      const found = await root.require('WAWebFindChatAction').findOrCreateLatestChat(wid, 'createChat');
      channel = collection?.get?.(chatId) || found?.chat;
    }
    pull = root.require('WAWebNewsletterPullMessagesFromServerAction')?.pullNewsletterMessagesFromServer;
    loader = root.require('WAWebChatLoadMessages');
  } catch (_) {
    return null;
  }
  const msgs = channel?.msgs;
  if (!msgs || typeof msgs.getModelsArray !== 'function') return null;
  if (typeof pull === 'function') {
    try {
      await pull(channel, { messageCount: Math.min(wanted, 100), cursor: null, resetUnreadCount: false });
    } catch (_) {
      // offline or refused: what the page holds
    }
  }
  // Bounded, since the page also serves live traffic: 10 rounds of 50.
  for (let round = 0; round < 10 && typeof loader?.loadEarlierMsgs === 'function'; round++) {
    if (msgs.length >= wanted || msgs.msgLoadState?.noEarlierMsgs) break;
    const held = msgs.length;
    try {
      await loader.loadEarlierMsgs({ chat: channel });
    } catch (_) {
      break;
    }
    if (msgs.length === held) break;
  }
  // Holes between the updates the collection holds are filled the way
  // WhatsApp Web's repairNewsletterMsgHistory does it, without its gate
  // (WAWebNewsletterGatingUtils.isProactiveGapFillingEnabled(), false on this
  // account on 2026-11-10): WAWebNewsletterMsgHistoryUtils.fillMsgHistoryGaps
  // asks the server for the missing server ids and answers the whole list
  // (measured: an 11-update hole filled). A pull ADDS to the collection, so
  // updates held from an earlier visit stay below the newest ones with a hole
  // between them, and loadEarlierMsgs continues below those.
  try {
    const gaps = root.require('WAWebNewsletterMsgHistoryUtils');
    const toData = root.require('WAWebMsgDataFromModel')?.msgDataFromMsgModel;
    const toModel = root.require('WAWebMsgModelFromData')?.msgModelFromMsgData;
    const jid = root.require('WAWebNewsletterValidationUtils')
      ?.toNewsletterJidOrThrow?.(channel.id.toJid());
    let skip: any = new Set();
    try {
      skip = root.require('WAWebNewsletterViewModeUIUtils')
        ?.getHiddenMessageServerIdsForChat?.(channel.id) || skip;
    } catch (_) { /* none hidden */ }
    if (typeof gaps?.fillMsgHistoryGaps === 'function' && typeof toData === 'function'
        && typeof toModel === 'function' && jid && typeof msgs.toArray === 'function') {
      const whole = await gaps.fillMsgHistoryGaps({
        jid, msgs: msgs.toArray().map(toData), serverIdsToSkip: skip });
      if (Array.isArray(whole) && whole.length > msgs.length) {
        msgs.add(whole.map(toModel), { silent: true });
        msgs.sort?.({ silent: true });
      }
    }
  } catch (_) {
    // offline, or the page lacks the modules: the holes stay
  }
  const held = msgs.getModelsArray().slice()
    .sort((a: any, b: any) => (Number(a?.t) || 0) - (Number(b?.t) || 0));
  // Only the run that ends at the newest update with no hole in its server
  // ids (what the fill could not close, offline say): the client pages back
  // from the oldest update it gets, and a page that skipped a hole would
  // leave it there for good. Server ids are dense (WhatsApp Web's own
  // identifyMsgGaps counts any jump as a hole; a deleted update keeps its
  // id); hidden ones are skipped, and an update without a server id (an own
  // post still sending) never breaks the run. Walked in server id order:
  // two updates of the same second may come in either order by date.
  let hidden: any = null;
  try {
    hidden = root.require('WAWebNewsletterViewModeUIUtils')
      ?.getHiddenMessageServerIdsForChat?.(channel.id) || null;
  } catch (_) { hidden = null; }
  const real = (m: any) => {
    const id = Number(m?.serverId);
    return Number.isSafeInteger(id) && id > 0 && id < 1e15 ? id : 0;
  };
  const ids = held.map(real).filter((id: number) => id).sort((a: number, b: number) => b - a);
  let floor = 0;
  for (let k = 1; k < ids.length; k++) {
    const above = ids[k - 1];
    const id = ids[k];
    if (above - id <= 1) continue;
    // more missing ids than hidden ones: a hole without counting them
    let missing = above - id - 1 > (Number(hidden?.size) || 0);
    for (let gap = id + 1; gap < above && !missing; gap++) {
      missing = !(hidden && typeof hidden.has === 'function' && hidden.has(gap));
    }
    if (missing) { floor = above; break; }
  }
  const run = floor ? held.filter((m: any) => !real(m) || real(m) >= floor) : held;
  return run.slice(-wanted).map((m: any) => serialize(m)).filter(Boolean);
}

// Runs inside the page: self-contained, no closure over anything here.
export async function readChannels(request: any): Promise<unknown> {
  const root = globalThis as any;
  const load = (name: string): any => {
    try { return typeof root.require === 'function' ? root.require(name) : null; } catch (_) { return null; }
  };
  // The channel queries do not carry "Show admin profile"; the page's own
  // channel metadata does (adminProfilesSettingEnabled): added to the node as
  // thread_metadata.settings.admin_profiles_enabled for the client, and only
  // when WhatsApp offers the setting to this account
  // (WAWebNewsletterExtendedGatingUtils.isNewsletterAdminProfilesSettingEnabled,
  // false on 2026-11-09, when the server answered the setting Bad Request).
  const withPageSettings = (node: any) => {
    try {
      const meta = load('WAWebCollections')?.WAWebNewsletterCollection?.get?.(node?.id)?.newsletterMetadata;
      const offered = load('WAWebNewsletterExtendedGatingUtils')?.isNewsletterAdminProfilesSettingEnabled?.(meta);
      const enabled = meta?.adminProfilesSettingEnabled;
      if (offered === true && typeof enabled === 'boolean' && node?.thread_metadata) {
        node.thread_metadata.settings = { ...(node.thread_metadata.settings || {}), admin_profiles_enabled: enabled };
      }
    } catch (_) {
      // the node as it came
    }
    return node;
  };
  const kind = request?.kind;
  if (kind === 'list') {
    const job = load('WAWebMexFetchAllNewslettersMetadataJob');
    if (typeof job?.mexFetchAllNewsletters !== 'function') return null;
    const data = await job.mexFetchAllNewsletters({ fetchWamoSub: false, fetchStatusMetadata: false });
    const list = data?.xwa2_newsletter_subscribed;
    return Array.isArray(list) ? list.map(withPageSettings) : null;
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
    // The lists are per country, as in WhatsApp Web: a view or a category
    // with no country is answered "Bad Request" (measured 2026-11-08: the
    // same list read 20 channels with ['BR'] and failed with []).
    let country = '';
    try {
      const own = await load('WAWebCountryCodeUtils')?.getMyCountryCode?.();
      if (typeof own === 'string' && /^[A-Z]{2}$/.test(own)) country = own;
    } catch (_) { country = ''; }
    const countryCodes = country ? [country] : [];
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
        view: listView, categories, countryCodes, limit, cursorToken: null });
      const result = data?.xwa2_newsletters_directory_list?.result;
      return Array.isArray(result) ? result : null;
    }
    const job = load('WAWebMexFetchRecommendedNewslettersJob');
    if (typeof job?.mexFetchRecommendedNewsletters !== 'function') return null;
    const data = await job.mexFetchRecommendedNewsletters(limit, countryCodes);
    const result = data?.xwa2_newsletters_recommended?.result;
    return Array.isArray(result) ? result : null;
  }
  if (kind === 'info') {
    const key = typeof request.key === 'string' ? request.key.trim() : '';
    if (!/^(?:\d+@newsletter|[A-Za-z0-9_-]{10,64})$/.test(key)) return null;
    const job = load('WAWebMexFetchNewsletterJob');
    if (typeof job?.mexGetNewsletter !== 'function') return null;
    // A channel this account does not follow refuses the viewer's metadata
    // ("WAWebMexFetchNewsletterJobQuery: Not Allowed", measured 2026-11-11 on
    // a recommended channel; the same query without it answered): read it
    // again without, and the channel is the guest's view of it.
    const read = (viewer: boolean) => job.mexGetNewsletter(key, 'GUEST', {
      fetchViewerMetadata: viewer, fetchCreationTime: true, fetchWamoSub: false,
      fetchStatusMetadata: false });
    let data: any;
    try {
      data = await read(true);
    } catch (e: any) {
      if (!/not allowed/i.test(String(e?.message || e))) throw e;
      data = await read(false);
    }
    return data?.xwa2_newsletter ? withPageSettings(data.xwa2_newsletter) : null;
  }
  if (kind === 'follow' || kind === 'unfollow') {
    const id = typeof request.id === 'string' ? request.id : '';
    const action = root.WPP?.newsletter?.[kind];
    if (!/^\d+@newsletter$/.test(id) || typeof action !== 'function') return null;
    await action(id);
    return { id };
  }
  if (kind === 'admin-profiles') {
    // WhatsApp Web's "Show admin profile" (channel info, read 2026-11-09):
    // WAWebMexUpdateNewsletterAdminProfileSettingJob, mutation 28226671310350649
    // with settings.admin_profiles_enabled. The job is loaded only with the
    // channel's info open, and the Bootloader could not load it on 2026-11-10
    // (15 s, never answered): the mutation goes by id through WAWebMexClient
    // then, with the variables the Neonize bridge sends (channel_reactions.py).
    const id = typeof request.id === 'string' ? request.id : '';
    if (!/^\d+@newsletter$/.test(id) || typeof request.enabled !== 'boolean') return null;
    const job = load('WAWebMexUpdateNewsletterAdminProfileSettingJob');
    const client = load('WAWebMexClient');
    if (typeof job?.mexUpdateNewsletterAdminProfileSetting === 'function') {
      await job.mexUpdateNewsletterAdminProfileSetting({ newsletterID: id, adminProfilesEnabled: request.enabled });
    } else if (typeof client?.fetchQuery === 'function') {
      await client.fetchQuery({ kind: 'Request', params: { id: '28226671310350649', metadata: {},
        name: 'WAWebMexUpdateNewsletterAdminProfileSettingJobMutation', operationKind: 'mutation', text: null } },
      { newsletter_id: id, updates: { settings: { admin_profiles_enabled: request.enabled } } });
    } else {
      return null;
    }
    const meta = load('WAWebCollections')?.WAWebNewsletterCollection?.get?.(id)?.newsletterMetadata;
    if (meta && typeof meta.set === 'function') meta.set({ adminProfilesSettingEnabled: request.enabled });
    return { id, enabled: request.enabled };
  }
  if (kind === 'insights') {
    // A channel's statistics for its admins: the MEX query of WhatsApp Web's
    // WAWebMexFetchNewsletterInsightsJob (id 9853618868050977, variables
    // {input: {newsletter_id, metrics}}), with the metrics the client sends
    // (core/channel_insights.py), sent by id through WAWebMexClient as the
    // followers' queries are and as the Neonize bridge sends it. Its answer,
    // xwa2_newsletter_admin_insights, goes to the client as it comes. The job
    // itself is loaded only with a channel's info open, and on 2026-11-10 the
    // Bootloader could not load it at all (15 s, never answered): every read
    // said "unavailable". Measured the same day: the query by id reaches the
    // server, which answers Bad Request for a channel under 100 followers.
    const id = typeof request.id === 'string' ? request.id : '';
    const metrics = Array.isArray(request.metrics)
      ? request.metrics.filter((m: any) => m && typeof m === 'object' && m.id && m.type).slice(0, 20)
      : [];
    const client = load('WAWebMexClient');
    if (!/^\d+@newsletter$/.test(id) || !metrics.length || typeof client?.fetchQuery !== 'function') {
      return null;
    }
    const data = await client.fetchQuery({ kind: 'Request', params: { id: '9853618868050977', metadata: {},
      name: 'WAWebMexFetchNewsletterInsightsJobQuery', operationKind: 'query', text: null } },
    { input: { newsletter_id: id, metrics } });
    const node = data?.xwa2_newsletter_admin_insights;
    return node && typeof node === 'object' ? node : null;
  }
  if (kind === 'reactions') {
    // Who may react to the channel's updates: WAWebMexUpdateNewsletterJob with
    // only reactionCodesSetting, as WhatsApp Web's channel settings send it
    // (editNewsletterMetadataQuery, editReactionCodesSetting), with its enum
    // NewsletterReactionCodesSetting (All 0, Basic 1, None 3; read 2026-11-09).
    // The job, not the UI's editNewsletterMetadataAction, which logs a refusal
    // and returns as if it had worked. The channel's metadata notification
    // updates the page.
    const id = typeof request.id === 'string' ? request.id : '';
    const names: any = { ALL: 'All', BASIC: 'Basic', NONE: 'None' };
    const setting = load('WAWebCommonNewsletterEnums')?.NewsletterReactionCodesSetting?.[
      names[String(request.value || '').toUpperCase()]];
    const job = load('WAWebMexUpdateNewsletterJob')?.mexUpdateNewsletter;
    if (!/^\d+@newsletter$/.test(id) || setting == null || typeof job !== 'function') return null;
    await job({ newsletterID: id, reactionCodesSetting: setting });
    return { id, value: String(request.value).toUpperCase() };
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
    // The action keeps the expiration to itself; the invite it just added to
    // the person's chat carries it.
    const sent = (chat.msgs?.getModelsArray?.() ?? []).filter((m: any) =>
      m?.type === 'newsletter_admin_invite'
      && String(m?.newsletterAdminInviteInfo?.newsletterId?.toString?.() ?? '') === id).pop();
    const expiration = Number(sent?.newsletterAdminInviteInfo?.inviteExpiration);
    return { id, user: request.user,
      expiration: Number.isInteger(expiration) && expiration > 0 ? expiration : 0 };
  }
  const mex = load('WAWebMexClient');
  const mexRequest = (queryId: string, name: string, operationKind: string) => ({
    kind: 'Request', params: { id: queryId, metadata: {}, name, operationKind, text: null } });
  if (kind === 'followers') {
    const id = typeof request.id === 'string' ? request.id : '';
    if (!/^\d+@newsletter$/.test(id) || typeof mex?.fetchQuery !== 'function') return null;
    const data = await mex.fetchQuery(mexRequest('27472091235714801',
      'WAWebMexFetchNewsletterFollowersJobQuery', 'query'), { input: { newsletter_id: id, count: 500 } });
    const edges = data?.xwa2_newsletter_followers?.followers?.edges;
    if (data?.xwa2_newsletter_followers == null) return null;
    const followers = (Array.isArray(edges) ? edges : []).map((edge: any) => {
      const user = person(edge?.node?.id);
      const role = String(edge?.role ?? '').toUpperCase();
      return user ? { id: user.replace('@c.us', '@s.whatsapp.net'),
        phone: person(edge?.node?.pn).replace('@c.us', '@s.whatsapp.net'),
        name: String(edge?.node?.display_name ?? '').trim().slice(0, 100),
        role: ['OWNER', 'ADMIN', 'SUBSCRIBER'].includes(role) ? role.toLowerCase() : 'subscriber' } : null;
    }).filter(Boolean);
    const order: any = { owner: 0, admin: 1 };
    followers.sort((a: any, b: any) => (order[a.role] ?? 2) - (order[b.role] ?? 2));
    return { id, followers };
  }
  if (kind === 'admin-pending') {
    const id = typeof request.id === 'string' ? request.id : '';
    if (!/^\d+@newsletter$/.test(id) || typeof mex?.fetchQuery !== 'function') return null;
    const data = await mex.fetchQuery(mexRequest('28049623068067370',
      'WAWebMexFetchNewsletterPendingInvitesJobQuery', 'query'), { newsletter_id: id });
    const invites = data?.xwa2_newsletter_admin?.pending_admin_invites;
    if (!Array.isArray(invites)) return null;
    const pending: string[] = [];
    for (const invite of invites) {
      const user = person(invite?.user?.id);
      if (user && !pending.includes(user)) pending.push(user);
    }
    return { id, pending };
  }
  if (kind === 'admin-demote' || kind === 'owner-change') {
    const id = typeof request.id === 'string' ? request.id : '';
    const user = person(request.user);
    const factory = load('WAWebWidFactory');
    const lid = load('WAWebLidMigrationUtils')?.toUserLidOrThrow;
    if (!/^\d+@newsletter$/.test(id) || !user || typeof mex?.fetchQuery !== 'function'
        || typeof factory?.createWid !== 'function' || typeof lid !== 'function') return null;
    const [queryId, name] = kind === 'admin-demote'
      ? ['9880997548630971', 'WAWebMexDemoteNewsletterAdminJobMutation']
      : ['9546742745432473', 'WAWebMexChangeNewsletterOwnerJobMutation'];
    await mex.fetchQuery(mexRequest(queryId, name, 'mutation'),
      { newsletter_id: id, user_id: lid(factory.createWid(user)).toString() });
    return { id, user: request.user };
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

export async function setChannelAdminProfiles(req: Request, res: Response) {
  const body = req.body || {};
  const id = channelKey({ id: body.id });
  if (!id || typeof body.enabled !== 'boolean') {
    return res.status(400).json({ status: 'error', code: 'invalid_channel' });
  }
  return answer(req, res, { kind: 'admin-profiles', id, enabled: body.enabled },
    'channel_admin_profiles_failed');
}

export async function channelInsights(req: Request, res: Response) {
  const body = req.body || {};
  const id = channelKey({ id: body.id });
  if (!id || !Array.isArray(body.metrics) || !body.metrics.length) {
    return res.status(400).json({ status: 'error', code: 'invalid_channel' });
  }
  return answer(req, res, { kind: 'insights', id, metrics: body.metrics }, 'channel_insights_unavailable');
}

export async function setChannelReactions(req: Request, res: Response) {
  const body = req.body || {};
  const id = channelKey({ id: body.id });
  const value = String(body.value || '').toUpperCase();
  if (!id || !['ALL', 'BASIC', 'NONE'].includes(value)) {
    return res.status(400).json({ status: 'error', code: 'invalid_channel' });
  }
  return answer(req, res, { kind: 'reactions', id, value }, 'channel_reactions_failed');
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

export async function channelFollowers(req: Request, res: Response) {
  const id = channelKey({ id: (req.body || {}).id });
  if (!id) return res.status(400).json({ status: 'error', code: 'invalid_channel' });
  return answer(req, res, { kind: 'followers', id }, 'channel_followers_unavailable');
}

export async function channelPendingAdmins(req: Request, res: Response) {
  const id = channelKey({ id: (req.body || {}).id });
  if (!id) return res.status(400).json({ status: 'error', code: 'invalid_channel' });
  return answer(req, res, { kind: 'admin-pending', id }, 'channel_admins_unavailable');
}

export async function demoteChannelAdmin(req: Request, res: Response) {
  const body = req.body || {};
  const id = channelKey({ id: body.id });
  if (!id || typeof body.user !== 'string') return res.status(400).json({ status: 'error', code: 'invalid_channel' });
  return answer(req, res, { kind: 'admin-demote', id, user: body.user }, 'channel_admin_demote_failed');
}

export async function changeChannelOwner(req: Request, res: Response) {
  const body = req.body || {};
  const id = channelKey({ id: body.id });
  if (!id || typeof body.user !== 'string') return res.status(400).json({ status: 'error', code: 'invalid_channel' });
  return answer(req, res, { kind: 'owner-change', id, user: body.user }, 'channel_owner_change_failed');
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
