// A chat cleared on another device (the phone), for WinZapp's conversation.
//
// WA-JS has no event for it. WhatsApp Web applies the app state mutation in
// WAWebClearChatSync (collection regular_high): its handler's
// applyMutations(mutations, ...) receives, for a clear made on the phone,
// { operation: 'set', indexParts: ['clearChat', '<chat>@lid', '0'],
//   value: { timestamp, clearChatAction: { messageRange: {
//     lastMessageTimestamp, messages } } } }
// (read over CDP on 2026-11-11, 2.3000.x, clearing a chat on the phone). The
// third part is "1" when the starred messages went too. The handler is the
// module's default export, an object registered once, so wrapping its method
// on the instance sees every clear.
//
// The page sends { chatId, until, keepStarred } for each clear and the server
// emits it as `chat-cleared`; the client (core/remote_chat_clear.py) removes
// the messages up to `until`. The Neonize bridge sends the same event
// (neonize_bridge/phone_chat_clear.py). Only the chat id leaves the page.

const BINDING = '__winzappOnChatCleared';

/** Runs inside the page; serialized by Puppeteer, so self-contained. */
export function installChatClearedInPage(): boolean {
  const win = window as any;
  const notify = win.__winzappOnChatCleared;
  if (typeof notify !== 'function' || typeof win.require !== 'function') return false;
  let handler: any = null;
  try {
    const entry = win.require('__debug')?.modulesMap?.WAWebClearChatSync;
    handler = entry?.defaultExport || null;
  } catch (_) {
    handler = null;
  }
  if (!handler) {
    try {
      win.require('WAWebClearChatSync');   // loads it, so the next try finds it
    } catch (_) { /* not loaded yet */ }
    return false;
  }
  if (typeof handler.applyMutations !== 'function') return false;
  if (handler.__winzappChatCleared) return true;
  const seconds = (value: any) => {
    const n = Number(value && typeof value === 'object' && 'low' in value
      ? value.low + value.high * 4294967296 : value);
    if (!Number.isFinite(n) || n <= 0) return 0;
    return Math.floor(n > 1e11 ? n / 1000 : n);
  };
  const original = handler.applyMutations;
  handler.applyMutations = function (mutations: any, ...rest: any[]) {
    try {
      for (const m of Array.isArray(mutations) ? mutations : []) {
        const parts = m && Array.isArray(m.indexParts) ? m.indexParts : null;
        if (!parts || parts[0] !== 'clearChat' || m.operation !== 'set') continue;
        const chatId = String(parts[1] || '');
        const range = m.value?.clearChatAction?.messageRange;
        const until = seconds(range?.lastMessageTimestamp) || seconds(m.value?.timestamp)
          || seconds(m.timestamp);
        if (!chatId.includes('@') || !until) continue;
        notify(JSON.stringify({ chatId, until, keepStarred: parts[2] !== '1' }));
      }
    } catch (_) {
      // one lost notice, never WhatsApp Web's own clear
    }
    return original.call(this, mutations, ...rest);
  };
  handler.__winzappChatCleared = true;
  return true;
}

/** The socket payload of what the page sent, or null when it is not one. */
export function chatClearedPayload(raw: unknown, session: string) {
  if (typeof raw !== 'string' || raw.length > 256) return null;
  try {
    const value = JSON.parse(raw);
    if (!value || typeof value.chatId !== 'string'
        || !/^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid|g\.us)$/.test(value.chatId)
        || !Number.isSafeInteger(value.until) || value.until <= 0
        || typeof value.keepStarred !== 'boolean') {
      return null;
    }
    return { session, chatId: value.chatId, until: value.until, keepStarred: value.keepStarred };
  } catch {
    return null;
  }
}

export async function installChatClearedBridge(client: any, io: any, logger: any) {
  const page = client?.page;
  if (!page) return;
  try {
    await page.exposeFunction(BINDING, (raw: unknown) => {
      const payload = chatClearedPayload(raw, client.session);
      if (payload) io.to(`session:${client.session}`).emit('chat-cleared', payload);
    });
  } catch {
    // Puppeteer bindings survive navigation; duplicate registration is expected.
  }
  const install = (attempt = 0) => {
    page.evaluate(installChatClearedInPage)
      .then((installed: boolean) => {
        if (!installed && attempt < 120) {
          setTimeout(() => install(attempt + 1), 500);
        } else if (installed) {
          logger?.info?.(`[${client.session}] chat-cleared bridge installed`);
        } else {
          logger?.warn?.('[chatClearedBridge] GAVE UP: a chat cleared on the phone '
            + 'will not be cleared in WinZapp this session');
        }
      })
      .catch((e: any) => logger?.warn?.(`[chatClearedBridge] install failed: ${e?.message || e}`));
  };
  // A page reload is a fresh JS context: the wrapper goes with it.
  page.on('load', () => install());
  install();
}
