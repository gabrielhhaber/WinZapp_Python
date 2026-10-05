/*
 * In-page runtime for POST /forward-messages (see deviceController.forwardMessages).
 *
 * wa-js's WPP.chat.forwardMessages() does `await ensureLazyModule(
 * 'WAWebChatForwardMessage')` and then needs its own `functions.forwardMessages`
 * binding. On WhatsApp Web 2.3000.1049007170 that fails with
 * forward_messages_not_available: the module is REGISTERED but cannot be
 * required ("Requiring module ... with unresolved dependencies"), because one
 * of its dependencies (WAWebDualUploadsSendPolicy) lives in a script that the
 * Bootloader lists among the page's resources and nobody loaded. wa-js stops
 * as soon as the module is registered, so it never fetches that script, and
 * its binding, made by module NAME, never happens late either.
 * docs/traps/whatsapp-web-version-pin.md has the measurements.
 *
 * So the function below (1) loads the unloaded Bootloader resources of the
 * components that carry the module and (2) calls WhatsApp's own
 * forwardMessages({chat, msgs, ...}) directly. The heal half is generic (module
 * name + component list): it is the remedy for wa-js's ensureLazyModule
 * weakness in general, only forwarding is wired to it for now.
 *
 * It is ONE self-contained plain-JS function body (no TypeScript, no closure
 * over Node variables, no backticks and no dollar-brace sequences), kept as a
 * string so that exactly this text runs in the page, in the Python source
 * contract tests and over CDP.
 *
 * Contract: resolves { ok: true, response } or { ok: false, detail }. ok:false
 * is ONLY "the module could not be made available" and nothing was sent. A
 * rejected forward throws and is never retried: the caller must not send twice.
 * With args.keepVoice the success also carries voice: { asked, kept }, the
 * voice messages in the batch and how many of them stayed voice messages.
 */

