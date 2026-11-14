import { callParticipantsPayload, installCallParticipantsInPage } from '../../util/callParticipantsBridge';

/** An object with WhatsApp Web's own events: on(name, fn), fire(name, ...args). */
function emitter(fields: Record<string, any> = {}) {
  const handlers: Record<string, Array<(...a: any[]) => void>> = {};
  return Object.assign(fields, {
    handlers,
    on: (event: string, fn: (...a: any[]) => void) => { (handlers[event] = handlers[event] || []).push(fn); },
    fire: (event: string, ...args: any[]) => (handlers[event] || []).forEach((fn) => fn(...args)),
  });
}

function withPage(run: (page: any) => void, activeCall: any = null) {
  const root = globalThis as any;
  const previous = [root.window, root.require];
  const sent: string[] = [];
  const calls = emitter({ activeCall });
  root.window = root;
  root.__winzappOnCallParticipants = (raw: string) => sent.push(raw);
  root.require = (name: string) => ({
    WAWebCallCollection: calls,
    WAWebLidMigrationUtils: { toPn: (wid: any) => (wid._serialized === '9@lid' ? { _serialized: '5511@c.us' } : null) },
  } as any)[name];
  try {
    run({ sent, calls });
  } finally {
    delete root.__winzappOnCallParticipants;
    [root.window, root.require] = previous;
  }
}

const wid = (id: string) => ({ _serialized: id });

// Puppeteer sends the function's source to the page: nothing of this module
// exists there. Run it the same way, so a module constant read inside fails
// here as it failed in the page.
const installInPage = (): boolean =>
  new Function(`return (${installCallParticipantsInPage.toString()})();`)();
const last = (sent: string[]) => JSON.parse(sent[sent.length - 1]);

// WAWebVoipBridgeCallStateHandlers assigns the list and raises
// change:groupCallParticipantStates on the call itself, never on the collection.
test('a change of who is connected on the held call sends the whole list, phone form first', () => withPage(({ sent, calls }) => {
  expect(installInPage()).toBe(true);
  const call = emitter({ id: 'CALL1', isGroup: true, groupCallParticipantsConnected: [wid('7@lid')] });
  calls.fire('change:activeCall', call);
  expect(last(sent)).toEqual({ callId: 'CALL1', connected: ['7@lid'] });
  call.groupCallParticipantsConnected = [wid('9@lid'), wid('7@lid'), wid('x@g.us')];
  call.fire('change:groupCallParticipantStates');
  expect(last(sent)).toEqual({ callId: 'CALL1', connected: ['5511@s.whatsapp.net', '7@lid'] });
  call.fire('change:groupCallParticipantsConnected');
  expect(sent.length).toBe(3);
}));

test('a call already held when the page is reached is followed too', () => {
  const call = emitter({ id: 'CALL4', isGroup: true, groupCallParticipantsConnected: [] });
  withPage(({ sent }) => {
    expect(installInPage()).toBe(true);
    expect(last(sent)).toEqual({ callId: 'CALL4', connected: [] });
    call.groupCallParticipantsConnected = [wid('7@lid')];
    call.fire('change:groupCallParticipantStates');
    expect(last(sent)).toEqual({ callId: 'CALL4', connected: ['7@lid'] });
  }, call);
});

test('a call is listened to once, however often it becomes the held call', () => withPage(({ calls }) => {
  expect(installInPage()).toBe(true);
  expect(installInPage()).toBe(true);
  expect(calls.handlers['change:activeCall'].length).toBe(1);
  const call = emitter({ id: 'CALL5', isGroup: true, groupCallParticipantsConnected: [] });
  calls.fire('change:activeCall', call);
  calls.fire('add', call);
  calls.fire('change:activeCall', call);
  expect(call.handlers['change:groupCallParticipantStates'].length).toBe(1);
}));

test('the held call is found whether the event hands over it alone or after the collection', () => withPage(({ sent, calls }) => {
  expect(installInPage()).toBe(true);
  const call = emitter({ id: 'CALL6', isGroup: true, groupCallParticipantsConnected: [wid('7@lid')] });
  calls.fire('change:activeCall', calls, call);
  expect(last(sent)).toEqual({ callId: 'CALL6', connected: ['7@lid'] });
  expect(call.handlers['change:groupCallParticipantStates'].length).toBe(1);
  const other = emitter({ id: 'CALL7', isGroup: true, groupCallParticipantsConnected: [wid('9@lid')] });
  calls.fire('add', other, calls, {});
  expect(last(sent)).toEqual({ callId: 'CALL7', connected: ['5511@s.whatsapp.net'] });
}));

test('a one-to-one call, a model without the list or no call says nothing', () => withPage(({ sent, calls }) => {
  expect(installInPage()).toBe(true);
  calls.fire('add', emitter({ id: 'CALL2', isGroup: false, groupCallParticipantsConnected: [wid('7@lid')] }));
  calls.fire('add', emitter({ id: 'CALL3', isGroup: true }));
  calls.fire('change:activeCall', null);
  expect(sent).toEqual([]);
}));

test('only a well-formed list leaves the server', () => {
  expect(callParticipantsPayload('{"callId":"C","connected":["1@lid"]}', 's'))
    .toEqual({ session: 's', callId: 'C', connected: ['1@lid'] });
  expect(callParticipantsPayload('{"callId":"C","connected":["1@g.us"]}', 's')).toBeNull();
  expect(callParticipantsPayload('{"callId":"","connected":[]}', 's')).toBeNull();
  expect(callParticipantsPayload('nope', 's')).toBeNull();
});
