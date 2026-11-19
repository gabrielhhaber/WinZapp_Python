import { channelsChangedPayload, installChannelsWatchInPage } from '../../util/channelsWatchBridge';

function collection() {
  const handlers: Record<string, Array<(m: any) => void>> = {};
  return {
    handlers,
    on: (event: string, fn: (m: any) => void) => { (handlers[event] = handlers[event] || []).push(fn); },
    fire: (event: string, model: any) => (handlers[event] || []).forEach((fn) => fn(model)),
  };
}

function withPage(run: (page: any) => Promise<void> | void) {
  const root = globalThis as any;
  const previous = [root.window, root.require];
  const sent: string[] = [];
  const channels = collection();
  const metadata = collection();
  root.window = root;
  root.__winzappOnChannelsChanged = (raw: string) => sent.push(raw);
  root.require = (name: string) => (name === 'WAWebCollections'
    ? { WAWebNewsletterCollection: channels, WAWebNewsletterMetadataCollection: metadata }
    : undefined);
  jest.useFakeTimers();
  return Promise.resolve(run({ sent, channels, metadata })).finally(() => {
    jest.useRealTimers();
    delete root.__winzappOnChannelsChanged;
    [root.window, root.require] = previous;
  });
}

const model = (id: string) => ({ id: { _serialized: id } });

test('following, leaving, muting and a role change are one coalesced event', () => withPage(({ sent, channels, metadata }) => {
  expect(installChannelsWatchInPage()).toBe(true);
  channels.fire('add', model('2@newsletter'));
  channels.fire('change:muteExpiration', model('2@newsletter'));
  channels.fire('add', model('2@newsletter'));
  jest.advanceTimersByTime(1000);
  expect(sent).toEqual(['{"kind":"joined","id":"2@newsletter"}']);
  channels.fire('change:muteExpiration', model('2@newsletter'));
  jest.advanceTimersByTime(1000);
  metadata.fire('change:membershipType', model('2@newsletter'));
  jest.advanceTimersByTime(1000);
  metadata.fire('change:name change:description', model('2@newsletter'));
  jest.advanceTimersByTime(1000);
  metadata.fire('change:adminCount', model('2@newsletter'));
  jest.advanceTimersByTime(1000);
  channels.fire('remove', model('2@newsletter'));
  jest.advanceTimersByTime(1000);
  expect(sent.slice(1).map((s) => JSON.parse(s).kind)).toEqual(['muted', 'role', 'edited', 'admins', 'left']);
}));

test('two channels changed in the same second are both reported', () => withPage(({ sent, channels, metadata }) => {
  // the admins window of channel 1 follows only channel 1
  expect(installChannelsWatchInPage()).toBe(true);
  metadata.fire('change:adminCount', model('1@newsletter'));
  channels.fire('change:muteExpiration', model('2@newsletter'));
  jest.advanceTimersByTime(1000);
  expect(sent).toEqual(['{"kind":"admins","id":"1@newsletter"}', '{"kind":"muted","id":"2@newsletter"}']);
}));

test('it installs once, and anything but a channel says nothing', () => withPage(({ sent, channels }) => {
  expect(installChannelsWatchInPage()).toBe(true);
  expect(installChannelsWatchInPage()).toBe(true);
  expect(channels.handlers.add.length).toBe(1);
  channels.fire('add', model('5511@c.us'));
  channels.fire('add', {});
  jest.advanceTimersByTime(1000);
  expect(sent).toEqual([]);
}));

test('a page not ready yet answers false, so the bridge tries the install again', () => {
  const root = globalThis as any;
  const previous = [root.window, root.require];
  root.window = root;
  root.require = () => undefined;
  try {
    expect(installChannelsWatchInPage()).toBe(false);
  } finally {
    [root.window, root.require] = previous;
  }
});

test('only a well-formed change leaves the server', () => {
  expect(channelsChangedPayload('{"kind":"left","id":"1@newsletter"}', 's'))
    .toEqual({ session: 's', kind: 'left', id: '1@newsletter' });
  expect(channelsChangedPayload('{"kind":"other","id":"1@newsletter"}', 's')).toBeNull();
  expect(channelsChangedPayload('{"kind":"left","id":"5511@c.us"}', 's')).toBeNull();
  expect(channelsChangedPayload({ kind: 'left' }, 's')).toBeNull();
  expect(channelsChangedPayload('not json', 's')).toBeNull();
});