export const FORWARD_RUNTIME_SOURCE = String.raw`async function (args) {
  const win = window;
  const moduleName = (args && args.moduleName) || 'WAWebChatForwardMessage';
  const fixedComponents = (args && args.components) || [
    'WAWebMediaForwardMediaMsg',
    'WAWebForwardMessageFlow.react',
    'WAWebForwardMessageModal.react',
  ];
  const maxComponents = 12;
  const perScriptMs = 15000;
  const budgetMs = (args && args.budgetMs) || 10000;
  const chatId = args && args.chatId;
  const messageIds = (args && args.messageIds) || [];

  let lastRequireError = '';
  const tryRequire = (name) => {
    try {
      const mod = win.require(name);
      lastRequireError = '';
      return mod;
    } catch (e) {
      const params = e && e.messageParams;
      lastRequireError = String(
        (params && params[1]) || (e && e.message) || e
      ).slice(0, 400);
      return null;
    }
  };
  const forwardModule = () => {
    const mod = tryRequire(moduleName);
    return mod && typeof mod.forwardMessages === 'function' ? mod : null;
  };
  const get = (coll, key) => {
    if (!coll) return undefined;
    return typeof coll.get === 'function' ? coll.get(key) : coll[key];
  };
  const entries = (coll) => {
    if (!coll) return [];
    if (typeof coll.keys === 'function') return Array.from(coll.keys());
    return Object.keys(coll);
  };
  const usableUrl = (value) => {
    // Only the WhatsApp static CDN and only scripts: never css, never another host.
    const raw = typeof value === 'string' ? value : value && (value.src || value.url);
    try {
      const url = new URL(String(raw));
      if (
        url.protocol === 'https:' &&
        url.hostname === 'static.whatsapp.net' &&
        url.port === '' &&
        !url.username &&
        !url.password &&
        url.pathname.endsWith('.js')
      ) {
        return url.href;
      }
    } catch (_) {
      // not a URL
    }
    return null;
  };
  const loadScript = (src, timeoutMs) =>
    new Promise((resolve) => {
      const script = document.createElement('script');
      let timer = null;
      const done = (ok) => {
        clearTimeout(timer);
        script.onload = null;
        script.onerror = null;
        resolve(ok);
      };
      script.onload = () => done(true);
      script.onerror = () => done(false);
      timer = setTimeout(() => done(false), timeoutMs);
      script.async = true;
      script.src = src;
      document.head.appendChild(script);
    });

  const notes = [];
  // Idempotent and shared: concurrent forwards wait on one heal, and a heal
  // that worked is remembered for the life of the page.
  const heal = async () => {
    const state = (win.__winzappLazyHeal = win.__winzappLazyHeal || {});
    if (state[moduleName] && state[moduleName].done) return true;
    if (!state[moduleName] || !state[moduleName].promise) {
      state[moduleName] = {
        done: false,
        promise: (async () => {
          let bootloader = tryRequire('Bootloader');
          bootloader = bootloader && bootloader.default ? bootloader.default : bootloader;
          const dbg = bootloader && bootloader.__debug;
          if (!dbg) {
            notes.push('no Bootloader.__debug');
            return false;
          }
          const names = [];
          for (const name of fixedComponents) {
            if (get(dbg.componentMap, name) !== undefined) names.push(name);
          }
          for (const name of entries(dbg.componentMap)) {
            if (names.length >= maxComponents) break;
            if (/forward/i.test(name) && names.indexOf(name) < 0) names.push(name);
          }
          const isLoaded = (hash) => {
            const loaded = dbg.loaded;
            if (loaded) {
              if (typeof loaded.has === 'function' ? loaded.has(hash) : loaded[hash]) {
                return true;
              }
            }
            const res = get(dbg.resources, hash);
            return Boolean(res && typeof res === 'object' && res.loaded === true);
          };
          const urls = [];
          for (const name of names) {
            const comp = get(dbg.componentMap, name);
            const hashes = Array.isArray(comp) ? comp : (comp && comp.r) || [];
            for (const hash of hashes) {
              if (isLoaded(hash)) continue;
              const url = usableUrl(get(dbg.resources, hash));
              if (!url) {
                notes.push('skipped resource ' + String(hash));
                continue;
              }
              if (urls.indexOf(url) < 0) urls.push(url);
            }
          }
          const startedAt = Date.now();
          for (const url of urls) {
            const left = budgetMs - (Date.now() - startedAt);
            if (left <= 0) {
              notes.push('budget exhausted');
              break;
            }
            const ok = await loadScript(url, Math.min(perScriptMs, left));
            notes.push((ok ? 'loaded ' : 'failed ') + url);
            if (forwardModule()) return true;
          }
          return Boolean(forwardModule());
        })(),
      };
    }
    const ok = await state[moduleName].promise;
    if (ok) {
      state[moduleName].done = true;
    } else {
      delete state[moduleName];
    }
    return ok;
  };

  // The response crosses back to Node by value. A raw result that cannot be
  // serialized would fail the evaluate AFTER the send, i.e. a 500 and a retry
  // from Python that forwards the same messages again.
  const serializable = (value) => {
    try {
      return JSON.parse(JSON.stringify(value));
    } catch (_) {
      return null;
    }
  };

  // args.keepVoice: a forwarded voice message stays a voice message.
  //
  // WhatsApp Web itself turns it into a plain audio. forwardMediaMsg(), in
  // WAWebMediaForwardMediaMsg, reads q = msg.mediaData.toJSON() and then sets
  // q.type = 'audio' unless the message came from a channel - so a forwarded
  // 'ptt' is something WhatsApp does send and its apps do show. Measured over
  // CDP on 2026-10-04: the copy arrived as type ptt, isForwarded true,
  // forwardingScore 1, duration and waveform intact.
  //
  // For the length of ONE forward, toJSON() of that one message hands out
  // data whose type refuses that single write. Everything else is still
  // WhatsApp's own forward. If WhatsApp stops converting this way the guard
  // simply never fires and the message goes out as audio, as it always did:
  // the guard cannot fail a send, and voice.kept says whether it worked.
  const keepVoice = Boolean(args && args.keepVoice);
  const guards = (win.__winzappVoiceGuards = win.__winzappVoiceGuards || new WeakMap());
  const guardVoiceType = (msg, voice) => {
    const media = msg && msg.type === 'ptt' ? msg.mediaData : null;
    if (!media || typeof media.toJSON !== 'function') return null;
    let guard = guards.get(media);
    if (!guard) {
      // One guard per message, shared by overlapping forwards of it and
      // removed by the last one out: two independent wrappers would each
      // restore the other's function, one of them for good.
      guard = {
        users: 0,
        refused: 0,
        own: Object.prototype.hasOwnProperty.call(media, 'toJSON'),
        original: media.toJSON,
      };
      guards.set(media, guard);
      const active = guard;
      media.toJSON = function () {
        const data = active.original.apply(this, arguments);
        if (data && data.type === 'ptt') {
          let type = 'ptt';
          try {
            Object.defineProperty(data, 'type', {
              enumerable: true,
              configurable: true,
              get: function () {
                return type;
              },
              set: function (value) {
                if (value === 'audio') {
                  active.refused += 1;
                  return;
                }
                type = value;
              },
            });
          } catch (_) {
            // Data that cannot be redefined (sealed, frozen): hand it out as
            // WhatsApp made it. The message goes as audio; nothing fails.
          }
        }
        return data;
      };
    }
    const held = guard;
    const refusedBefore = held.refused;
    held.users += 1;
    voice.asked += 1;
    return function () {
      if (held.refused > refusedBefore) voice.kept += 1;
      held.users -= 1;
      if (held.users > 0) return;
      if (held.own) media.toJSON = held.original;
      else delete media.toJSON;
      guards.delete(media);
    };
  };

  const run = async () => {
    const WPP = win.WPP;
    let mod = forwardModule();
    const bound = Boolean(
      WPP && WPP.whatsapp && WPP.whatsapp.functions &&
        typeof WPP.whatsapp.functions.forwardMessages === 'function'
    );
    const voice = { asked: 0, kept: 0 };
    const releases = [];
    const models = async () => {
      const found = [];
      for (const id of messageIds) found.push(await WPP.chat.getMessageById(id));
      if (keepVoice) {
        for (const msg of found) {
          const release = guardVoiceType(msg, voice);
          if (release) releases.push(release);
        }
      }
      return found;
    };
    const releaseAll = () => {
      // Runs after the send. A throw from here would turn a forward that
      // WENT OUT into a 500, and Python posts the same forward again.
      while (releases.length) {
        const release = releases.pop();
        try {
          release();
        } catch (_) {
          // a guard that could not be taken off changes nothing about the send
        }
      }
    };
    const sent = (response) => {
      const outcome = { ok: true, response: serializable(response) };
      if (keepVoice) outcome.voice = voice;
      return outcome;
    };
    if (mod && bound) {
      // A build that works: exactly what the library call does. With a voice
      // message to keep, the library is handed the guarded models themselves:
      // by id it may build a second model of a message that is not in the
      // store, and that one would carry no guard.
      const guarded = keepVoice ? await models() : null;
      let libResponse;
      try {
        libResponse = await WPP.chat.forwardMessages(
          chatId,
          releases.length ? guarded : messageIds
        );
      } finally {
        releaseAll();
      }
      return sent(libResponse);
    }
    if (!mod) {
      await heal();
      mod = forwardModule();
    }
    if (!mod) {
      return {
        ok: false,
        detail:
          'forwardMessages unavailable: require(' + moduleName + ') failed: ' +
          (lastRequireError || 'no forwardMessages export') +
          (notes.length ? ' [' + notes.join('; ') + ']' : ''),
      };
    }
    const chat = await WPP.chat.find(chatId);
    const msgs = await models();
    // The single send. Whatever it throws propagates: no retry, no second
    // path. The finally only takes the voice guards back off.
    let response;
    try {
      response = await mod.forwardMessages({
        chat: chat,
        msgs: msgs,
        multicast: false,
        includeCaption: false,
        appendedText: false,
      });
    } finally {
      releaseAll();
    }
    return sent(response);
  };

  // Python gives up on its POST after 20 s and posts the same forward again
  // while this evaluate is still running. The second request must wait for
  // the first one's result, never send. The key is released on any outcome so
  // a later, deliberate forward of the same messages is not blocked.
  const inflight = (win.__winzappForwardInflight = win.__winzappForwardInflight || {});
  const key = String(chatId) + '|' + messageIds.join(',') + (keepVoice ? '|voice' : '');
  if (inflight[key]) return await inflight[key];
  const pending = run();
  inflight[key] = pending;
  try {
    return await pending;
  } finally {
    delete inflight[key];
  }
}`;

export function buildForwardRuntimeExpression(args: {
  chatId: string;
  messageIds: string[];
  keepVoice?: boolean;
}): string {
  return `(${FORWARD_RUNTIME_SOURCE})(${JSON.stringify(args)})`;
}
