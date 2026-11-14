// Live changes to WhatsApp events, for the open conversation.
//
// WA-JS has no event for them. WhatsApp Web keeps the answers in
// WAWebEventResponseCollection.EventResponseCollection (an answer arriving or
// changing adds or changes a model whose parentMsgKey is the event's key), and
// an edit or a cancel changes the event's own Msg in place (eventName,
// eventStartTime, isEventCanceled...). Read over CDP on 2026-11-11 (2.3000.x).
//
// Changes are coalesced over half a second per event into one `event-changed`
// socket event { id } (the event message's own id): the client reads the
// answers again, or the message, and repaints its row. Only ids leave the
// page. The Neonize bridge sends the same event (neonize_bridge/events.py).

const BINDING = '__winzappOnEventChanged';
const KINDS = ['responses', 'edited'];

/** Runs inside the page; serialized by Puppeteer, so self-contained. */
export function installEventsWatchInPage(): boolean {
  const win = window as any;
  const notify = win.__winzappOnEventChanged;
  const req = (name: string): any => { try { return win.require(name); } catch (_) { return undefined; } };
  const responses = req('WAWebEventResponseCollection')?.EventResponseCollection;
  const msgs = req('WAWebCollections')?.Msg;
  if (typeof notify !== 'function' || typeof responses?.on !== 'function'
      || typeof msgs?.on !== 'function') {
    return false;
  }
  if (responses.__winzappEventsWatch) return true;
  const pending = new Map<string, string>();
  let timer: any = null;
  const send = (kind: string, key: any) => {
    try {
      const id = String(key?.id || '');
      if (!/^[A-Za-z0-9]+$/.test(id)) return;
      // an edit wins over answers in the same burst: the client reads both
      if (pending.get(id) !== 'edited') pending.set(id, kind);
      if (timer) return;
      timer = setTimeout(() => {
        timer = null;
        const changes = Array.from(pending.entries());
        pending.clear();
        for (const [event, change] of changes) notify(JSON.stringify({ kind: change, id: event }));
      }, 500);
    } catch (_) {
      // one lost change, never the listener
    }
  };
  responses.on('add change:eventResponse', (model: any) => send('responses', model?.parentMsgKey));
  msgs.on('change:eventName change:eventDescription change:eventStartTime change:eventEndTime '
    + 'change:eventLocation change:eventJoinLink change:isEventCanceled', (model: any) => {
    if (model?.type === 'event_creation') send('edited', model.id);
  });
  responses.__winzappEventsWatch = true;
  return true;
}

/** The socket payload of what the page sent, or null when it is not one. */
export function eventChangedPayload(raw: unknown, session: string) {
  if (typeof raw !== 'string' || raw.length > 256) return null;
  try {
    const value = JSON.parse(raw);
    if (!value || !KINDS.includes(value.kind) || typeof value.id !== 'string'
        || !/^[A-Za-z0-9]{6,64}$/.test(value.id)) {
      return null;
    }
    return { session, kind: value.kind, id: value.id };
  } catch {
    return null;
  }
}

export async function installEventsWatchBridge(client: any, io: any, logger: any) {
  const page = client?.page;
  if (!page) return;
  try {
    await page.exposeFunction(BINDING, (raw: unknown) => {
      const payload = eventChangedPayload(raw, client.session);
      if (payload) io.to(`session:${client.session}`).emit('event-changed', payload);
    });
  } catch {
    // Puppeteer bindings survive navigation; duplicate registration is expected.
  }
  const install = (attempt = 0) => {
    page.evaluate(installEventsWatchInPage)
      .then((installed: boolean) => {
        if (!installed && attempt < 120) {
          setTimeout(() => install(attempt + 1), 500);
        } else if (installed) {
          logger?.info?.(`[${client.session}] events watch installed`);
        } else {
          logger?.warn?.('[eventsWatchBridge] GAVE UP: answers to events will only show on reopening');
        }
      })
      .catch((e: any) => logger?.warn?.(`[eventsWatchBridge] install failed: ${e?.message || e}`));
  };
  // A page reload is a fresh JS context: the listeners go with it.
  page.on('load', () => install());
  install();
}
