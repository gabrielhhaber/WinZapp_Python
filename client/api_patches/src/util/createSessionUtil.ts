/*
 * Copyright 2021 WPPConnect Team
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
import { create, SocketState, StatusFind } from '@wppconnect-team/wppconnect';
import { exec, execFile, execSync } from 'child_process';
import { Request } from 'express';

import { download } from '../controller/sessionController';
import { WhatsAppServer } from '../types/WhatsAppServer';
import chatWootClient from './chatWootClient';
import { autoDownload, callWebHook, startHelper } from './functions';
import { clientsArray, eventEmitter } from './sessionUtil';
import Factory from './tokenStore/factory';

/**
 * Kill the actual Puppeteer/Chrome browser process (and its tree of child
 * renderer/GPU processes) behind `page`, given a live handle to it.
 *
 * This is the precise, preferred kill path — used whenever a `page` is
 * actually available (e.g. closeSession() on an already-created session).
 * `page.browser().process()` returns Node's own ChildProcess for a
 * locally-launched browser; Node's `ChildProcess.kill()` alone only signals
 * that one process on Windows (Windows has no process-group SIGKILL
 * cascade), so this shells out to `taskkill /T` for the actual tree-kill —
 * the same thing @puppeteer/browsers' own Process.kill() does internally.
 * Falls back to forceKillByUserDataDir() (below) when no page/pid is
 * available, e.g. mid-pairing, before create() has returned.
 */
function forceKillBrowserProcess(page: any, logger?: any): boolean {
  let pid: number | undefined;
  try {
    pid = page?.browser?.()?.process?.()?.pid;
    if (!pid) return false;
    if (process.platform === 'win32') {
      execSync(`taskkill /pid ${pid} /T /F`, { stdio: 'ignore' });
    } else {
      process.kill(pid, 'SIGKILL');
    }
    logger?.info?.(
      `[forceKillBrowserProcess] Killed browser process ${pid} and its tree`
    );
    return true;
  } catch (e: any) {
    logger?.warn?.(
      `[forceKillBrowserProcess] Failed to kill browser process ${pid}: ${
        e?.message || e
      }`
    );
    return false;
  }
}

/**
 * Best-effort fallback: kill whatever process (Chrome + its children) has
 * this session's userDataDir on its command line, used when there is no
 * live `page`/`pid` handle to kill precisely (e.g. shouldClose fires inside
 * catchQR/catchLinkCode, which run synchronously *during* `create()` —
 * before the `wppClient` it returns is even assigned, so nothing in this
 * file can reach the browser directly yet).
 *
 * This used to be `exec('pkill -9 -f "<userDataDir>"')` unconditionally.
 * `pkill` does not exist on Windows at all — Node's child_process.exec
 * spawns it via cmd.exe, which can't find the command, and the callback
 * here discarded the error — so on Windows this silently killed nothing,
 * ever. That's the root cause behind WinZapp's WhatsApp Web session
 * occasionally coming back "logged out" after exiting: closeSession()'s
 * fast path (see sessionController.ts) skips the graceful
 * `await client.close()` whenever the session isn't fully CONNECTED and
 * relies entirely on this function instead — silently doing nothing left
 * Chrome running, so WinZapp's own exit routine eventually had to
 * `taskkill /F /T` the whole Node process tree once its grace period ran
 * out, tearing down Chrome's profile (a LevelDB store) mid-write.
 */
