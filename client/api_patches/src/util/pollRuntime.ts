// Each exported function must be fully self-contained: Puppeteer serializes
// only the function's own text into the WhatsApp page.
// JavaScript syntax also lets Node tests run the exact browser functions.
//
// Read from WhatsApp Web's own source over CDP (WinZapp's HeadlessChrome,
// 2026-10-11), not guessed:
//  - a poll is a Msg with type 'poll_creation'; pollOptions is [{name, localId}];
//  - votes live in WAWebPollsPollVoteCollection.PollVoteCollection; the votes
//    of one poll are PollVoteCollection.getForParentAddressingModeInsensitive([msg.id])[0]
//    (a group with toArray()), each vote having selectedOptionLocalIds (empty
//    = the voter cleared the vote);
//  - WAWebPollsSendVoteMsgAction.sendVote(pollMsg, Set<localId>) casts or
//    changes the current user's vote; an empty Set clears it.
// readPollVotes returns only counts and option names. readPollVoters is the one
// that returns who voted what, for the "View votes" window; it is only called
// when the user asks for it, and only sender ids and option ids leave the page.

export async function readPollVotes({ messageIds }) {
  // Puppeteer sends only this function's own text to the page, so the helpers
  // live inside it (a module-level helper would be undefined there).
  const loadModule = (name) => {
    const loader = globalThis['WPP']?.loader;
    if (typeof loader?.loadModule === 'function') return loader.loadModule(name);
    if (typeof loader?.moduleRequire === 'function') return loader.moduleRequire(name);
    return undefined;
  };
  const pollMessage = async (id) => {
    const msg = await globalThis['WPP']?.chat?.getMessageById?.(id);
    return msg && msg.type === 'poll_creation' ? msg : undefined;
  };
  const collection = loadModule('WAWebPollsPollVoteCollection')?.PollVoteCollection;
  if (typeof collection?.getForParentAddressingModeInsensitive !== 'function') {
    throw new Error('poll_votes_unavailable');
  }
  const meUser = loadModule('WAWebUserPrefsMeUser');
  const hydrate = loadModule('WAWebAddonHydrationUtils')?.hydrateAddons;
  const hydrationType = loadModule('WAWebMsgType')?.MSG_TYPE?.POLL_UPDATE;
  const result = {};
  for (const messageId of messageIds) {
    const msg = await pollMessage(messageId);
    if (!msg) continue;
    if (typeof hydrate === 'function' && hydrationType !== undefined) {
      try { await hydrate({ ids: [msg.id], hydrationType }); } catch { /* best effort */ }
    }
    const group = collection.getForParentAddressingModeInsensitive([msg.id])?.[0];
    const votes = group?.toArray?.() ?? [];
    let mine = new Set();
    try {
      const me = meUser?.getMeLidUserOrThrow?.();
      const mineVote = me ? group?.getVoteFromSenderAddressingModeInsensitive?.(me) : undefined;
      mine = new Set(mineVote?.selectedOptionLocalIds ?? []);
    } catch { /* not knowing my vote must not hide the counts */ }
    const counts = new Map();
    let voters = 0;
    for (const vote of votes) {
      const ids = vote?.selectedOptionLocalIds ?? [];
      if (!ids.length) continue;
      voters += 1;
      for (const id of ids) counts.set(id, (counts.get(id) ?? 0) + 1);
    }
    result[messageId] = {
      voters,
      options: (msg.pollOptions ?? []).map((option) => ({
        name: option.name,
        localId: option.localId,
        votes: counts.get(option.localId) ?? 0,
        mine: mine.has(option.localId),
      })),
    };
  }
  return result;
}

export async function readPollVoters({ messageId }) {
  // Puppeteer sends only this function's own text to the page, so the helpers
  // live inside it (a module-level helper would be undefined there).
  const loadModule = (name) => {
    const loader = globalThis['WPP']?.loader;
    if (typeof loader?.loadModule === 'function') return loader.loadModule(name);
    if (typeof loader?.moduleRequire === 'function') return loader.moduleRequire(name);
    return undefined;
  };
  const idOf = (value) => {
    if (!value) return '';
    if (typeof value === 'string') return value;
    return String(value._serialized ?? (typeof value.toString === 'function' ? value.toString() : ''));
  };
  try {
    const msg = await globalThis['WPP']?.chat?.getMessageById?.(messageId);
    const collection = loadModule('WAWebPollsPollVoteCollection')?.PollVoteCollection;
    if (!msg || msg.type !== 'poll_creation'
        || typeof collection?.getForParentAddressingModeInsensitive !== 'function') {
      return { ok: false, error: 'poll_voters_unavailable' };
    }
    const hydrate = loadModule('WAWebAddonHydrationUtils')?.hydrateAddons;
    const hydrationType = loadModule('WAWebMsgType')?.MSG_TYPE?.POLL_UPDATE;
    if (typeof hydrate === 'function' && hydrationType !== undefined) {
      try { await hydrate({ ids: [msg.id], hydrationType }); } catch { /* best effort */ }
    }
    let me = '';
    try { me = idOf(loadModule('WAWebUserPrefsMeUser')?.getMeLidUserOrThrow?.()); } catch { /* unknown */ }
    const group = collection.getForParentAddressingModeInsensitive([msg.id])?.[0];
    const meUser = loadModule('WAWebUserPrefsMeUser');
    const contacts = loadModule('WAWebContactCollection')?.ContactCollection;
    const voters = new Array();
    for (const vote of group?.toArray?.() ?? []) {
      const localIds = (vote?.selectedOptionLocalIds ?? []).filter((id) => Number.isInteger(id));
      if (!localIds.length) continue;
      const sender = idOf(vote.sender ?? vote.senderUserJid ?? vote.from);
      if (!sender) continue;
      // Measured over CDP on a group poll: the vote's `sender` is the voter's
      // @lid Wid; WhatsApp Web's ContactCollection has a name (address book)
      // and/or a pushname for each, so it names voters the client cannot.
      let mine = !!me && sender === me;
      try { if (typeof meUser?.isMeAccount === 'function') mine = meUser.isMeAccount(vote.sender) === true; } catch { /* keep equality */ }
      let name = '';
      try {
        const contact = contacts?.get?.(vote.sender);
        name = String(contact?.name || contact?.pushname || '').trim();
      } catch { /* the client names them */ }
      voters.push({ sender, mine, name, localIds });
    }
    return {
      ok: true,
      hideVoterNames: msg.pollHideVoterNames === true,
      options: (msg.pollOptions ?? []).map((option) => ({ name: option.name, localId: option.localId })),
      voters,
    };
  } catch {
    return { ok: false, error: 'poll_voters_unavailable' };
  }
}

