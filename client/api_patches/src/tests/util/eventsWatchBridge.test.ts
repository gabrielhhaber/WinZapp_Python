import { eventChangedPayload, installEventsWatchInPage } from '../../util/eventsWatchBridge';

function collection() {
  const handlers: Record<string, Array<(m: any) => void>> = {};
  return {
    on: (events: string, fn: (m: any) => void) => {
      for (const e of events.split(' ')) (handlers[e] = handlers[e] || []).push(fn);
    },
    fire: (event: string, model: any) => (handlers[event] || []).forEach((fn) => fn(model)),
  };
}

function withPage(run: (page: any) => void) {
  const root = globalThis as any;
  const previous = [root.window, root.require];
  const sent: string[] = [];
  const responses = collection();
  const msgs = collection();
  root.window = root;
  root.__winzappOnEventChanged = (raw: string) => sent.push(raw);
  root.require = (name: string) => ({
    WAWebEventResponseCollection: { EventResponseCollection: responses },
    WAWebCollections: { Msg: msgs },
  } as any)[name];
  jest.useFakeTimers();
  try {
    run({ sent, responses, msgs });
  } finally {
    jest.useRealTimers();
    delete root.__winzappOnEventChanged;
    [root.window, root.require] = previous;
  }
}

test('answers and edits become one coalesced change per event', () => withPage(({ sent, responses, msgs }) => {
  expect(installEventsWatchInPage()).toBe(true);
  expect(installEventsWatchInPage()).toBe(true);     // installed once
  responses.fire('add', { parentMsgKey: { id: '3EB0AAAA' } });
  responses.fire('change:eventResponse', { parentMsgKey: { id: '3EB0AAAA' } });
  msgs.fire('change:isEventCanceled', { type: 'event_creation', id: { id: '3EB0BBBB' } });
  msgs.fire('change:eventName', { type: 'chat', id: { id: '3EB0CCCC' } });   // not an event
  jest.advanceTimersByTime(500);
  expect(sent.map((s) => JSON.parse(s))).toEqual([
    { kind: 'responses', id: '3EB0AAAA' }, { kind: 'edited', id: '3EB0BBBB' }]);
}));

test('only a well-formed change becomes a socket payload', () => {
  expect(eventChangedPayload('{"kind":"edited","id":"3EB0AAAA"}', 's'))
    .toEqual({ session: 's', kind: 'edited', id: '3EB0AAAA' });
  for (const bad of ['{"kind":"x","id":"3EB0AAAA"}', '{"kind":"edited","id":"a b"}', 'x', 5]) {
    expect(eventChangedPayload(bad, 's')).toBeNull();
  }
});