function forceKillByUserDataDir(userDataDir: string, logger?: any) {
  if (!userDataDir) return;
  if (process.platform === 'win32') {
    // Windows has no built-in "kill by command-line substring" either, so
    // this uses PowerShell's CIM/WMI process query — the closest equivalent
    // to `pkill -f` available without a third-party dependency.
    //
    // Critical gotcha: the filter text ends up embedded verbatim in this
    // very powershell.exe's own `-Command` argument, so its own CommandLine
    // always matches the `-like` pattern too. Without excluding $PID, the
    // query finds and Stop-Process's *itself* mid-script on every single
    // invocation — it never reaches the process it was meant to kill, exits
    // with a bare non-zero code and no stdout/stderr, and the real stale
    // Chrome process (holding the userDataDir profile lock) survives. That
    // silent 100%-failure is exactly what let a locked profile force a full
    // API/Node restart to clear on the next pairing attempt.
    //
    // Second half of the same gotcha: the pattern has to be separator- and
    // shortname-agnostic. Both call sites below pass `userDataDir/<session>`
    // with a FORWARD slash, but that value never reaches Chrome verbatim —
    // createSessionUtil hands puppeteer the relative `./userDataDir/<session>`
    // and ChromeLauncher resolves it (`path.resolve()`) before building the
    // argument, so the process's real CommandLine reads
    // `--user-data-dir=C:\<install path>\api\userDataDir\<session>`:
    // backslashes, and possibly 8.3-shortened components anywhere in the
    // install path (`PROGRA~1`-style) when it has spaces or long names.
    // PowerShell's `-like` treats `\` and `/` as ordinary, non-interchangeable
    // characters, so the forward-slash filter matched the real Chrome process
    // exactly zero times — the only process it ever matched was the
    // powershell.exe running the query itself, whose `-Command` argument does
    // contain the forward-slash text verbatim. That is why the $PID exclusion
    // above, on its own, only turns the self-kill into a silent no-op.
    // Collapsing every run of separators into a `*` wildcard matches just the
    // tail of the path, which is immune to both the separator flavour and to
    // 8.3 shortening of the parent directories. The `-like` metacharacters are
    // backtick-escaped first (a backtick survives a single-quoted PowerShell
    // string literal and is exactly what `-like` reads as "literal next
    // character"), so a session id is never mistaken for a wildcard.
    const psFilter = userDataDir
      .replace(/'/g, "''")
      .replace(/[`*?[\]]/g, '`$&')
      .replace(/[\\/]+/g, '*');
    const script =
      `$mypid = $PID; ` +
      `Get-CimInstance Win32_Process | ` +
      `Where-Object { $_.ProcessId -ne $mypid -and $_.CommandLine -like '*${psFilter}*' } | ` +
      `ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }`;
    execFile(
      'powershell.exe',
      ['-NoProfile', '-NonInteractive', '-Command', script],
      (err) => {
        if (err) {
          logger?.warn?.(
            `[forceKillByUserDataDir] PowerShell kill failed: ${err.message}`
          );
        }
      }
    );
  } else {
    exec(`pkill -9 -f "${userDataDir}"`, () => {});
  }
}

/**
 * Restore `MsgKey.prototype._serialized` inside the WhatsApp Web page.
 *
 * WhatsApp Web's minified build renamed the `_serialized` getter on MsgKey to
 * `$1` (the Wid class, used for chat/contact ids, was NOT affected — only
 * MsgKey). WPPConnect's serializer is hardcoded against the old name:
 *
 *     WAPI._serializeMessageObj = e => Object.assign(
 *       WAPI._serializeRawObj(e), { id: e.id._serialized, ... })
 *
 * so `id` became `undefined` and JSON.stringify dropped the property outright.
 * Every message returned by get-messages arrived with no id at all, WinZapp
 * normalized it to `key.id = ""`, and DatabaseManager dropped 100% of them as
 * id-less (a chat list with unread counts over a database with zero messages).
 * `quotedMsgId` and `quotedParticipant` were silently lost the same way.
 *
 * The getter below reads `$1` when present and otherwise rebuilds the id from
 * the MsgKey's own fields, which is exactly the format WhatsApp itself uses
 * (`false_120363127023984493@g.us_AC9249DF…`). That second path is what keeps
 * this working if a future build renames `$1` again.
 */
async function restoreMsgKeySerialized(
  page: any,
  logger: any,
  session: string
) {
  if (!page) return;
  try {
    const result = await page.evaluate(() => {
      const install = () => {
        const MK = (window as any).WPP?.whatsapp?.MsgKey;
        // wa-js is injected asynchronously after each navigation, so on the
        // page-reload path MsgKey usually does not exist yet on the first try.
        if (!MK || !MK.prototype) return false;
        // `in` walks the prototype chain — an unaffected build already has it.
        if ('_serialized' in MK.prototype) return true;
        Object.defineProperty(MK.prototype, '_serialized', {
          configurable: true,
          get() {
            if (typeof this.$1 === 'string' && this.$1) return this.$1;
            const jid = (w: any) =>
              typeof w === 'string' ? w : w?._serialized ?? w?.$1 ?? '';
            const remote = jid(this.remote);
            if (!remote || !this.id) return '';
            const participant = jid(this.participant);
            return (
              `${!!this.fromMe}_${remote}_${this.id}` +
              (participant ? `_${participant}` : '')
            );
          },
        });
        return true;
      };
      if (install()) return 'installed';
      // Retry in the background rather than awaiting here: blocking the
      // evaluate would stall session startup for as long as wa-js takes.
      let tries = 0;
      const timer = setInterval(() => {
        if (install() || ++tries > 60) clearInterval(timer);
      }, 500);
      return 'scheduled (wa-js not ready yet)';
    });
    logger.info(`[${session}] MsgKey._serialized shim: ${result}`);
  } catch (e: any) {
    // Never fatal: without the shim messages lose their ids, but the session
    // itself is still perfectly usable.
    logger.error(
      `[${session}] Failed to install MsgKey._serialized shim: ${
        e?.message || e
      }`
    );
  }
}

/**
 * Adapt WA-JS's status sender to WhatsApp Web's current object argument.
 *
 * WA-JS still calls encryptAndSendStatusMsg(msg, proto, reporters), while the
 * current WhatsApp module expects { sendMsgRecord, msgProtobuf,
 * metricsReporter }. The old call throws inside the page, WA-JS swallows it,
 * and the API only sees messageSendResult=ERROR_UNKNOWN.
 */
async function restoreStatusSender(page: any, logger: any, session: string) {
  if (!page) return;
  try {
    const result = await page.evaluate(() => {
      const install = () => {
        const wpp = (window as any).WPP;
        if (!wpp?.loader?.moduleRequire) return false;
        if ((window as any).__winzappStatusSenderInstalled) return true;

        try {
          const sendModule = wpp.loader.moduleRequire('WAWebSendMsgJob');
          const statusModule = wpp.loader.moduleRequire(
            'WAWebEncryptAndSendStatusMsg'
          );
          const protoModule = wpp.loader.moduleRequire(
            'WAWebE2EProtoGenerator'
          );
          const original = sendModule?.encryptAndSendMsg;
          const sendStatus = statusModule?.encryptAndSendStatusMsg;

          // The legacy function accepts positional arguments and needs no shim.
          if (
            typeof original !== 'function' ||
            typeof sendStatus !== 'function' ||
            typeof protoModule?.createMsgProtobuf !== 'function' ||
            sendStatus.length !== 1
          ) {
            return false;
          }

          sendModule.encryptAndSendMsg = async function (
            sendMsgRecord: any,
            metricsReporter: any,
            ...additionalArgs: any[]
          ) {
            if (
              sendMsgRecord?.data?.to?.toString?.() !== 'status@broadcast'
            ) {
              return original.apply(this, [
                sendMsgRecord,
                metricsReporter,
                ...additionalArgs,
              ]);
            }

            await sendStatus({
              sendMsgRecord,
              msgProtobuf: protoModule.createMsgProtobuf(sendMsgRecord.data),
              metricsReporter,
            });

            return {
              t: sendMsgRecord.data.t,
              sync: null,
              phash: null,
              addressingMode: null,
              count: null,
              error: null,
            };
          };

          (window as any).__winzappStatusSenderInstalled = true;
          return true;
        } catch (e) {
          return false;
        }
      };

      if (install()) return 'installed';
      let tries = 0;
      const timer = setInterval(() => {
        if (install() || ++tries > 60) clearInterval(timer);
      }, 500);
      return 'scheduled (WhatsApp modules not ready yet)';
    });
    logger.info(`[${session}] status sender shim: ${result}`);
  } catch (e: any) {
    logger.error(
      `[${session}] Failed to install status sender shim: ${e?.message || e}`
    );
  }
}

/**
 * Give WhatsApp Web a durable storage bucket.
 *
 * In a headless, freshly-created Chrome profile the persistent-storage
 * permission defaults to 'prompt', and since there is nobody to answer a
 * prompt Chrome resolves it to a denial: navigator.storage.persist() returns
 * false and WhatsApp Web logs
 *   [storage] storage bucket persistence denied (aquire-persistent-storage-denied)
 * A non-durable bucket still works — it is merely evictable under storage
 * pressure — so this is a robustness fix, not the history-sync fix. (What
 * actually stalled history sync was the blanket request interception hanging
 * the backend worker's cross-origin imports; see the long note in start.js.)
 *
 * Two details, both measured against this exact Chrome build rather than
 * assumed, because getting either wrong leaves the grant silently ineffective:
 *
 *   * The CDP session must stay attached. `Browser.grantPermissions` is a
 *     session-scoped override — detaching resets the origin straight back to
 *     'prompt'. The earlier version of this function called cdp.detach()
 *     immediately after granting, which is why the log kept reporting
 *     permission 'prompt' and persisted false even though the grant itself
 *     succeeded. The session is parked on the page so it outlives this call.
 *
 *   * No puppeteer overridePermissions() here. That call replaces the whole
 *     granted set for the origin, so asking for ['notifications'] flips
 *     durableStorage from 'prompt' to 'denied' — measurably worse than doing
 *     nothing. The CDP grant below covers notifications anyway.
 *
 * Best-effort throughout: never throw from here.
 */
async function grantPersistentStorage(page: any, logger: any, session: string) {
  if (!page) return;
  const origin = 'https://web.whatsapp.com';
  try {
    // durableStorage has no puppeteer-level name, so it goes over raw CDP.
    // Reuse one session per page: a fresh one per navigation would be fine,
    // but each detach would revoke what the previous one granted.
    if (!page.__wzPermissionSession) {
      page.__wzPermissionSession = await page.createCDPSession();
    }
    await page.__wzPermissionSession.send('Browser.grantPermissions', {
      origin,
      permissions: ['durableStorage', 'notifications'],
    });
  } catch (e: any) {
    page.__wzPermissionSession = null;
    logger?.warn?.(
      `[${session}] Browser.grantPermissions failed: ${e?.message || e}`
    );
  }
  try {
    const state = await page.evaluate(async () => {
      const out: any = {};
      out.notificationApi = typeof (globalThis as any).Notification;
      try {
        out.permission = (
          await navigator.permissions.query({
            name: 'persistent-storage' as PermissionName,
          })
        ).state;
      } catch (e: any) {
        out.permission = `query-failed: ${e?.message || e}`;
      }
      try {
        out.persisted = await navigator.storage.persisted();
        if (!out.persisted) out.granted = await navigator.storage.persist();
        out.persistedAfter = await navigator.storage.persisted();
      } catch (e: any) {
        out.persistError = String(e?.message || e);
      }
      return out;
    });
    // Logged at info even on success: this single line is the fastest way to
    // tell a session that can ingest history from one that silently cannot.
    logger?.info?.(`[${session}] persistent storage: ${JSON.stringify(state)}`);
    if (!state?.persistedAfter) {
      logger?.warn?.(
        `[${session}] WhatsApp Web did NOT get a persistent storage bucket — ` +
          `its database stays evictable, so Chrome may drop synced history ` +
          `under storage pressure.`
      );
    }
  } catch (e: any) {
    logger?.warn?.(
      `[${session}] Could not verify persistent storage: ${e?.message || e}`
    );
  }
}

export default class CreateSessionUtil {
  forceKillSession(session: string, logger?: any) {
    const client: any = clientsArray[session];
    if (!forceKillBrowserProcess(client?.page, logger)) {
      forceKillByUserDataDir(`userDataDir/${session}`, logger);
    }
  }

  startChatWootClient(client: any) {
    if (client.config.chatWoot && !client._chatWootClient)
      client._chatWootClient = new chatWootClient(
        client.config.chatWoot,
        client.session
      );
    return client._chatWootClient;
  }

  async createSessionUtil(
    req: any,
    clientsArray: any,
    session: string,
    res?: any
  ) {
    try {
      let client = this.getClient(session) as any;
      if (client.status != null && client.status !== 'CLOSED') return;
      client.status = 'INITIALIZING';
      client.config = req.body;

      const tokenStore = new Factory();
      const myTokenStore = tokenStore.createTokenStory(client);
      const tokenData = await myTokenStore.getToken(session);

      // we need this to update phone in config every time session starts, so we can ask for code for it again.
      //
      // WinZapp patch: only rewrite the token when we actually read one back.
      // Upstream writes `tokenData ?? {}`, so ANY failure to read the stored
      // token — a transient fs error, a file still locked by a previous
      // instance that was force-killed, a partially-written JSON — is
      // immediately made permanent by overwriting it with an empty object.
      // The saved WhatsApp Web credentials are then gone for good and the next
      // start looks like a logout, even though the phone still lists the linked
      // device. Reading nothing is not a reason to destroy what is on disk.
      if (tokenData) {
        myTokenStore.setToken(session, tokenData);
      } else {
        req.logger?.warn?.(
          `[${session}] Token store returned no data — leaving the stored token untouched.`
        );
      }

      this.startChatWootClient(client);

      if (req.serverOptions.customUserDataDir) {
        req.serverOptions.createOptions.puppeteerOptions = Object.assign(
          { protocolTimeout: 300000 },
          req.serverOptions.createOptions.puppeteerOptions || {},
          { userDataDir: req.serverOptions.customUserDataDir + session }
        );
      } else {
        req.serverOptions.createOptions.puppeteerOptions = Object.assign(
          { protocolTimeout: 300000 },
          req.serverOptions.createOptions.puppeteerOptions || {}
        );
      }

      // Best-effort kill for the shouldClose branches below. `wppClient` is
      // only assigned once `create()` actually returns — catchLinkCode/
      // catchQR/statusFind run synchronously *during* create() itself
      // (before that assignment), so referencing `wppClient` there is a
      // temporal-dead-zone ReferenceError, silently swallowed by their
      // existing `try { wppClient.close() } catch {}`. This has the same
      // problem and stays wrapped for the same reason, but tries the
      // precise process-tree kill first and only falls back to the
      // userDataDir scan when no live page/pid is reachable yet.
      const killBrowserOrFallback = () => {
        let killed = false;
        try {
          killed = forceKillBrowserProcess(wppClient?.page, req.logger);
        } catch (e) {}
        if (!killed)
          forceKillByUserDataDir(`userDataDir/${session}`, req.logger);
      };

      const wppClient = await create(
        Object.assign(
          {},
          { tokenStore: myTokenStore },
          client.config.proxy
            ? {
                proxy: {
                  url: client.config.proxy?.url,
                  username: client.config.proxy?.username,
                  password: client.config.proxy?.password,
                },
              }
            : {},
          req.serverOptions.createOptions,
          {
            session: session,
            phoneNumber: client.config.phone ?? null,
            deviceName:
              client.config.phone == undefined // bug when using phone code this shouldn't be passed (https://github.com/wppconnect-team/wppconnect-server/issues/1687#issuecomment-2099357874)
                ? client.config?.deviceName ||
                  req.serverOptions.deviceName ||
                  'WppConnect'
                : undefined,
            poweredBy:
              client.config.phone == undefined // bug when using phone code this shouldn't be passed (https://github.com/wppconnect-team/wppconnect-server/issues/1687#issuecomment-2099357874)
                ? client.config?.poweredBy ||
                  req.serverOptions.poweredBy ||
                  'WPPConnect-Server'
                : undefined,
            catchLinkCode: (code: string) => {
              if ((client as any).shouldClose) {
                req.logger.info(
                  `[${session}] shouldClose detected in catchLinkCode. Force-killing browser.`
                );
                killBrowserOrFallback();
                clientsArray[session] = undefined;
                return;
              }
              this.exportPhoneCode(req, client.config.phone, code, client, res);
            },
            // Not a WPPConnect option — WinZapp's own host.layer.js patch reads
            // it off `this.options` (create() spreads the caller's options into
            // the Whatsapp instance verbatim, so an unknown key survives). See
            // client/core/wppconnect_host_layer_patch.py, checkQrCode v4.
            catchLinkCodeError: (failure: {
              name?: string;
              message?: string;
              session?: string;
              attempt?: number;
              retryInSeconds?: number;
              rateLimited?: boolean;
              stack?: string;
              details?: Record<string, string>;
            }) => {
              if ((client as any).shouldClose) return;
              this.exportPhoneCodeError(
                req,
                client.config.phone,
                failure,
                client
              );
            },
            catchQR: (
              base64Qr: any,
              asciiQR: any,
              attempt: any,
              urlCode: string
            ) => {
              if ((client as any).shouldClose) {
                req.logger.info(
                  `[${session}] shouldClose detected in catchQR. Force-killing browser.`
                );
                killBrowserOrFallback();
                clientsArray[session] = undefined;
                return;
              }
              this.exportQR(req, base64Qr, urlCode, client, res);
            },
            onLoadingScreen: (percent: string, message: string) => {
              req.logger.info(`[${session}] ${percent}% - ${message}`);
            },
            statusFind: (statusFind: StatusFind) => {
              try {
                if ((client as any).shouldClose) {
                  req.logger.info(
                    `[${session}] shouldClose detected in statusFind. Force-killing browser.`
                  );
                  killBrowserOrFallback();
                  clientsArray[session] = undefined;
                  return;
                }
                eventEmitter.emit(
                  `status-${client.session}`,
                  client,
                  statusFind
                );
                const statusPayload = {
                  status: statusFind,
                  session: client.session,
                };
                // Deliberately NOT forwarded over Socket.IO as 'status-find'.
                //
                // WinZapp registers a listener for that event
                // (websocket_client.py's on_wpp_status_find), and that handler
                // treats a single `disconnectedMobile` or `notLogged` as a
                // permanent logout: it calls _handle_logout() →
                // _reset_credentials_and_show_pairing(), which wipes the
                // WA_token, drops `paired`, and runs clear_local_data() over
                // the whole local database. One event, no confirmation — none
                // of the safeguards main.py's _act_on_unlink_decision() applies
                // (never before the session has connected this run, several
                // consecutive strikes spaced by real wall-clock time, and a
                // _still_linked_on_server() probe that must positively answer
                // "no linked phone" before anything is wiped) are on that path.
                //
                // But `disconnectedMobile` does not mean "unlinked". WPPConnect
                // documents it as "Client has disconnected to the mobile
                // device" — the phone became unreachable, which is routine
                // (dead battery, no signal, the machine sleeping). `notLogged`
                // is the one that means "scan the QR code again".
                //
                // Before this emit existed nothing published 'status-find' over
                // Socket.IO (only the webhook below), so that handler was dead
                // code and the mismatch was harmless. Adding the emit armed it,
                // and forced re-pairings started being reported. The status
                // still reaches WinZapp truthfully through `client.status`
                // below, which routes it via the REST poll into the guarded
                // path instead. Re-add this line only together with a fix for
                // on_wpp_status_find()'s own classification.
                if (
                  statusFind === StatusFind.disconnectedMobile ||
                  statusFind === StatusFind.notLogged
                ) {
                  (client as any)._markedConnected = false;
                  client.status = statusFind;
                  client.qrcode = null;
                }
                if (statusFind === StatusFind.autocloseCalled) {
                  client.status = 'CLOSED';
                  client.qrcode = null;
                  client.close();
                  clientsArray[session] = undefined;
                }
                callWebHook(client, req, 'status-find', statusPayload);
                req.logger.info(statusFind + '\n\n');
              } catch (error) {}
            },
          }
        )
      );

      // Poll every 2s: if shouldClose was set while create() is blocked, close browser immediately
      const shouldClosePoller = setInterval(() => {
        if ((client as any).shouldClose) {
          req.logger.info(
            `[${session}] shouldClose detected by poller. Force-killing browser.`
          );
          clearInterval(shouldClosePoller);
          killBrowserOrFallback();
          clientsArray[session] = undefined;
        }
      }, 2000);

      if (clientsArray[session] && (clientsArray[session] as any).shouldClose) {
        clearInterval(shouldClosePoller);
        req.logger.info(
          `[${session}] Session was closed during initialization. Terminating browser.`
        );
        try {
          await wppClient.close();
        } catch (e) {}
        clientsArray[session] = undefined;
        if (res && !res.headersSent) {
          res.status(200).json({
            status: false,
            message: 'Session closed during initialization',
          });
        }
        return;
      }
      clearInterval(shouldClosePoller);

      client = clientsArray[session] = Object.assign(wppClient, client);
      if (client.page) {
        client.page.on('console', (msg: any) => {
          const text = msg.text();
          if (text.includes('[browser-evaluate]')) {
            req.logger.info(text);
          }
        });
        // The shim lives on a prototype inside the page, so it dies with every
        // WhatsApp Web reload (and wa-js is re-injected fresh each time).
        // Re-install on load as well as right now, or the first reload
        // silently brings the id-less messages back.
        client.page.on('load', () => {
          restoreMsgKeySerialized(client.page, req.logger, session);
          restoreStatusSender(client.page, req.logger, session);
          // The permission grant survives a navigation (it is stored on the
          // browser context), but the bucket request does not — persist() has
          // to be asked again by the new document, so re-run the whole thing.
          grantPersistentStorage(client.page, req.logger, session);
        });
        await restoreMsgKeySerialized(client.page, req.logger, session);
        await restoreStatusSender(client.page, req.logger, session);
        await grantPersistentStorage(client.page, req.logger, session);
      }
      await this.start(req, client);

      if (req.serverOptions.webhook.onParticipantsChanged) {
        await this.onParticipantsChanged(req, client);
      }

      if (req.serverOptions.webhook.onReactionMessage) {
        await this.onReactionMessage(client, req);
      }

      if (req.serverOptions.webhook.onRevokedMessage) {
        await this.onRevokedMessage(client, req);
      }

      if (req.serverOptions.webhook.onPollResponse) {
        await this.onPollResponse(client, req);
      }
      if (req.serverOptions.webhook.onLabelUpdated) {
        await this.onLabelUpdated(client, req);
      }
    } catch (e) {
      req.logger.error(e);
      if (e instanceof Error && e.name == 'TimeoutError') {
        const client = this.getClient(session) as any;
        client.status = 'CLOSED';
      }
    }
  }

  async opendata(req: Request, session: string, res?: any) {
    await this.createSessionUtil(req, clientsArray, session, res);
  }

  exportPhoneCode(
    req: any,
    phone: any,
    phoneCode: any,
    client: WhatsAppServer,
    res?: any
  ) {
    if ((client as any).shouldClose) return;
    eventEmitter.emit(`phoneCode-${client.session}`, phoneCode, client);

    Object.assign(client, {
      status: 'PHONECODE',
      phoneCode: phoneCode,
      phone: phone,
    });

    req.io.emit('phoneCode', {
      data: phoneCode,
      phone: phone,
      session: client.session,
    });

    callWebHook(client, req, 'phoneCode', {
      phoneCode: phoneCode,
      phone: phone,
      session: client.session,
    });

    if (res && !res._headerSent)
      res.status(200).json({
        status: 'phoneCode',
        phone: phone,
        phoneCode: phoneCode,
        session: client.session,
      });
  }

  /**
   * Report a failed pairing-code request to the client.
   *
   * WhatsApp can refuse to issue a link-by-code — CompanionHelloError is the
   * one seen in practice — while the session itself stays perfectly healthy
   * and goes on rotating auth codes. Before this the failure never left the
   * Node process: host.layer.js logged it and the browser quietly retried,
   * while the Python side sat out its full 90-second phoneCode timeout and
   * then reported the generic "no pairing code received", leaving the person
   * trying to pair with nothing to act on.
   *
   * Deliberately does NOT touch client.status, close the session, or answer
   * `res`: checkQrCode()'s retry is still live and a later attempt may well
   * succeed, and /start-session has long since responded. This is a
   * notification, not a terminal state.
   */
  exportPhoneCodeError(
    req: any,
    phone: any,
    failure: {
      name?: string;
      message?: string;
      attempt?: number;
      retryInSeconds?: number;
      rateLimited?: boolean;
      stack?: string;
      details?: Record<string, string>;
    },
    client: WhatsAppServer
  ) {
    if ((client as any).shouldClose) return;

    const name = failure?.name || 'Error';
    const message = failure?.message || '';
    // attempt/retryInSeconds come from host.layer.js's backoff. Forwarded
    // rather than dropped: a log line saying which attempt this was and when
    // the next one is due is most of what makes a failing pairing run
    // diagnosable after the fact.
    const attempt = failure?.attempt;
    const retryInSeconds = failure?.retryInSeconds;
    // Set by checkQrCode v8 when WhatsApp answered rate-overlimit (429). The
    // quota is per phone number and lives on WhatsApp's side, so it outlives
    // this session and this process — which is why the first pairing code
    // after a dropped session fails and the second one works. Forwarded so
    // the client can say that instead of showing "CompanionHelloError".
    const rateLimited = failure?.rateLimited === true;

    req.logger?.warn(
      `[${client.session}] pairing code request failed: ${name}: ${message}` +
        (attempt ? ` (attempt ${attempt}, next retry in ${retryInSeconds}s)` : '')
    );

    // WhatsApp Web's own bundle throws this from inside the page, and its class
    // name ("CompanionHelloError") is all that survived to here before. The
    // page-context stack is the only thing that names the Meta module that
    // threw, and any extra own property on the error is the only place a
    // server refusal code could be hiding — both are logged in full because
    // there is nowhere else to get them from.
    if (failure?.stack) {
      req.logger?.warn(
        `[${client.session}] pairing code failure stack: ${failure.stack}`
      );
    }
    if (failure?.details && Object.keys(failure.details).length) {
      req.logger?.warn(
        `[${client.session}] pairing code failure details: ` +
          JSON.stringify(failure.details)
      );
    }

    const payload = {
      name: name,
      message: message,
      phone: phone,
      session: client.session,
      attempt: attempt,
      retryInSeconds: retryInSeconds,
      rateLimited: rateLimited,
      stack: failure?.stack || '',
      details: failure?.details || {},
    };

    req.io.emit('phoneCodeError', payload);
    callWebHook(client, req, 'phoneCodeError', payload);
  }

  exportQR(
    req: any,
    qrCode: any,
    urlCode: any,
    client: WhatsAppServer,
    res?: any
  ) {
    if ((client as any).shouldClose) return;
    eventEmitter.emit(`qrcode-${client.session}`, qrCode, urlCode, client);
    Object.assign(client, {
      status: 'QRCODE',
      qrcode: qrCode,
      urlcode: urlCode,
    });

    qrCode = qrCode.replace('data:image/png;base64,', '');
    const imageBuffer = Buffer.from(qrCode, 'base64');

    req.io.emit('qrCode', {
      data: 'data:image/png;base64,' + imageBuffer.toString('base64'),
      session: client.session,
    });

    callWebHook(client, req, 'qrcode', {
      qrcode: qrCode,
      urlcode: urlCode,
      session: client.session,
    });
    if (res && !res._headerSent)
      res.status(200).json({
        status: 'qrcode',
        qrcode: qrCode,
        urlcode: urlCode,
        session: client.session,
      });
  }

  async onParticipantsChanged(req: any, client: any) {
    await client.isConnected();
    await client.onParticipantsChanged((message: any) => {
      callWebHook(client, req, 'onparticipantschanged', message);
    });
  }

  async start(req: Request, client: WhatsAppServer) {
    // Register the state listener FIRST so a CONNECTED event that arrives while
    // we retry isConnected() below is never lost (GPT r1 #1). The event is the
    // authority; the isConnected() retry is only a fallback for the case where
    // the page was already connected before the listener was attached.
    await this.checkStateSession(client, req);
    // Wire message/ack/presence listeners now, before the retry loop's early
    // returns, so they're attached no matter how finalization resolves.
    await this.wireListeners(req, client);

    // isConnected() runs a WAPI (wa-js) function inside the page. Right after a
    // WhatsApp Web (re)load wa-js may not be injected yet, so the call throws
    // "WAPI is not defined". The old code (a) treated ANY non-throw as CONNECTED
    // (ignoring the boolean) and (b) let that transient error fall into catch
    // and skip status=CONNECTED entirely, leaving the session stuck reporting
    // INITIALIZING forever even though it connected seconds later. Bounded retry
    // until wa-js answers, and only accept an explicit `true`.
    const maxAttempts = 20; // ~10s total; a warning, never a disconnect proof
    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
      try {
        const connected = await client.isConnected();
        if (connected === true) {
          // Only promote if the state listener hasn't already moved us to a
          // newer terminal state (UNPAIRED/TIMEOUT/etc). The event wins.
          if (client.status === 'INITIALIZING') {
            this.markConnected(client, req);
          }
          return;
        }
        // isConnected() returned false: page reachable but not logged in yet.
        // Keep polling; a real login will flip it or the state event will fire.
      } catch (error) {
        // "WAPI is not defined" is an initialization race, NOT a session error.
        // Do not emit session-error for it; just wait for wa-js to load.
        req.logger.info(
          `[${client.session}] isConnected() not ready yet (attempt ${attempt}/${maxAttempts})`
        );
      }
      await new Promise((r) => setTimeout(r, 500));
    }
    // Retry window elapsed without a positive isConnected(). This is only a
    // warning: the onStateChange listener stays armed and will still promote to
    // CONNECTED (or a terminal state) whenever WhatsApp Web reports it.
    req.logger.info(
      `[${client.session}] isConnected() did not confirm within retry window — ` +
        `relying on onStateChange (session may still come up).`
    );
  }

  /**
   * Register message/ack/presence listeners. Split out of start() so the
   * isConnected() retry loop cannot delay or skip them — they must be wired
   * regardless of how connection finalization resolves.
   */
  async wireListeners(req: Request, client: WhatsAppServer) {
    await this.listenMessages(client, req);

    if (req.serverOptions.webhook.listenAcks) {
      await this.listenAcks(client, req);
    }

    if (req.serverOptions.webhook.onPresenceChanged) {
      await this.onPresenceChanged(client, req);
    }

    await this.onUnreadCountChanged(client, req);
    await this.onIncomingCallDirect(client, req);
  }

  /**
   * WinZapp patch: forward WA-JS's own `chat.unread_count_changed` event
   * (fired whenever a chat's unread count changes for ANY reason — including
   * the user reading it on their phone or another linked device) to
   * WinZapp's client as a `chats-update` socket event.
   *
   * There is no wppconnect-server webhook/event wrapper for this — every
   * other onXxx() in this file (onReactionMessage, onPollResponse, ...)
   * relies on an ExposedFn binding wppconnect's own page-injection code sets
   * up ahead of time, and this event has none. `page.exposeFunction()`
   * needs no such prior wiring: it lets Node register a callback directly
   * invocable from the page, so a small `WPP.on(...)` installed here is
   * self-contained. Without this, WinZapp's own `on_chats_update` handler
   * (which already exists client-side and does exactly the right thing)
   * never received a single event — a chat read on the phone stayed shown
   * as unread in WinZapp until the user happened to open it there too.
   *
   * `WPP.on` lives on the page and is re-injected fresh on every WhatsApp
   * Web reload, same as the msgKey._serialized shim above — re-install on
   * 'load' too, or the first reload silently drops this listener.
   *
   * The install itself polls (same setInterval pattern as the
   * MsgKey._serialized/status-sender shims above), instead of trusting the
   * single attempt the first version of this patch made: wireListeners()
   * runs before isConnected() is confirmed, so window.WPP routinely isn't
   * injected yet at that point, and WhatsApp Web's SPA never fires a second
   * 'load' once connected — so a failed first try used to mean the listener
   * never existed for the rest of the session. Measured live: zero
   * chats-update events for unreadCount over 24+ minutes of active message
   * traffic with the single-attempt version.
   */
  async onUnreadCountChanged(client: WhatsAppServer, req: Request) {
    try {
      await client.page.exposeFunction(
        '__winzappOnUnreadChanged',
        (chatId: string, unreadCount: number, previousUnreadCount: number | null) => {
          req.io.emit('chats-update', {
            data: [{ remoteJid: chatId, unreadCount, previousUnreadCount }],
            session: client.session,
          });
        }
      );
    } catch (e) {
      // exposeFunction throws if a prior session already registered this
      // name on the same page (e.g. a reconnect reusing the browser) —
      // harmless, the existing binding still works.
    }

    const installListener = () => {
      client.page
        .evaluate(() => {
          // Everything in here is wrapped, and the "installed" flag is only
          // set once WPP.on() has actually returned: `WPP.on` is foreign,
          // minified code that can throw even once the `!WPP.on` guard has
          // passed (wa-js present, emitter not bootstrapped). Marking the
          // context as installed first would leave it permanently claiming a
          // listener it never registered, and letting the throw escape is
          // worse still — on the first attempt it rejects the whole evaluate
          // so the retry below is never even scheduled, and from inside the
          // retry it skips clearInterval(), leaving a timer firing every 500ms
          // for the life of the page. Same shape as restoreStatusSender above,
          // for the same reason.
          const install = () => {
            try {
            const WPP = (window as any).WPP;
            if (!WPP || !WPP.on) return false;
            if ((window as any).__winzappUnreadListenerInstalled) return true;
            WPP.on('chat.unread_count_changed', (evt: any) => {
              try {
              const chatId = evt?.chat?.id?._serialized || evt?.chat?.id;
              if (!chatId) return;
                  // The count this chat held BEFORE the change, forwarded so the
                  // client can tell a real read apart from a meaningless zero.
                  // A chat merely being loaded into the Store reports unreadCount=0
                  // with nothing behind it, and is indistinguishable from "the user
                  // just read this chat on their phone" unless you know whether the
                  // count actually fell from something.
                  //
                  // Measured against a live session: the field really is a plain
                  // number, matching wa-js's own typing. Three consecutive messages
                  // in one group came through as unreadCount 1/2/3 with
                  // previousUnreadCount 0/1/2 — tracking exactly one step behind.
                  // The suspicion it might be Backbone's options object (wa-js
                  // emits it straight from the Store's `change:unreadCount`
                  // callback, whose third argument is options in every other
                  // handler of that shape) did not hold. The `.previous()` fallback
                  // stays anyway: it costs nothing, and it is what keeps this
                  // working if a future wa-js changes the payload — silently, since
                  // nothing else here would notice. Neither being a number sends
                  // null, and the client then keeps its own conservative count.
                  // Deriving `previous` must never be able to suppress the event
                  // itself: the unread count is the payload that matters and the
                  // previous value is an extra, so it is computed defensively and
                  // the emit happens regardless. Reading `.previous` off a Store
                  // model is a property access on foreign, minified code that can
                  // throw or be a getter with side effects, and a throw anywhere in
                  // this callback silently takes the whole listener down — the
                  // client then stops being told about unread counts at all, with
                  // nothing in any log to say so. Measured, not hypothetical: an
                  // earlier version of this block computed `previous` inline and
                  // the chats-update stream stopped dead.
              let previous: any = null;
              try {
                const raw = evt?.previousUnreadCount;
                if (typeof raw === 'number') {
                  previous = raw;
                } else if (typeof evt?.chat?.previous === 'function') {
                  const p = evt.chat.previous('unreadCount');
                  previous = typeof p === 'number' ? p : null;
                }
              } catch (e) {
                previous = null;
              }
              (window as any).__winzappOnUnreadChanged(chatId, evt.unreadCount, previous);
              } catch (e) {
                // Anything unexpected in here costs at most this one event.
                // Never the listener: WhatsApp Web's own objects are foreign
                // and minified, and losing this stream means unread counts
                // silently stop updating everywhere in the app.
              }
            });
            (window as any).__winzappUnreadListenerInstalled = true;
            return true;
            } catch (e) {
              return false;
            }
          };
          if (install()) return 'installed';
          // wa-js is injected asynchronously, so the first try above usually
          // finds no window.WPP at all — poll instead of giving up, exactly
          // like the MsgKey._serialized and status sender shims. See this
          // method's own doc comment for why a single attempt was never
          // enough here.
          let tries = 0;
          const timer = setInterval(() => {
            if (install() || ++tries > 60) {
              clearInterval(timer);
              // The evaluate below can only ever log 'scheduled' — it returns
              // long before this loop resolves — so without this line the log
              // cannot tell "installed three seconds later" apart from "gave
              // up after 30s and this session will never see an unread event",
              // which is exactly the blind spot that let the single-attempt
              // version go unnoticed for 24 minutes. The page console is
              // already bridged into log.log for anything tagged
              // '[browser-evaluate]' (see the page.on('console') handler where
              // the client is created).
              console.log(
                (window as any).__winzappUnreadListenerInstalled
                  ? `[browser-evaluate] onUnreadCountChanged listener: installed after ${tries} retries`
                  : '[browser-evaluate] onUnreadCountChanged listener: GAVE UP after 60 retries — wa-js never appeared, unread counts will not update'
              );
            }
          }, 500);
          return 'scheduled (wa-js not ready yet)';
        })
        .then((result: string) =>
          req.logger.info(`[${client.session}] onUnreadCountChanged listener: ${result}`)
        )
        .catch((e: any) =>
          req.logger.warn(
            `[onUnreadCountChanged] install failed: ${e?.message || e}`
          )
        );
    };

    // A page reload gets a fresh JS context (window.__winzappUnreadListenerInstalled
    // starts undefined again along with everything else wa-js re-injects), so
    // re-running installListener() here is exactly the reinstall the reload
    // needs — no separate reset step required.
    client.page.on('load', installListener);
    installListener();
  }

  /**
   * WinZapp patch: detect incoming WhatsApp calls by injecting a listener
   * directly into the WhatsApp Web page via page.evaluate, bypassing
   * wppconnect's own client.onIncomingCall() wrapper which stopped working
   * after a WhatsApp Web VoIP stack change (wa-js 4.5.0 bug, fixed in 4.6.0).
   *
   * Uses the same page.exposeFunction + page.evaluate pattern as
   * onUnreadCountChanged above. Two detection layers are installed:
   *
   *   1. WPP.on('call.incoming_call') — the wa-js event itself, in case the
   *      issue is only in wppconnect's wrapper and not in wa-js's internal
   *      registration.
   *   2. Direct Store access — if WPP exposes the internal CallStore/CallCollection,
   *      we hook its 'add' event directly, bypassing wa-js's event plumbing
   *      entirely.
   *
   * If both layers fire for the same call, the Python side receives two
   * events. This is harmless: the sound plays idempotently and the toast
   * deduplicates by nature (Windows suppresses identical toasts within a
   * short window).
   *
   * If neither layer works (WhatsApp changed too much), nothing breaks —
   * every call is wrapped in try/catch and failures are logged.
   */
  async onIncomingCallDirect(client: WhatsAppServer, req: Request) {
    try {
      await client.page.exposeFunction(
        '__winzappOnIncomingCall',
        (
          event: string,
          state: string,
          peerJid: string,
          callId: string,
          isVideo: boolean,
          isGroup: boolean,
          groupJid: string,
          callTimestamp: number,
          observedAt: number
        ) => {
          req.io.emit('incomingcall', {
            session: client.session,
            data: {
              event: event,
              state: state,
              peerJid: peerJid,
              id: callId,
              isVideo: isVideo,
              isGroup: isGroup,
              groupJid: groupJid,
              timestamp: callTimestamp,
              observedAt: observedAt,
            },
          });
        }
      );
    } catch (e) {
      // exposeFunction throws if a prior session already registered this
      // name on the same page (e.g. a reconnect reusing the browser) —
      // harmless, the existing binding still works.
    }

    const installListener = (attempt = 0) => {
      // CallStore is hydrated from persisted WhatsApp Web state at startup.
      // Its `add` event therefore does not necessarily mean "a call started
      // now". Refresh the boundary on every page load and reject older calls.
      const listenerStartedAt = Date.now();
      client.page
        .evaluate((listenerStartedAt: number) => {
          const WPP = (window as any).WPP;
          if (
            !WPP ||
            !WPP.on ||
            (window as any).__winzappIncomingCallInstalled
          ) {
            return (window as any).__winzappIncomingCallInstalled === true;
          }
          (window as any).__winzappIncomingCallInstalled = true;

          // WA-JS 4.5 only exposes the incoming offer publicly. Later states
          // live in CallStore, and some WhatsApp builds mutate them without
          // firing the expected Backbone event, so retain and poll by call id.
          const trackedCalls = new Map<string, any>();
          const ignoredHistoricalCallIds = new Set<string>();
          const CALL_START_GRACE_MS = 5000;
          const stores = [
            WPP?.whatsapp?.CallStore,
            WPP?.whatsapp?.CallCollection,
            (window as any).Store?.Call,
          ].filter((store, index, all) => store && all.indexOf(store) === index);
          const callIdOf = (call: any) =>
            String(call?.id?._serialized || call?.id || '');
          const groupJidOf = (call: any) =>
            String(
              call?.groupJid?._serialized ||
              call?.groupJid?.toString?.() ||
              ''
            );
          const callStateOf = (call: any) => {
            const raw = String(
              call?.getState?.() || call?.state || call?.get?.('state') || ''
            );
            // WhatsApp Web 2.3000 may return its newer numeric VoIP enum even
            // though WA-JS 4.5 types still advertise strings. ReceivedCall (3)
            // and ReceivedCallWithoutOffer (8) both mean it is still ringing.
            const numericStates: Record<string, string> = {
              '0': 'NONE',
              '1': 'CALLING',
              '2': 'PREACCEPT_RECEIVED',
              '3': 'INCOMING_RING',
              '4': 'ACCEPT_SENT',
              '5': 'ACCEPT_RECEIVED',
              '6': 'ACTIVE',
              '7': 'HANDLED_REMOTELY',
              '8': 'INCOMING_RING',
              '9': 'REJOINING',
              '10': 'LINK',
              '11': 'CONNECTED_LONELY',
              '12': 'PRE_CALLING',
              '13': 'ENDED',
              '14': 'CALL_B_STARTING',
            };
            return numericStates[raw] || raw;
          };
          const callTimestampOf = (call: any) => {
            const raw =
              call?.offerTime ??
              call?.timestamp ??
              call?.t ??
              call?.startTime ??
              call?.createdAt ??
              call?.get?.('offerTime') ??
              call?.get?.('timestamp') ??
              call?.get?.('t') ??
              0;
            const numeric = Number(raw);
            if (!Number.isFinite(numeric) || numeric <= 0) return 0;
            if (numeric >= 1e15) return Math.floor(numeric / 1000);
            if (numeric >= 1e12) return Math.floor(numeric);
            if (numeric >= 1e9) return Math.floor(numeric * 1000);
            return 0;
          };
          const isHistoricalIncomingCall = (call: any, source: string) => {
            const id = callIdOf(call);
            if (id && ignoredHistoricalCallIds.has(id)) return true;
            const timestamp = callTimestampOf(call);
            const receivedWhileOffline =
              call?.offerReceivedWhileOffline === true ||
              call?.get?.('offerReceivedWhileOffline') === true;
            const predatesListener =
              timestamp > 0 &&
              timestamp < listenerStartedAt - CALL_START_GRACE_MS;
            // A timestamp-less Store add is ambiguous because this collection
            // also hydrates persisted models. Fail closed; the public event
            // still covers a genuine live call when WA-JS provides it.
            const timestampLessStoreEvent = !timestamp && source === 'store';
            if (
              receivedWhileOffline ||
              predatesListener ||
              timestampLessStoreEvent
            ) {
              if (id) ignoredHistoricalCallIds.add(id);
              return true;
            }
            return false;
          };
          const emitCall = (event: string, call: any, state = '') => {
            const peerJid =
              call?.peerJid?._serialized ||
              call?.peerJid?.toString?.() ||
              call?.sender?._serialized ||
              call?.sender?.toString?.() ||
              call?.from?._serialized ||
              call?.from?.toString?.() ||
              '';
            if (!peerJid) return;
            (window as any).__winzappOnIncomingCall(
              event,
              state,
              peerJid,
              callIdOf(call),
              !!call?.isVideo || !!call?.isVideoCall,
              !!call?.isGroup || !!call?.isGroupCall,
              groupJidOf(call),
              Math.floor(callTimestampOf(call) / 1000),
              Math.floor(Date.now() / 1000)
            );
          };
          const rememberCall = (call: any) => {
            const id = callIdOf(call);
            if (!id || ignoredHistoricalCallIds.has(id)) return;
            const previous = trackedCalls.get(id) || {};
            trackedCalls.set(id, {
              call: call || previous.call,
              startedAt: previous.startedAt || Date.now(),
              missingSince: 0,
              foundInStore: previous.foundInStore || false,
            });
          };
          const findCall = (id: string) => {
            for (const store of stores) {
              try {
                const direct = store?.get?.(id);
                if (direct) return direct;
                const models =
                  store?.getModelsArray?.() || store?._models || store?.models || [];
                const found = models.find?.((model: any) => callIdOf(model) === id);
                if (found) return found;
              } catch (e) {
                // Try the next exported alias.
              }
            }
            return null;
          };
          const emitIncomingOffer = (
            call: any,
            attempt = 0,
            source = 'wpp'
          ) => {
            const id = callIdOf(call);
            const richCall = findCall(id) || call;
            if (isHistoricalIncomingCall(richCall, source)) return;
            const isGroup = !!richCall?.isGroup || !!richCall?.isGroupCall;
            if (isGroup && !groupJidOf(richCall) && attempt < 10) {
              window.setTimeout(() => {
                // A terminal Store event removes this id. Do not resurrect a
                // call that ended while we were waiting for group metadata.
                if (trackedCalls.has(id))
                  emitIncomingOffer(call, attempt + 1, source);
              }, 100);
              return;
            }
            emitCall('offer', richCall, 'INCOMING_RING');
          };

          // ── Layer 1: WPP.on('call.incoming_call') ──────────────────
          // Uses wa-js's own event, which MAY work if the bug is only
          // in wppconnect's Node-side wrapper rather than in wa-js's
          // internal event registration.
          try {
            WPP.on('call.incoming_call', (call: any) => {
              try {
                if (isHistoricalIncomingCall(call, 'wpp')) return;
                rememberCall(call);
                // The public WA-JS event can be a reduced object: it says the
                // call is a group call but omits groupJid. CallStore's model
                // contains the group id, so briefly wait for it before
                // announcing. The direct Store listener may win this race;
                // Python deduplicates both events by call id.
                emitIncomingOffer(call, 0, 'wpp');
              } catch (e) {
                // Never let one event kill the listener
              }
            });
          } catch (e) {
            // WPP.on might not support this event in this version
          }

          // ── Layer 2: direct internal CallStore access ──────────────
          // If wa-js exposes the raw WhatsApp Web Store for calls, hook
          // its collection's 'add' event directly. This bypasses wa-js's
          // event plumbing entirely.
          try {
            for (const store of stores) {
              if (store && typeof store.on === 'function') {
                store.on('add', (call: any) => {
                  try {
                    // Only trigger for incoming calls (not outgoing)
                    const isIncoming =
                      call?.getState?.() === 'INCOMING_RING' ||
                      call?.isIncoming ||
                      call?.direction === 'incoming' ||
                      !call?.outgoing;
                    if (!isIncoming) return;

                    if (isHistoricalIncomingCall(call, 'store')) return;

                    const initialState = String(call?.getState?.() || 'INCOMING_RING');
                    emitCall('offer', call, initialState);
                    rememberCall(call);
                  } catch (e) {
                    // Never let one event kill the listener
                  }
                });
                const onStateChange = (call: any) => {
                  try {
                    const id = callIdOf(call);
                    if (ignoredHistoricalCallIds.has(id)) {
                      const ignoredState = callStateOf(call);
                      if (ignoredState && ignoredState !== 'INCOMING_RING') {
                        ignoredHistoricalCallIds.delete(id);
                      }
                      return;
                    }
                    const nextState = callStateOf(call);
                    if (!nextState) return;
                    emitCall('state', call, nextState);
                    if (nextState === 'INCOMING_RING') rememberCall(call);
                    else trackedCalls.delete(callIdOf(call));
                  } catch (e) {
                    // A malformed state update must not remove the listener.
                  }
                };
                // Collections re-emit model changes, including for a CallModel
                // that existed before our `add` listener saw it.
                store.on('change:state', onStateChange);
                store.on('change', onStateChange);
                store.on('remove', (call: any) => {
                  try {
                    const id = callIdOf(call);
                    if (ignoredHistoricalCallIds.delete(id)) return;
                    emitCall('ended', call, 'ENDED');
                    trackedCalls.delete(callIdOf(call));
                  } catch (e) {
                    // Never let one event kill the listener
                  }
                });
              }
            }
          } catch (e) {
            // Store access failed — not critical
          }

          // Poll only active incoming calls. If a model disappears for 2.5s,
          // WhatsApp has removed it after answer/rejection/caller cancellation.
          (window as any).__winzappIncomingCallPoll = window.setInterval(() => {
            const now = Date.now();
            for (const [id, tracked] of trackedCalls.entries()) {
              try {
                const liveCall = findCall(id);
                const currentCall = liveCall || tracked.call;
                const state = callStateOf(currentCall);
                if (state && state !== 'INCOMING_RING') {
                  emitCall('state', currentCall, state);
                  trackedCalls.delete(id);
                  continue;
                }
                if (liveCall) {
                  tracked.call = liveCall;
                  tracked.missingSince = 0;
                  tracked.foundInStore = true;
                  continue;
                }

                if (tracked.foundInStore) {
                  tracked.missingSince = tracked.missingSince || now;
                }
                if (tracked.foundInStore && now - tracked.missingSince >= 2500) {
                  emitCall('ended', tracked.call, 'ENDED');
                  trackedCalls.delete(id);
                } else if (now - tracked.startedAt >= 120000) {
                  emitCall('timeout', tracked.call, 'NOT_ANSWERED');
                  trackedCalls.delete(id);
                }
              } catch (e) {
                // The next poll can recover from a transient Store mutation.
              }
            }
          }, 500);
          return true;
        }, listenerStartedAt)
        .then((installed: boolean) => {
          if (!installed && attempt < 120) {
            setTimeout(() => installListener(attempt + 1), 500);
          }
        })
        .catch((e: any) =>
          req.logger.warn(
            `[onIncomingCallDirect] install failed: ${e?.message || e}`
          )
        );
    };

    // Re-install on page reload (fresh JS context loses the listener)
    client.page.on('load', installListener);
    installListener();
  }

  /**
   * Idempotent connection finalization. Runs at most once whether triggered by
   * the onStateChange CONNECTED event or by a successful isConnected() retry, so
   * we never skip startHelper()/session-logged (GPT r1 #5) nor run them twice.
   */
  markConnected(client: WhatsAppServer, req: Request) {
    if ((client as any)._markedConnected) return;
    (client as any)._markedConnected = true;
    Object.assign(client, { status: 'CONNECTED', qrcode: null });
    req.logger.info(`Started Session: ${client.session}`);
    req.io.emit('session-logged', { status: true, session: client.session });
    try {
      startHelper(client, req);
    } catch (error) {
      req.logger.error(error);
    }
  }

  async checkStateSession(client: WhatsAppServer, req: Request) {
    await client.onStateChange((state) => {
      req.logger.info(`State Change ${state}: ${client.session}`);
      const conflits = [SocketState.CONFLICT];

      if (conflits.includes(state)) {
        client.useHere();
        return;
      }

      // The state event is the authority for status (GPT r1 #2). CONNECTED
      // promotes (idempotently); disconnect/unpaired states clear a stale
      // CONNECTED so the client stops believing it's online. CONFLICT is left
      // to useHere() above and is NOT treated as terminal.
      if (state === SocketState.CONNECTED) {
        this.markConnected(client, req);
      } else if (
        state === SocketState.UNPAIRED ||
        state === SocketState.UNPAIRED_IDLE ||
        state === SocketState.TIMEOUT ||
        state === SocketState.DEPRECATED_VERSION ||
        state === SocketState.PROXYBLOCK ||
        state === SocketState.TOS_BLOCK ||
        state === SocketState.SMB_TOS_BLOCK
      ) {
        // Allow a later CONNECTED to re-finalize (e.g. re-pair) by clearing the
        // once-guard, and drop the CONNECTED status so REST reports the truth.
        (client as any)._markedConnected = false;
        client.status = state;
      }
    });
  }

  async listenMessages(client: WhatsAppServer, req: Request) {
    const incomingCallListenerStartedAt = Date.now();

    await client.onMessage(async (message: any) => {
      eventEmitter.emit(`mensagem-${client.session}`, client, message);
      callWebHook(client, req, 'onmessage', message);
      if (message.type === 'location')
        client.onLiveLocation(message.sender.id, (location) => {
          callWebHook(client, req, 'location', location);
        });
    });

    await client.onAnyMessage(async (message: any) => {
      message.session = client.session;

      if (message.type === 'gp2' && message.subtype === 'subject') {
        const serialized = (value: any) =>
          value?._serialized || value?.id?._serialized || value || '';
        const candidates = [message.chatId, message.from, message.to].map(serialized);
        const groupId = candidates.find(
          (value: any) => typeof value === 'string' && value.endsWith('@g.us')
        );
        if (groupId) {
          let subject = message.body || message.subject || message.value || '';
          if (!subject) {
            try {
              const chat: any = await client.getChatById(groupId);
              subject =
                chat?.groupMetadata?.subject ||
                chat?.name ||
                chat?.formattedTitle ||
                '';
            } catch (e: any) {
              req.logger.warn(
                `[group-subject] Failed to resolve ${groupId}: ${e?.message || e}`
              );
            }
          }
          if (subject) {
            req.io.emit('groups.update', {
              data: [{ id: groupId, subject }],
              session: client.session,
            });
          }
        }
      }

      if (message.type === 'sticker') {
        download(message, client, req.logger);
      }

      if (
        req.serverOptions?.websocket?.autoDownload ||
        (req.serverOptions?.webhook?.autoDownload && message.fromMe == false)
      ) {
        await autoDownload(client, req, message);
      }

      req.io.emit('received-message', {
        response: message,
        session: client.session,
      });
      if (req.serverOptions.webhook.onSelfMessage && message.fromMe)
        callWebHook(client, req, 'onselfmessage', message);
    });

    // WhatsApp re-delivers an edited message under its ORIGINAL key.id, but
    // that re-delivery only ever arrives through this dedicated
    // chat.msg_edited/onMessageEdit event — never through onAnyMessage
    // above, whose chat.new_message hook simply never fires for an edit.
    // Without this, an edit made by anyone else than the WinZapp user was
    // silently dropped server-side: nothing at all reached the Python
    // client until the next full resync re-fetched the chat from scratch
    // and picked up the current text that way. Re-emitting through the
    // SAME 'received-message' event onAnyMessage already uses is
    // deliberate: WAPI.processMessageObj() serializes an edit's `msg`
    // identically to a normal message (same key.id as the original), so
    // it flows straight into the existing same-id dedup check in
    // MainWindow.on_new_message() (client/main.py's _apply_possible_edit())
    // that was already built for exactly this — it just never received a
    // live edit event to actually detect until now.
    await client.onMessageEdit(async (eventOrChat: any, _id?: string, legacyMessage?: any) => {
      // Current WPPConnect emits one { chat, id, msg } object even though its
      // public type still declares the legacy three-argument callback.
      //
      // The last fallback is a SHAPE check, not a bare `?? eventOrChat`. The
      // guard below exists precisely for the case where a wrapper arrives
      // with no `msg`, and accepting the wrapper itself defeats it: `{chat,
      // id}` is truthy and an object, so it would sail through and be
      // re-emitted on 'received-message' as if it were a serialized message.
      // Python then feeds it to on_new_message()'s same-id dedup and into
      // _apply_possible_edit(), which compares the stored text against a
      // body that does not exist — a much worse failure than a dropped edit,
      // and a silent one. A real serialized message always carries `type`
      // (or at least `body`); a `{chat, id}` wrapper carries neither.
      const looksSerialized =
        eventOrChat &&
        typeof eventOrChat === 'object' &&
        (eventOrChat.type !== undefined || eventOrChat.body !== undefined);
      const message =
        legacyMessage ?? eventOrChat?.msg ?? (looksSerialized ? eventOrChat : undefined);
      if (!message || typeof message !== 'object') {
        req.logger.warn(
          `[${client.session}] onMessageEdit emitted without a serialized message`
        );
        return;
      }
      message.session = client.session;
      req.io.emit('received-message', {
        response: message,
        session: client.session,
      });
      callWebHook(client, req, 'onmessageedit', message);
    });

    await client.onIncomingCall(async (call) => {
      const rawOfferTime = Number((call as any)?.offerTime || 0);
      const offerTimeMs =
        rawOfferTime >= 1e15
          ? Math.floor(rawOfferTime / 1000)
          : rawOfferTime >= 1e12
          ? Math.floor(rawOfferTime)
          : rawOfferTime >= 1e9
          ? Math.floor(rawOfferTime * 1000)
          : 0;
      const receivedWhileOffline =
        (call as any)?.offerReceivedWhileOffline === true;
      const predatesListener =
        offerTimeMs > 0 && offerTimeMs < incomingCallListenerStartedAt - 5000;

      if (receivedWhileOffline || predatesListener) {
        req.logger.info(
          '[incomingcall] ignored historical offer from WhatsApp state hydration'
        );
        return;
      }

      req.io.emit('incomingcall', {
        ...call,
        session: client.session,
        timestamp: offerTimeMs ? Math.floor(offerTimeMs / 1000) : 0,
        observedAt: Math.floor(Date.now() / 1000),
      });
      callWebHook(client, req, 'incomingcall', call);
    });
  }

  async listenAcks(client: WhatsAppServer, req: Request) {
    await client.onAck(async (ack) => {
      req.io.emit('onack', { ...ack, session: client.session });
      callWebHook(client, req, 'onack', ack);
    });
  }

  async onPresenceChanged(client: WhatsAppServer, req: Request) {
    await client.onPresenceChanged(async (presenceChangedEvent) => {
      req.io.emit('onpresencechanged', {
        ...presenceChangedEvent,
        session: client.session,
      });
      callWebHook(client, req, 'onpresencechanged', presenceChangedEvent);
    });
  }

  async onReactionMessage(client: WhatsAppServer, req: Request) {
    await client.isConnected();
    await client.onReactionMessage(async (reaction: any) => {
      req.io.emit('onreactionmessage', {
        ...reaction,
        session: client.session,
      });
      callWebHook(client, req, 'onreactionmessage', reaction);
    });
  }

  async onRevokedMessage(client: WhatsAppServer, req: Request) {
    await client.isConnected();
    await client.onRevokedMessage(async (response: any) => {
      req.io.emit('onrevokedmessage', {
        ...response,
        session: client.session,
      });
      callWebHook(client, req, 'onrevokedmessage', response);
    });
  }
  async onPollResponse(client: WhatsAppServer, req: Request) {
    await client.isConnected();
    await client.onPollResponse(async (response: any) => {
      req.io.emit('onpollresponse', {
        ...response,
        session: client.session,
      });
      callWebHook(client, req, 'onpollresponse', response);
    });
  }
  async onLabelUpdated(client: WhatsAppServer, req: Request) {
    await client.isConnected();
    await client.onUpdateLabel(async (response: any) => {
      req.io.emit('onupdatelabel', {
        ...response,
        session: client.session,
      });
      callWebHook(client, req, 'onupdatelabel', response);
    });
  }

  encodeFunction(data: any, webhook: any) {
    data.webhook = webhook;
    return JSON.stringify(data);
  }

  decodeFunction(text: any, client: any) {
    const object = JSON.parse(text);
    if (object.webhook && !client.webhook) client.webhook = object.webhook;
    delete object.webhook;
    return object;
  }

  getClient(session: any) {
    let client = clientsArray[session];

    if (!client)
      client = clientsArray[session] = {
        status: null,
        session: session,
      } as any;
    return client;
  }
}
