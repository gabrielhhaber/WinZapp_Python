// WhatsApp events (a chat's "Event": name, date, place, call link, who goes)
// inside the page. Each exported function must be fully self-contained:
// Puppeteer serializes only the function's own text into the WhatsApp page.
//
// Read from WhatsApp Web's own source over CDP (WinZapp's HeadlessChrome,
// 2026-11-11, 2.3000.x, wa-js 4.7.2), not guessed:
//  - an event is a Msg with type 'event_creation': eventName,
//    eventDescription, eventStartTime, eventEndTime (seconds), eventLocation
//    (a string, or {name, degreesLatitude, degreesLongitude}), eventJoinLink
//    (a call link, '/video/' in it for a video call), isEventCanceled,
//    eventExtraGuestsAllowed, eventIsScheduledCall, messageSecret;
//  - an edit (or a cancel: isEventCanceled) changes that same Msg in place;
//  - the answers live in WAWebEventResponseCollection.EventResponseCollection,
//    byParent(WAWebReferentialMsgKey.getReferentialMsgKey(event)): each has
//    sender and eventResponse (1 going, 2 not going, 3 maybe; the
//    protobuf's Message$EventResponseMessage$EventResponseType);
//  - WAWebSendEventResponseMsgAction.sendEventResponseMsg(type,
//    WAWebFrontendMsgGetters.getAsEventCreation(msg)) answers;
//  - WAWebSendEventCreationMsgAction.sendEventCreationMessage({chat,
//    eventCreationOpts}) creates and WAWebSendEventEditMsgAction.
//    sendEventEditMessage(fields, getAsEventCreation(msg)) edits or cancels.
//    Both live in the lazy bundle of WhatsApp Web's "create event" window
//    ("WAWebEventsCreateEventModalFlow.react"), loaded here without opening it.

export async function readEventResponses({ messageIds }: { messageIds: string[] }) {
  const root = globalThis as any;
  const req = (name: string): any => { try { return root.require(name); } catch (_) { return undefined; } };
  const hydrate = req('WAWebAddonHydrationUtils')?.hydrateAddons;
  const type = req('WAWebMsgType')?.MSG_TYPE?.EVENT_RESPONSE;
  const collection = req('WAWebEventResponseCollection')?.EventResponseCollection;
  const asEvent = req('WAWebFrontendMsgGetters')?.getAsEventCreation;
  const referential = req('WAWebReferentialMsgKey')?.getReferentialMsgKey;
  const meUser = req('WAWebUserPrefsMeUser');
  if (typeof collection?.byParent !== 'function' || typeof asEvent !== 'function'
      || typeof referential !== 'function') {
    throw new Error('event_responses_unavailable');
  }
  const out: any = {};
  for (const messageId of messageIds) {
    let msg: any;
    try { msg = await root.WPP?.chat?.getMessageById?.(messageId); } catch (_) { msg = undefined; }
    if (!msg || msg.type !== 'event_creation') continue;
    if (typeof hydrate === 'function' && type !== undefined) {
      try { await hydrate({ ids: [msg.id], hydrationType: type }); } catch (_) { /* best effort */ }
    }
    const group = collection.byParent(referential(asEvent(msg)));
    const list = group?.toArray?.() ?? Array.from(group ?? []);
    const responses: any[] = [];
    let mine = 0;
    for (const r of list) {
      const response = Number(r?.eventResponse) || 0;
      if (!response) continue;
      let isMe = false;
      try { isMe = !!meUser?.isMeAccount?.(r.sender); } catch (_) { isMe = false; }
      if (isMe) mine = response;
      responses.push({
        sender: String(r?.sender?._serialized || r?.sender || ''),
        response,
        extraGuestCount: Number(r?.extraGuestCount) || 0,
        isMe,
      });
    }
    out[messageId] = { responses, mine };
  }
  return out;
}

export async function writeEventResponse({ messageId, response }: { messageId: string; response: number }) {
  const root = globalThis as any;
  const req = (name: string): any => { try { return root.require(name); } catch (_) { return undefined; } };
  const send = req('WAWebSendEventResponseMsgAction')?.sendEventResponseMsg;
  const asEvent = req('WAWebFrontendMsgGetters')?.getAsEventCreation;
  if (typeof send !== 'function' || typeof asEvent !== 'function') {
    return { ok: false, error: 'event_response_unavailable' };
  }
  if (![1, 2, 3].includes(response)) return { ok: false, error: 'event_response_invalid' };
  let msg: any;
  try { msg = await root.WPP?.chat?.getMessageById?.(messageId); } catch (_) { msg = undefined; }
  if (!msg || msg.type !== 'event_creation') return { ok: false, error: 'event_not_found' };
  if (msg.isEventCanceled || msg.eventInvalidated) return { ok: false, error: 'event_canceled' };
  await send(response, asEvent(msg));
  return { ok: true };
}

