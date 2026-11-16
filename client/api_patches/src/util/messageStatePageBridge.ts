// Bounded, account-owned delivery for independent message-state observers.
const pages = new WeakMap();

export async function installMessageStatePageBridge(client, io, logger, binding,
  eventName, installInPage, payloadOf) {
  const page = client?.page;
  if (!page) return;
  let bindings = pages.get(page);
  if (!bindings) { bindings = new Map(); pages.set(page, bindings); }
  let owner = bindings.get(binding);
  if (owner) {
    Object.assign(owner, { client, io, logger, installInPage, payloadOf });
    if (typeof owner.install === 'function') owner.install();
    return;
  }
  owner = { client, io, logger, installInPage, payloadOf,
    closed: false, generation: 0, timer: null, install: null };
  bindings.set(binding, owner);
  try {
    await page.exposeFunction(binding, (raw) => {
      if (owner.closed) return;
      const payload = owner.payloadOf(raw, owner.client.session);
      if (payload) owner.io.to(`session:${owner.client.session}`).emit(eventName, payload);
    });
  } catch (_) { /* a Puppeteer binding can survive reinstallation */ }
  const install = (attempt = 0, generation = owner.generation) => {
    if (owner.closed || generation !== owner.generation) return;
    clearTimeout(owner.timer);
    page.evaluate(owner.installInPage).then((installed) => {
      if (owner.closed || generation !== owner.generation) return;
      if (!installed && attempt < 120) owner.timer = setTimeout(() => install(attempt + 1, generation), 500);
      else if (!installed) owner.logger?.warn?.(`[${eventName}] unavailable this session`);
    }).catch(() => {
      if (!owner.closed && generation === owner.generation)
        owner.logger?.warn?.(`[${eventName}] page observation failed`);
    });
  };
  owner.install = install;
  page.on('load', () => { owner.generation += 1; install(); });
  page.on('close', () => { owner.closed = true; clearTimeout(owner.timer); });
  install();
}
