import { callParticipantsPayload, installCallParticipantsInPage } from '../../util/callParticipantsBridge';

function withPage(run: (page: any) => void) {
  const root = globalThis as any;
  const previous = [root.window, root.require];
  const sent: string[] = [];
  const handlers: Record<string, Array<(m: any) => void>> = {};
  const calls = {
    on: (event: string, fn: (m: any) => void) => { (handlers[event] = handlers[event] || []).push(fn); },
  };
  const fire = (event: string, model: any) => (handlers[event] || []).forEach((fn) => fn(model));
  root.window = root;
  root.__winzappOnCallParticipants = (raw: string) => sent.push(raw);
  root.require = (name: string) => ({
    WAWebCallCollection: calls,
    WAWebLidMigrationUtils: { toPn: (wid: any) => (wid._serialized === '9@lid' ? { _serialized: '5511@c.us' } : null) },
  } as any)[name];
  try {
    run({ sent, fire, handlers });
  } finally {
    delete root.__winzappOnCallParticipants;
    [root.window, root.require] = previous;
  }
}

const wid = (id: string) => ({ _serialized: id });

test('a change of who is connected sends the whole list, phone form first', () => withPage(({ sent, fire }) => {
  expect(installCallParticipantsInPage()).toBe(true);
  fire('change:groupCallParticipantsConnected', {
    id: 'CALL1', isGroup: true, groupCallParticipantsConnected: [wid('9@lid'), wid('7@lid'), wid('x@g.us')] });
  expect(JSON.parse(sent[0])).toEqual({ callId: 'CALL1', connected: ['5511@s.whatsapp.net', '7@lid'] });
}));

test('a one-to-one call or a model without the list says nothing', () => withPage(({ sent, fire, handlers }) => {
  expect(installCallParticipantsInPage()).toBe(true);
  expect(installCallParticipantsInPage()).toBe(true);
  expect(handlers['change:groupCallParticipantsConnected'].length).toBe(1);
  fire('add', { id: 'CALL2', isGroup: false, groupCallParticipantsConnected: [wid('7@lid')] });
  fire('add', { id: 'CALL3', isGroup: true });
  expect(sent).toEqual([]);
}));

test('only a well-formed list leaves the server', () => {
  expect(callParticipantsPayload('{"callId":"C","connected":["1@lid"]}', 's'))
    .toEqual({ session: 's', callId: 'C', connected: ['1@lid'] });
  expect(callParticipantsPayload('{"callId":"C","connected":["1@g.us"]}', 's')).toBeNull();
  expect(callParticipantsPayload('{"callId":"","connected":[]}', 's')).toBeNull();
  expect(callParticipantsPayload('nope', 's')).toBeNull();
});