export async function createEvent(request: any) {
  const root = globalThis as any;
  const req = (name: string): any => { try { return root.require(name); } catch (_) { return undefined; } };
  const chatId = String(request?.chatId || '');
  const name = String(request?.name || '').trim();
  const startTime = Number(request?.startTime) || 0;
  const endTime = Number(request?.endTime) || undefined;
  const callType = request?.callType === 'voice' || request?.callType === 'video'
    ? request.callType : undefined;
  if (!chatId || !name || !startTime) return { ok: false, error: 'event_invalid' };
  try {
    await req('JSResourceForInteraction')('WAWebEventsCreateEventModalFlow.react')
      .__setRef('WinZapp').load();
  } catch (_) { /* the module may already be there */ }
  const send = req('WAWebSendEventCreationMsgAction')?.sendEventCreationMessage;
  if (typeof send !== 'function') return { ok: false, error: 'event_create_unavailable' };
  const chat = await root.WPP?.chat?.find?.(chatId);
  if (!chat) return { ok: false, error: 'chat_not_found' };
  const before = new Set(chat.msgs.getModelsArray().map((m: any) => m.id?._serialized));
  await send({
    chat,
    eventCreationOpts: {
      name,
      description: typeof request?.description === 'string' ? request.description : undefined,
      location: typeof request?.location === 'string' ? request.location : undefined,
      startTime,
      endTime,
      callType,
      extraGuestsAllowed: request?.extraGuestsAllowed === true,
    },
  });
  // sendEventCreationMessage answers nothing: the new message is the one
  // this account added to the chat with this name and start.
  const made = chat.msgs.getModelsArray().filter((m: any) => m.type === 'event_creation'
    && m.id?.fromMe && !before.has(m.id?._serialized)
    && m.eventName === name && Number(m.eventStartTime) === startTime);
  const id = made.length ? made[made.length - 1].id._serialized : '';
  return { ok: true, id };
}

export async function editEvent(request: any) {
  const root = globalThis as any;
  const req = (name: string): any => { try { return root.require(name); } catch (_) { return undefined; } };
  try {
    await req('JSResourceForInteraction')('WAWebEventsCreateEventModalFlow.react')
      .__setRef('WinZapp').load();
  } catch (_) { /* the module may already be there */ }
  const send = req('WAWebSendEventEditMsgAction')?.sendEventEditMessage;
  const asEvent = req('WAWebFrontendMsgGetters')?.getAsEventCreation;
  if (typeof send !== 'function' || typeof asEvent !== 'function') {
    return { ok: false, error: 'event_edit_unavailable' };
  }
  let msg: any;
  try { msg = await root.WPP?.chat?.getMessageById?.(String(request?.messageId || '')); } catch (_) { msg = undefined; }
  if (!msg || msg.type !== 'event_creation') return { ok: false, error: 'event_not_found' };
  if (!msg.id?.fromMe) return { ok: false, error: 'event_not_mine' };
  if (msg.isEventCanceled) return { ok: false, error: 'event_canceled' };
  const location = (value: any) => {
    if (typeof value === 'string') return value;
    return typeof value?.name === 'string' ? value.name : undefined;
  };
  const cancel = request?.isEventCanceled === true;
  const pick = (key: string, current: any) => (request && key in request ? request[key] : current);
  const name = String(pick('name', msg.eventName) || '').trim();
  const startTime = Number(pick('startTime', msg.eventStartTime)) || 0;
  if (!name || !startTime) return { ok: false, error: 'event_invalid' };
  const link = String(msg.eventJoinLink || '');
  const currentCall = link ? (link.includes('/video/') ? 'video' : 'voice') : undefined;
  const callType = 'callType' in (request || {})
    ? (request.callType === 'voice' || request.callType === 'video' ? request.callType : undefined)
    : currentCall;
  await send({
    name,
    description: pick('description', msg.eventDescription),
    location: location(pick('location', msg.eventLocation)),
    startTime,
    endTime: Number(pick('endTime', msg.eventEndTime)) || undefined,
    callType,
    isEventCanceled: cancel,
  }, asEvent(msg));
  return { ok: true };
}
