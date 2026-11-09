// Live changes to the channels the account follows, for the Channels window.
//
// WA-JS has no event for them. WhatsApp Web keeps the followed channels in
// WAWebCollections.WAWebNewsletterCollection: following one adds its model,
// leaving one removes it, and muting changes its `muteExpiration`; a role
// change (made an admin, the owner changed) is `membershipType` on its model
// in WAWebNewsletterMetadataCollection, and so are its `name` and
// `description` when an admin edits them. All of it also when done on the
// phone or another device; `adminCount` there changes with the channel's
// admins. Read over CDP on 2026-11-08 (2.3000.x).
//
// Every change becomes one `channels-changed` socket event, coalesced over a
// second (a page load adds every followed channel at once). Only the
// channel's id leaves the page. The Neonize bridge sends the same event from
// neonize's NewsletterJoin/Leave/MuteChange (neonize_bridge/channel_events.py).

const BINDING = '__winzappOnChannelsChanged';
const KINDS = ['joined', 'left', 'muted', 'role', 'edited', 'admins'];

/** Runs inside the page; serialized by Puppeteer, so self-contained. */
export function installChannelsWatchInPage(): boolean {
  const win = window as any;
  const notify = win.__winzappOnChannelsChanged;
  let collections: any = null;
  try {
    collections = typeof win.require === 'function' ? win.require('WAWebCollections') : null;
  } catch (_) {
    collections = null;
  }
  const channels = collections && collections.WAWebNewsletterCollection;
  const metadata = collections && collections.WAWebNewsletterMetadataCollection;
  if (!channels || typeof channels.on !== 'function' || typeof notify !== 'function') {
    return false;
  }
  if (channels.__winzappChannelsWatch) return true;
  let pending: any = null;
  let timer: any = null;
  const send = (kind: string, model: any) => {
    try {
      const id = String((model && model.id && (model.id._serialized || model.id)) || '');
      if (!/^\d+@newsletter$/.test(id)) return;
      // the last change of the burst is the one reported; the window reads
      // the whole list again anyway
      pending = { kind, id };
      if (timer) return;
      timer = setTimeout(() => {
        timer = null;
        const change = pending;
        pending = null;
        if (change) notify(JSON.stringify(change));
      }, 1000);
    } catch (_) {
      // one lost event, never the listener
    }
  };
  channels.on('add', (model: any) => send('joined', model));
  channels.on('remove', (model: any) => send('left', model));
  channels.on('change:muteExpiration', (model: any) => send('muted', model));
  if (metadata && typeof metadata.on === 'function') {
    metadata.on('change:membershipType', (model: any) => send('role', model));
    metadata.on('change:name change:description', (model: any) => send('edited', model));
    // an admin promoted or gone, an invite accepted, a new owner (the
    // followers have no event: WhatsApp Web reads them with the info open)
    metadata.on('change:adminCount', (model: any) => send('admins', model));
  }
  channels.__winzappChannelsWatch = true;
  return true;
}

/** The socket payload of what the page sent, or null when it is not one. */
export function channelsChangedPayload(raw: unknown, session: string) {
  if (typeof raw !== 'string' || raw.length > 256) return null;
  try {
    const value = JSON.parse(raw);
    if (!value || !KINDS.includes(value.kind) || typeof value.id !== 'string'
        || !/^\d+@newsletter$/.test(value.id)) {
      return null;
    }
    return { session, kind: value.kind, id: value.id };
  } catch {
    return null;
  }
}

export async function installChannelsWatchBridge(client: any, io: any, logger: any) {
  const page = client?.page;
  if (!page) return;
  try {
    await page.exposeFunction(BINDING, (raw: unknown) => {
      const payload = channelsChangedPayload(raw, client.session);
      if (payload) io.to(`session:${client.session}`).emit('channels-changed', payload);
    });
  } catch {
    // Puppeteer bindings survive navigation; duplicate registration is expected.
  }
  const install = (attempt = 0) => {
    page.evaluate(installChannelsWatchInPage)
      .then((installed: boolean) => {
        if (!installed && attempt < 120) {
          setTimeout(() => install(attempt + 1), 500);
        } else if (installed) {
          logger?.info?.(`[${client.session}] channels watch installed`);
        } else {
          logger?.warn?.(`[channelsWatchBridge] GAVE UP after ${attempt + 1} attempts — `
            + 'the Channels window will only update on Refresh');
        }
      })
      .catch((e: any) => logger?.warn?.(`[channelsWatchBridge] install failed: ${e?.message || e}`));
  };
  // A page reload is a fresh JS context: the listener goes with it.
  page.on('load', () => install());
  install();
}
