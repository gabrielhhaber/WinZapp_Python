// get-messages can answer a stand-in where a message should be. When a reply
// quotes a message the page has not loaded, WhatsApp Web builds a model from
// the quote (`fromQuotedMsg`) and keeps it in MsgStore under the real id; the
// history query (msgFindBefore reads the IndexedDB, then hands back the
// MsgStore model of each id) returns that stand-in in the real message's
// place: text, author and position right, but no `t`, `timestamp` or `ack`.
//
// Measured live (2026-11-19, a group): the stand-in of a message received at
// 10:30:53 had no `t` while the IndexedDB `message` row of the same id held
// t=10:30:53 and ack=3. The client stamped the stand-in "now" on a restart,
// so the message jumped below the replies that quote it.
//
// The repair reads the stored row of each message that has no time; with no
// row, the stand-in's own `clientReceivedTsMillis` is the next best time.
// Plain JavaScript on purpose: tests/test_message_stamp_repair.py runs it.

export const STORED_MESSAGE_STAMPS = String.raw`
async function(ids) {
  return await new Promise((resolve) => {
    const found = {};
    const timer = setTimeout(() => resolve(found), 3000);
    const done = () => { clearTimeout(timer); resolve(found); };
    try {
      const open = indexedDB.open('model-storage');
      // Never create the database: an aborted upgrade leaves it absent.
      open.onupgradeneeded = () => open.transaction && open.transaction.abort();
      open.onerror = done;
      open.onsuccess = () => {
        const db = open.result;
        try {
          const transaction = db.transaction('message', 'readonly');
          const store = transaction.objectStore('message');
          for (const id of ids) {
            const read = store.get(id);
            read.onsuccess = () => {
              const row = read.result;
              const t = Number(row && row.t);
              if (t > 0) found[id] = { t, ack: typeof row.ack === 'number' ? row.ack : null };
            };
          }
          transaction.oncomplete = () => { db.close(); done(); };
          transaction.onerror = () => { db.close(); done(); };
          transaction.onabort = () => { db.close(); done(); };
        } catch (_error) {
          db.close();
          done();
        }
      };
    } catch (_error) {
      done();
    }
  });
}`;

function messageKey(message) {
  const id = message && message.id;
  return id && typeof id === 'object' ? id._serialized : id;
}

function hasStamp(message) {
  return Number(message && message.t) > 0 || Number(message && message.timestamp) > 0;
}

export function messagesWithoutStamp(messages) {
  return (messages || [])
    .filter((message) => message && !hasStamp(message))
    .map(messageKey)
    .filter((key) => typeof key === 'string' && key.length > 0);
}

export function applyStoredStamps(messages, stored) {
  let repaired = 0;
  for (const message of messages || []) {
    if (!message || hasStamp(message)) continue;
    const row = (stored || {})[messageKey(message)];
    const t = row ? row.t : Math.floor(Number(message.clientReceivedTsMillis || 0) / 1000);
    if (!(t > 0)) continue;
    message.t = t;
    message.timestamp = t;
    if (row && typeof row.ack === 'number' && message.ack == null) message.ack = row.ack;
    repaired++;
  }
  return repaired;
}

export async function repairMissingStamps(client, messages, logger) {
  if (!Array.isArray(messages)) return messages;
  const ids = messagesWithoutStamp(messages);
  if (!ids.length) return messages;
  let stored = {};
  try {
    stored =
      (await client.page.evaluate(
        `(${STORED_MESSAGE_STAMPS})(${JSON.stringify(ids)})`
      )) || {};
  } catch (error) {
    // The fallback below still applies; never fail the page over it.
    if (logger) logger.warn(`[get-messages] stored times unreadable: ${error}`);
  }
  const repaired = applyStoredStamps(messages, stored);
  // Counts only: an id names a chat.
  if (logger)
    logger.info(
      `[get-messages] ${ids.length} message(s) without a time; ` +
        `${Object.keys(stored).length} stored, ${repaired} repaired`
    );
  return messages;
}
