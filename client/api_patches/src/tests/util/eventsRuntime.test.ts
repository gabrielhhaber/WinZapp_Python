import { createEvent, editEvent, readEventResponses, writeEventResponse } from '../../util/eventsRuntime';
import { eventFields } from '../../controller/eventController';

// The shapes WhatsApp Web's own modules had over CDP on 2026-11-11.
const ID = 'true_123@lid_3EB0AAAA';
const calls: any[] = [];

function eventMsg(extra: any = {}) {
  return {
    id: { _serialized: ID, fromMe: true, id: '3EB0AAAA' }, type: 'event_creation',
    eventName: 'Churrasco', eventDescription: 'No sítio', eventStartTime: 1800000000,
    eventEndTime: 1800003600, eventLocation: { name: 'Sítio', degreesLatitude: 0 },
    eventJoinLink: 'https://call.whatsapp.com/video/abc', isEventCanceled: false, ...extra,
  };
}

function withPage(msg: any, run: (chatMsgs: any[]) => Promise<void>) {
  const root = globalThis as any;
  const previous = [root.require, root.WPP];
  calls.length = 0;
  const chatMsgs: any[] = [];
  const modules: any = {
    WAWebMsgType: { MSG_TYPE: { EVENT_RESPONSE: 'event_response' } },
    WAWebAddonHydrationUtils: { hydrateAddons: async (o: any) => calls.push(['hydrate', o.hydrationType]) },
    WAWebFrontendMsgGetters: { getAsEventCreation: (m: any) => ({ event: m }) },
    WAWebReferentialMsgKey: { getReferentialMsgKey: (e: any) => e.event.id },
    WAWebUserPrefsMeUser: { isMeAccount: (s: any) => (s?._serialized || s) === 'me@lid' },
    WAWebEventResponseCollection: { EventResponseCollection: { byParent: () => ({ toArray: () => [
      { sender: { _serialized: 'me@lid' }, eventResponse: 1 },
      { sender: 'ana@lid', eventResponse: 3, extraGuestCount: 2 },
      { sender: 'bia@lid', eventResponse: 0 },
    ] }) } },
    WAWebSendEventResponseMsgAction: { sendEventResponseMsg: async (t: number, e: any) => calls.push(['respond', t, e.event.id._serialized]) },
    JSResourceForInteraction: (name: string) => ({ __setRef: () => ({ load: async () => calls.push(['load', name]) }) }),
    WAWebSendEventCreationMsgAction: { sendEventCreationMessage: async (o: any) => {
      calls.push(['create', o.eventCreationOpts]);
      chatMsgs.push({ type: 'event_creation', id: { _serialized: 'true_c@g.us_NEW', fromMe: true },
        eventName: o.eventCreationOpts.name, eventStartTime: o.eventCreationOpts.startTime });
    } },
    WAWebSendEventEditMsgAction: { sendEventEditMessage: async (f: any, e: any) => calls.push(['edit', f, e.event.id._serialized]) },
  };
  root.require = (name: string) => modules[name];
  root.WPP = { chat: {
    getMessageById: async (id: string) => (id === ID ? msg : undefined),
    find: async (id: string) => (id === 'c@g.us' ? { msgs: { getModelsArray: () => chatMsgs } } : undefined),
  } };
  return run(chatMsgs).finally(() => { [root.require, root.WPP] = previous; });
}

test('answers are read with who answered what and which one is mine', () => withPage(eventMsg(), async () => {
  const got: any = await readEventResponses({ messageIds: [ID, 'false_x@lid_NOPE'] });
  expect(got).toEqual({ [ID]: { mine: 1, responses: [
    { sender: 'me@lid', response: 1, extraGuestCount: 0, isMe: true },
    { sender: 'ana@lid', response: 3, extraGuestCount: 2, isMe: false },
  ] } });
  expect(calls).toEqual([['hydrate', 'event_response']]);
}));

test('an answer goes through WhatsApp Web own action, never to a canceled event', async () => {
  await withPage(eventMsg(), async () => {
    expect(await writeEventResponse({ messageId: ID, response: 2 })).toEqual({ ok: true });
    expect(await writeEventResponse({ messageId: ID, response: 4 })).toEqual({ ok: false, error: 'event_response_invalid' });
    expect(calls).toEqual([['respond', 2, ID]]);
  });
  await withPage(eventMsg({ isEventCanceled: true }), async () => {
    expect(await writeEventResponse({ messageId: ID, response: 1 })).toEqual({ ok: false, error: 'event_canceled' });
  });
});

test('creating loads the lazy bundle and answers the new message id', () => withPage(eventMsg(), async () => {
  const got = await createEvent({ chatId: 'c@g.us', name: ' Churrasco ', startTime: 1800000000,
    location: 'Sítio', callType: 'video', extraGuestsAllowed: true });
  expect(got).toEqual({ ok: true, id: 'true_c@g.us_NEW' });
  expect(calls[0]).toEqual(['load', 'WAWebEventsCreateEventModalFlow.react']);
  expect(calls[1][1]).toMatchObject({ name: 'Churrasco', location: 'Sítio', callType: 'video', extraGuestsAllowed: true });
  expect(await createEvent({ chatId: 'c@g.us', name: '', startTime: 1 })).toEqual({ ok: false, error: 'event_invalid' });
}));

test('an edit keeps what it does not change, and a cancel is an edit', () => withPage(eventMsg(), async () => {
  expect(await editEvent({ messageId: ID, description: 'Na praia' })).toEqual({ ok: true });
  expect(calls[1]).toEqual(['edit', { name: 'Churrasco', description: 'Na praia', location: 'Sítio',
    startTime: 1800000000, endTime: 1800003600, callType: 'video', isEventCanceled: false }, ID]);
  await editEvent({ messageId: ID, isEventCanceled: true });
  expect(calls[3][1].isEventCanceled).toBe(true);
}));

test('only the creator edits', () => withPage(eventMsg({ id: { _serialized: ID, fromMe: false } }), async () => {
  expect(await editEvent({ messageId: ID, name: 'X' })).toEqual({ ok: false, error: 'event_not_mine' });
}));

test('the route validates the fields', () => {
  expect(eventFields({ name: 'A', startTime: 10, endTime: 20, callType: 'none' }))
    .toEqual({ name: 'A', startTime: 10, endTime: 20, callType: null });
  expect(eventFields({ name: 'A', startTime: 20, endTime: 10 })).toBeNull();
  expect(eventFields({ name: ' ', startTime: 10 })).toBeNull();
  expect(eventFields({ name: 'A' })).toBeNull();
  expect(eventFields({ description: 'x' }, false)).toEqual({ description: 'x' });
  expect(eventFields({ callType: 'phone' }, false)).toBeNull();
});
