// Who is connected to a group call, live, for the call participants dialog and
// for saying who joined or left.
//
// WA-JS has no event for it. WhatsApp Web keeps the call being held in
// WAWebCallCollection (`activeCall`), and its model's
// `groupCallParticipantsConnected` is the list of participants connected (the
// floating call window counts and walks it: WAWebFloatingVoipRenderer.react).
// The call model is not a Backbone model: its events stay on the call and never
// reach the collection. When the list changes,
// WAWebVoipBridgeCallStateHandlers assigns it and raises
// `change:groupCallParticipantStates` on the call itself; nothing raises
// `change:groupCallParticipantsConnected` (read over CDP on 2026-11-13,
// 2.3000.x), which is what the collection was listened on before, so nobody
// joining or leaving was ever said. The collection raises `change:activeCall`
// with the new call when one becomes the call held here; each such call gets
// the listener.
//
// Each change sends the whole connected list of that call, phone form where
// WhatsApp Web knows it; the client compares it with the last one (who
// joined, who left). The Neonize bridge sends the same event from the
// roster (neonize_bridge/call_media.py). Only ids leave the page.

const BINDING = '__winzappOnCallParticipants';
const MAX_PEOPLE = 1024;

/** Runs inside the page; serialized by Puppeteer, so self-contained: a
 * module constant read in here is undefined in the page, and the
 * ReferenceError it threw inside `send` was swallowed, so nothing was ever
 * sent (MAX_PEOPLE, until 2026-11-13). */
export function installCallParticipantsInPage(): boolean {
  const win = window as any;
  const MAX_IN_PAGE = 1024;
  const notify = win.__winzappOnCallParticipants;
  const load = (name: string): any => {
    try { return typeof win.require === 'function' ? win.require(name) : null; } catch (_) { return null; }
  };
  const calls = load('WAWebCallCollection');
  if (!calls || typeof calls.on !== 'function' || typeof notify !== 'function') return false;
  if (calls.__winzappCallParticipants) return true;
  const text = (value: any) => String((value && (value._serialized || value.toString?.())) || value || '');
  const send = (call: any) => {
    try {
      if (!call || call.isGroup === false) return;
      const id = text(call.id);
      const people = call.groupCallParticipantsConnected;
      if (!id || !Array.isArray(people) || people.length > MAX_IN_PAGE) return;
      const migration = load('WAWebLidMigrationUtils');
      const connected: string[] = [];
      for (const wid of people) {
        let normalized = wid;
        try { normalized = migration?.toPn?.(wid) || wid; } catch (_) { /* keep the LID */ }
        const jid = text(normalized).replace(/@c\.us$/, '@s.whatsapp.net');
        if (/^\d+@(?:lid|s\.whatsapp\.net)$/.test(jid)) connected.push(jid);
      }
      notify(JSON.stringify({ callId: id, connected }));
    } catch (_) {
      // one lost update, never the listener
    }
  };
  // Both names: the one WhatsApp Web raises now, and the one its constants
  // also declare, should a later version raise that instead.
  const CHANGES = ['change:groupCallParticipantStates', 'change:groupCallParticipantsConnected'];
  const watched = new WeakSet<any>();
  const watch = (call: any) => {
    try {
      if (!call || typeof call !== 'object' || typeof call.on !== 'function' || watched.has(call)) return;
      watched.add(call);
      for (const event of CHANGES) call.on(event, () => send(call));
    } catch (_) {
      // a call without events is still reported when it becomes active
    }
  };
  const held = (call: any) => { watch(call); send(call); };
  calls.on('change:activeCall', held);
  calls.on('add', held);
  if (calls.activeCall) held(calls.activeCall);
  calls.__winzappCallParticipants = true;
  return true;
}

/** The socket payload of what the page sent, or null when it is not one. */
export function callParticipantsPayload(raw: unknown, session: string) {
  if (typeof raw !== 'string' || raw.length > 64 * 1024) return null;
  try {
    const value = JSON.parse(raw);
    const { callId, connected } = value || {};
    if (typeof callId !== 'string' || !callId || callId.length > 512 || !Array.isArray(connected)
        || connected.length > MAX_PEOPLE
        || !connected.every((jid: unknown) => typeof jid === 'string'
          && /^\d+@(?:lid|s\.whatsapp\.net)$/.test(jid))) {
      return null;
    }
    return { session, callId, connected };
  } catch {
    return null;
  }
}

export async function installCallParticipantsBridge(client: any, io: any, logger: any) {
  const page = client?.page;
  if (!page) return;
  try {
    await page.exposeFunction(BINDING, (raw: unknown) => {
      const payload = callParticipantsPayload(raw, client.session);
      if (payload) io.to(`session:${client.session}`).emit('call-participants', payload);
    });
  } catch {
    // Puppeteer bindings survive navigation; duplicate registration is expected.
  }
  const install = (attempt = 0) => {
    page.evaluate(installCallParticipantsInPage)
      .then((installed: boolean) => {
        if (!installed && attempt < 120) {
          setTimeout(() => install(attempt + 1), 500);
        } else if (installed) {
          logger?.info?.(`[${client.session}] call participants bridge installed`);
        } else {
          logger?.warn?.(`[callParticipantsBridge] GAVE UP after ${attempt + 1} attempts — `
            + 'who joins or leaves a group call will not be said');
        }
      })
      .catch((e: any) => logger?.warn?.(`[callParticipantsBridge] install failed: ${e?.message || e}`));
  };
  // A page reload is a fresh JS context: the listener goes with it.
  page.on('load', () => install());
  install();
}