export async function writePollVote({ messageId, localIds }) {
  // Puppeteer sends only this function's own text to the page, so the helpers
  // live inside it (a module-level helper would be undefined there).
  const loadModule = (name) => {
    const loader = globalThis['WPP']?.loader;
    if (typeof loader?.loadModule === 'function') return loader.loadModule(name);
    if (typeof loader?.moduleRequire === 'function') return loader.moduleRequire(name);
    return undefined;
  };
  const pollMessage = async (id) => {
    const msg = await globalThis['WPP']?.chat?.getMessageById?.(id);
    return msg && msg.type === 'poll_creation' ? msg : undefined;
  };
  try {
    if (typeof messageId !== 'string' || !messageId || !Array.isArray(localIds)
        || !localIds.every((id) => Number.isInteger(id) && id >= 0)) {
      return { ok: false, error: 'poll_vote_invalid' };
    }
    const sendVote = loadModule('WAWebPollsSendVoteMsgAction')?.sendVote;
    const msg = await pollMessage(messageId);
    if (typeof sendVote !== 'function' || !msg) {
      return { ok: false, error: 'poll_vote_unavailable' };
    }
    const valid = new Set((msg.pollOptions ?? []).map((option) => option.localId));
    if (!localIds.every((id) => valid.has(id))) {
      return { ok: false, error: 'poll_vote_invalid' };
    }
    const limit = Number(msg.pollSelectableOptionsCount) || 0;
    if (limit > 0 && localIds.length > limit) {
      return { ok: false, error: 'poll_vote_invalid' };
    }
    await sendVote(msg, new Set(localIds));
    return { ok: true };
  } catch {
    // Native errors may carry contact names, message content and identifiers.
    return { ok: false, error: 'poll_vote_failed' };
  }
}

// WPP.chat.sendCreatePollMessage takes no end time and no "hide voter names".
// WhatsApp Web, read over CDP on 2026-11-18, builds the poll's protobuf from the
// message model's `pollEndTime` (absolute, milliseconds) and `pollHideVoterNames`
// (WAWebPollsGeneratePollCreationMessageProto: either one makes it a
// pollCreationMessageV6). wa-js' sendRawMessage keeps every field of the model
// it is given, so the poll is built here as sendCreatePollMessage builds it and
// the two fields are added (the time limit as `pollEndTime`). Answers like the library's sendPollMessage: the
// sent message, read back with WAPI.getMessageById.
export async function sendPollWithSettings({
  chatId, name, choices, selectableCount, hideVoterNames, durationSeconds,
}) {
  const wpp = globalThis['WPP'];
  const random = (size) => globalThis['self'].crypto.getRandomValues(new Uint8Array(size));
  // Only what was asked for becomes a field: WhatsApp Web treats the presence
  // of either one as "this poll is a V6". The time limit arrives as a length
  // and becomes WhatsApp's absolute end time here, when the poll is sent.
  const hasLimit = Number.isFinite(durationSeconds) && durationSeconds > 0;
  const poll = {
    type: 'poll_creation',
    pollName: name,
    pollOptions: choices.map((choice, localId) => ({ name: choice, localId })),
    pollEncKey: random(32),
    pollSelectableOptionsCount: selectableCount || 0,
    messageSecret: random(32),
    ...(hideVoterNames === true ? { pollHideVoterNames: true } : {}),
    ...(hasLimit ? { pollEndTime: Date.now() + Math.floor(durationSeconds) * 1000 } : {}),
  };
  const sent = await wpp.chat.sendRawMessage(chatId, poll, {});
  const message = await globalThis['WAPI'].getMessageById(sent.id);
  return JSON.parse(JSON.stringify(message));
}
