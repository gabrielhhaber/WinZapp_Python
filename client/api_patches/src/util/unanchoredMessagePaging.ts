// The first page of get-messages for a chat that is not a channel: the page's
// newest window, then anchored paging backwards (see the comment in
// deviceController.getMessages for why WAPI and why anchored).
// Plain JavaScript and self-contained on purpose: page.evaluate() sends the
// function's source to the page, and tests/test_message_stamp_repair.py runs
// it under Node with a fake WAPI.getMessages.

export async function pageUnanchoredMessages({ chatId, targetCount }) {
  const keyOf = (m) =>
    (m && m.id && (m.id._serialized || m.id)) || null;
  // A quote stand-in has no t but still its receive time: without it
  // the sort put it first and the slice could drop it.
  const stampOf = (m) =>
    Number(m?.t ?? m?.timestamp ?? 0) ||
    Math.floor(Number(m?.clientReceivedTsMillis ?? 0) / 1000) ||
    0;

  const fetchBatch = async (anchor) =>
    // @ts-ignore: WAPI is a global of WhatsApp Web's page
    window.WAPI.getMessages(chatId, {
      count: targetCount,
      direction: 'before',
      id: anchor,
    });

  // First page: no anchor, so wa-js anchors on the chat's last received
  // message and hands back the newest window it can.
  let result = await fetchBatch(null);
  if (!Array.isArray(result)) return result;

  const seen = new Set();
  for (const m of result) {
    const k = keyOf(m);
    if (k) seen.add(String(k));
  }

  // Then page backwards from the oldest message we hold. Bounded by
  // maxPages as well as by targetCount: this runs inside the one
  // Puppeteer page that also serves live traffic, so an unbounded walk
  // over a huge chat would stall every other request behind it.
  let pages = 0;
  const maxPages = 10;
  while (result.length < targetCount && pages < maxPages) {
    let oldest = result[0];
    for (const m of result) {
      if (stampOf(m) < stampOf(oldest)) oldest = m;
    }
    const anchor = keyOf(oldest);
    if (!anchor) break;

    let older;
    try {
      older = await fetchBatch(String(anchor));
    } catch (e) {
      // Surfaced rather than swallowed — a silent break here is exactly
      // how the previous dead loop hid its own failure for weeks.
      console.log(
        `[browser-evaluate] paging failed for ${chatId} at anchor ${anchor}: ${e}`
      );
      break;
    }
    if (!Array.isArray(older) || older.length === 0) break;

    // Stop on a page that adds nothing new; without this an anchor that
    // keeps re-returning its own window loops until maxPages for free.
    let added = 0;
    for (const m of older) {
      const k = keyOf(m);
      if (!k || seen.has(String(k))) continue;
      seen.add(String(k));
      result.push(m);
      added++;
    }
    if (added === 0) break;
    pages++;
  }

  result.sort((a, b) => stampOf(a) - stampOf(b));
  if (result.length > targetCount) {
    // Keep the newest `targetCount` — the caller asked for a window
    // ending at "now", and the final page can overshoot.
    result = result.slice(result.length - targetCount);
  }
  console.log(
    `[browser-evaluate] getMessages ${chatId}: ${result.length} msg(s) after ${pages} extra page(s)`
  );
  return result;
}
