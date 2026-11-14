import { chatClearedPayload, installChatClearedInPage } from '../../util/chatClearedBridge';

// The shape WhatsApp Web's ClearChatSync received for a clear made on the
// phone (read over CDP on 2026-11-11).
const clear = (parts: string[], value: any = {
  timestamp: 1794444496000,
  clearChatAction: { messageRange: { lastMessageTimestamp: 1794444400 } },
}) => ({ operation: 'set', indexParts: parts, value, timestamp: 1794444496000 });

function withPage(run: (page: any) => void) {
  const root = globalThis as any;
  const previous = [root.window, root.require];
  const sent: string[] = [];
  const applied: any[] = [];
  const handler: any = {
    applyMutations(mutations: any, ...rest: any[]) { applied.push([mutations, ...rest]); return 'done'; },
  };
  root.window = root;
  root.__winzappOnChatCleared = (raw: string) => sent.push(raw);
  root.require = (name: string) => (name === '__debug'
    ? { modulesMap: { WAWebClearChatSync: { defaultExport: handler } } }
    : {});
  try {
    run({ sent, applied, handler });
  } finally {
    delete root.__winzappOnChatCleared;
    [root.window, root.require] = previous;
  }
}

test('a clear from the phone is reported and still applied by WhatsApp Web', () => withPage(({ sent, applied, handler }) => {
  expect(installChatClearedInPage()).toBe(true);
  expect(installChatClearedInPage()).toBe(true);    // once
  const batch = [clear(['clearChat', '123@lid', '0']), clear(['favorites'])];
  expect(handler.applyMutations(batch, { x: 1 }, false)).toBe('done');
  expect(applied).toEqual([[batch, { x: 1 }, false]]);
  expect(sent).toEqual(['{"chatId":"123@lid","until":1794444400,"keepStarred":true}']);
}));

test('the starred ones gone, no range, a removal', () => withPage(({ sent, handler }) => {
  installChatClearedInPage();
  handler.applyMutations([
    clear(['clearChat', '5511@c.us', '1']),
    clear(['clearChat', '1@g.us', '0'], { timestamp: 1794444496000, clearChatAction: {} }),
    { ...clear(['clearChat', '9@lid', '0']), operation: 'remove' },
  ]);
  expect(sent.map((s) => JSON.parse(s))).toEqual([
    { chatId: '5511@c.us', until: 1794444400, keepStarred: false },
    { chatId: '1@g.us', until: 1794444496, keepStarred: true },
  ]);
}));

test('a page without the module or the binding is retried', () => {
  const root = globalThis as any;
  const previous = [root.window, root.require];
  root.window = root;
  root.require = () => ({});
  try {
    expect(installChatClearedInPage()).toBe(false);
  } finally {
    [root.window, root.require] = previous;
  }
});

test('only a well-formed notice becomes a socket payload', () => {
  expect(chatClearedPayload('{"chatId":"123@lid","until":5,"keepStarred":true}', 's'))
    .toEqual({ session: 's', chatId: '123@lid', until: 5, keepStarred: true });
  for (const bad of ['{"chatId":"1@newsletter","until":5,"keepStarred":true}',
    '{"chatId":"123@lid","until":0,"keepStarred":true}',
    '{"chatId":"123@lid","until":5}', 'x', 42, 'a'.repeat(300)]) {
    expect(chatClearedPayload(bad, 's')).toBeNull();
  }
});
