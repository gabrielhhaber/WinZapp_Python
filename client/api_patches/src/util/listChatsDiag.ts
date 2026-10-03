/*
 * Diagnostics for POST /list-chats (see deviceController.listChats).
 *
 * A tester's server answered 200 with 0 chats on 30+ attempts while
 * get-messages and all-contacts worked, and the log could not say whether the
 * ChatStore list was empty or non-empty and emptied by the visibleChats filter.
 * These functions turn the two lists into ONE log line made only of counts,
 * booleans and a short ASCII code: docs/traps/log-pii.md forbids a JID, a
 * phone number, a name or message text in a log. Nothing here decides anything
 * about the response.
 *
 * Plain functions with only erasable type annotations, so the tests can run
 * this very file under node's type stripping.
 */

export interface ListChatsCounts {
  raw: number;
  visible: number;
  groups: number;
  users: number;
  lids: number;
  other: number;
  droppedShells: number;
  droppedNoT: number;
  droppedNoLastMsg: number;
  droppedNoUnread: number;
  droppedNoOpened: number;
  droppedNoContact: number;
}

// 6 digits at most, so a count can never look like a phone number or an id.
const MAX_COUNT = 999999;

/** Counts over the list BEFORE the visibleChats filter and the one after it. */
export function buildDiag(rawChats: any, visibleChats: any): ListChatsCounts {
  const raw: any[] = Array.isArray(rawChats) ? rawChats : [];
  const visible: any[] = Array.isArray(visibleChats) ? visibleChats : [];
  const kept = new Set<any>(visible);
  const counts: ListChatsCounts = {
    raw: raw.length,
    visible: visible.length,
    groups: 0,
    users: 0,
    lids: 0,
    other: 0,
    droppedShells: Math.max(raw.length - visible.length, 0),
    droppedNoT: 0,
    droppedNoLastMsg: 0,
    droppedNoUnread: 0,
    droppedNoOpened: 0,
    droppedNoContact: 0,
  };

  for (const chat of raw) {
    try {
      const candidate = chat?.id?._serialized || chat?.id;
      const jid = typeof candidate === 'string' ? candidate : '';
      if (jid.endsWith('@g.us')) counts.groups++;
      else if (jid.endsWith('@lid')) counts.lids++;
      else if (jid.endsWith('@c.us') || jid.endsWith('@s.whatsapp.net'))
        counts.users++;
      else counts.other++;

      // Same fields the visibleChats filter reads, so the next log says WHY a
      // chat was dropped, not only that it was.
      if (kept.has(chat)) continue;
      if (!chat?.t) counts.droppedNoT++;
      if (!chat?.lastMessage) counts.droppedNoLastMsg++;
      if (!(Number(chat?.unreadCount || 0) > 0)) counts.droppedNoUnread++;
      if (chat?.hasChatBeenOpened !== true) counts.droppedNoOpened++;
      if (chat?.contact?.isMyContact !== true) counts.droppedNoContact++;
    } catch (_error) {
      counts.other++;
    }
  }
  return counts;
}

function count(value: any): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return 0;
  return Math.min(Math.trunc(value), MAX_COUNT);
}

/** A count, or a fixed word when the value is not a number. */
function countOr(value: any, words: string[], fallback: string): string {
  if (typeof value === 'number') return String(count(value));
  return words.includes(value) ? value : fallback;
}

/**
 * A closed classification of an error text, never any of its words: an error
 * message can carry a name or an id (docs/traps/log-pii.md).
 */
function errorCode(value: any): string {
  if (typeof value !== 'string' || !value) return 'none';
  if (value.includes('returned a non-array')) return 'non_array';
  if (value.includes('_serializeChatObj is unavailable'))
    return 'serializer_unavailable';
  if (/Cannot read|is not a function|is not defined|undefined/i.test(value))
    return 'js_type_error';
  if (/time(d)? ?out/i.test(value)) return 'timeout';
  return 'other';
}

/** The one-line form of a diagnostic; never throws, never spans two lines. */
export function formatDiagLine(d: any): string {
  try {
    const flag = (value: any) => (value === true ? 'true' : 'false');
    return (
      '[listChats] diag' +
      ` raw=${count(d?.raw)}` +
      ` rawFirst=${countOr(d?.rawFirst, [], 'na')}` +
      ` rawSecond=${countOr(d?.rawSecond, [], 'none')}` +
      ` visible=${count(d?.visible)}` +
      ` groups=${count(d?.groups)}` +
      ` users=${count(d?.users)}` +
      ` lids=${count(d?.lids)}` +
      ` other=${count(d?.other)}` +
      ` droppedShells=${count(d?.droppedShells)}` +
      ` droppedNoT=${count(d?.droppedNoT)}` +
      ` droppedNoLastMsg=${count(d?.droppedNoLastMsg)}` +
      ` droppedNoUnread=${count(d?.droppedNoUnread)}` +
      ` droppedNoOpened=${count(d?.droppedNoOpened)}` +
      ` droppedNoContact=${count(d?.droppedNoContact)}` +
      ` recovered=${flag(d?.recovered)}` +
      ` firstError=${errorCode(d?.firstError)}` +
      ` storeReady=${flag(d?.storeReady)}` +
      ` storeChats=${countOr(d?.storeChats, [], 'na')}` +
      ` idbChats=${countOr(d?.idbChats, ['unavailable', 'timeout', 'skipped'], 'unavailable')}` +
      ` ms=${count(d?.ms)}`
    );
  } catch (_error) {
    return '[listChats] diag unavailable';
  }
}

const RATE_LIMIT_MS = 30_000;
const IDB_INTERVAL_MS = 60_000;

export interface DiagLogState {
  lastAt: number;
  lastKey: string;
  loggedChats: boolean;
  idbAt: number;
}

const states = new Map<string, DiagLogState>();

/** Rate-limit state of one session (the Node serves several accounts' paths). */
export function diagStateFor(session: string): DiagLogState {
  let state = states.get(session);
  if (!state) {
    state = { lastAt: 0, lastKey: '', loggedChats: false, idbAt: 0 };
    states.set(session, state);
  }
  return state;
}

/**
 * Whether this call's diagnostic is worth a line: every call that ends with no
 * visible chat, and the first one of the session that returns any. At most one
 * per 30 s, plus one whenever the numbers change.
 */
export function shouldLogDiag(
  state: DiagLogState,
  counts: ListChatsCounts,
  now: number
): boolean {
  const firstWithChats = counts.visible > 0 && !state.loggedChats;
  if (counts.visible > 0 && !firstWithChats) return false;
  const key = [
    counts.raw,
    counts.visible,
    counts.groups,
    counts.users,
    counts.lids,
    counts.droppedShells,
  ].join('/');
  if (key === state.lastKey && now - state.lastAt < RATE_LIMIT_MS) return false;
  state.lastKey = key;
  state.lastAt = now;
  if (counts.visible > 0) state.loggedChats = true;
  return true;
}

/** The IndexedDB count is paid at most once a minute per session. */
export function claimIdbCount(state: DiagLogState, now: number): boolean {
  if (now - state.idbAt < IDB_INTERVAL_MS) return false;
  state.idbAt = now;
  return true;
}
