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
 * See the License for the specific language governing permclearSessionissions and
 * limitations under the License.
 */
import { Message, Whatsapp } from '@wppconnect-team/wppconnect';
import { Request, Response } from 'express';
import fs from 'fs';
import mime from 'mime-types';
import QRCode from 'qrcode';
import { Logger } from 'winston';

import { version } from '../../package.json';
import config from '../config';
import { observePayload, StatusSessionSchema } from '../dto/sync';
import {
  countJidFallback,
  observeEvaluate,
} from '../middleware/instrumentation';
import { resolveMessageForMedia } from '../services/messageResolver';
import CreateSessionUtil from '../util/createSessionUtil';
import { callWebHook, contactToArray } from '../util/functions';
import getAllTokens from '../util/getAllTokens';
import { clientsArray, deleteSessionOnArray } from '../util/sessionUtil';

const SessionUtil = new CreateSessionUtil();

async function downloadFileFunction(
  message: Message,
  client: Whatsapp,
  logger: Logger
) {
  try {
    const buffer = await client.decryptFile(message);

    const filename = `./WhatsAppImages/file${message.t}`;
    if (!fs.existsSync(filename)) {
      let result = '';
      if (message.type === 'ptt') {
        result = `${filename}.oga`;
      } else {
        result = `${filename}.${mime.extension(message.mimetype)}`;
      }

      await fs.writeFile(result, buffer, (err) => {
        if (err) {
          logger.error(err);
        }
      });

      return result;
    } else {
      return `${filename}.${mime.extension(message.mimetype)}`;
    }
  } catch (e) {
    logger.error(e);
    logger.warn(
      'Erro ao descriptografar a midia, tentando fazer o download direto...'
    );
    try {
      const buffer = await client.downloadMedia(message);
      const filename = `./WhatsAppImages/file${message.t}`;
      if (!fs.existsSync(filename)) {
        let result = '';
        if (message.type === 'ptt') {
          result = `${filename}.oga`;
        } else {
          result = `${filename}.${mime.extension(message.mimetype)}`;
        }

        await fs.writeFile(result, buffer, (err) => {
          if (err) {
            logger.error(err);
          }
        });

        return result;
      } else {
        return `${filename}.${mime.extension(message.mimetype)}`;
      }
    } catch (e) {
      logger.error(e);
      logger.warn('Não foi possível baixar a mídia...');
    }
  }
}

export async function download(message: any, client: any, logger: any) {
  try {
    const path = await downloadFileFunction(message, client, logger);
    return path?.replace('./', '');
  } catch (e) {
    logger.error(e);
  }
}

export async function startAllSessions(
  req: Request,
  res: Response
): Promise<any> {
  /**
   * #swagger.tags = ["Auth"]
     #swagger.autoBody=false
     #swagger.operationId = 'startAllSessions'
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.parameters["secretkey"] = {
      schema: 'THISISMYSECURECODE'
     }
   */
  const { secretkey } = req.params;
  const { authorization: token } = req.headers;

  let tokenDecrypt = '';

  if (secretkey === undefined) {
    tokenDecrypt = (token as any).split(' ')[0];
  } else {
    tokenDecrypt = secretkey;
  }

  const allSessions = await getAllTokens(req);

  if (tokenDecrypt !== req.serverOptions.secretKey) {
    res.status(400).json({
      response: 'error',
      message: 'The token is incorrect',
    });
  }

  allSessions.map(async (session: string) => {
    const util = new CreateSessionUtil();
    await util.opendata(req, session);
  });

  return await res
    .status(201)
    .json({ status: 'success', message: 'Starting all sessions' });
}

export async function showAllSessions(
  req: Request,
  res: Response
): Promise<any> {
  /**
   * #swagger.tags = ["Auth"]
     #swagger.autoBody=false
     #swagger.operationId = 'showAllSessions'
     #swagger.autoQuery=false
     #swagger.autoHeaders=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["secretkey"] = {
      schema: 'THISISMYSECURETOKEN'
     }
   */
  const { secretkey } = req.params;
  const { authorization: token } = req.headers;

  let tokenDecrypt: any = '';

  if (secretkey === undefined) {
    tokenDecrypt = token?.split(' ')[0];
  } else {
    tokenDecrypt = secretkey;
  }

  const arr: any = [];

  if (tokenDecrypt !== req.serverOptions.secretKey) {
    res.status(400).json({
      response: false,
      message: 'The token is incorrect',
    });
  }

  Object.keys(clientsArray).forEach((item) => {
    arr.push({ session: item });
  });

  res.status(200).json({ response: await getAllTokens(req) });
}

export async function startSession(req: Request, res: Response): Promise<any> {
  /**
   * #swagger.tags = ["Auth"]
     #swagger.autoBody=false
     #swagger.operationId = 'startSession'
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.requestBody = {
      required: true,
      "@content": {
        "application/json": {
          schema: {
            type: "object",
            properties: {
              webhook: { type: "string" },
              waitQrCode: { type: "boolean" },
              proxy: {
                type: "object",
                properties: {
                  url: { type: "string" },
                  username: { type: "string" },
                  password: { type: "string" },
                }
              }
            }
          },
          example: {
            webhook: "",
            waitQrCode: false,
            proxy: {
              url: "http://myproxy.com:8080",
              username: "myuser",
              password: "mypassword"
            }
          }
        }
      }
     }
   */
  const session = req.session;
  const { waitQrCode = false } = req.body;

  await getSessionState(req, res);
  await SessionUtil.opendata(req, session, waitQrCode ? res : null);
}

export async function closeSession(req: Request, res: Response): Promise<any> {
  /**
   * #swagger.tags = ["Auth"]
     #swagger.operationId = 'closeSession'
     #swagger.autoBody=true
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  const session = req.session;
  try {
    const client = (clientsArray as any)[session];
    if (!client) {
      return await res
        .status(200)
        .json({ status: true, message: 'Session successfully closed' });
    }

    // WinZapp fix: Force-kill unauthenticated/QRCODE sessions to instantly kill
    // orphaned Chrome processes when abandoning pairing, but close CONNECTED/open
    // sessions gracefully to preserve LevelDB pairing state.
    if (client.status !== 'CONNECTED' && client.status !== 'open') {
      req.logger.info(
        `[${session}] Force killing session because status is ${client.status}`
      );
      client.shouldClose = true;
      try {
        SessionUtil.forceKillSession(session, req.logger);
      } catch (e) {}
      (clientsArray as any)[session] = undefined;
      return await res
        .status(200)
        .json({ status: true, message: 'Session force closed' });
    }

    // WinZapp fix: this placeholder has to be distinguishable from "gone".
    //
    // It exists so requests arriving DURING the close see an unusable client:
    // statusConnection.ts tests `req.client && req.client.isConnected`, and a
    // stub without that property falls through to 404 Disconnected — which the
    // MessageQueue relies on to classify a send as "do not resend". That part
    // must not change.
    //
    // But it used to be `{ status: null }`, and getSessionState() answers
    // `{status: 'CLOSED'}` for `client == null || client.status == null`. So
    // status-session started reporting CLOSED the instant this line ran —
    // before `await req.client.close()` below had done anything. WinZapp's
    // shutdown polls exactly that endpoint to decide the browser has finished
    // flushing its profile (_wait_for_session_flushed) and then force-kills the
    // Node tree, Chrome included. The poll could never observe the flush: every
    // shutdown_audit.log from the field shows it satisfied on poll #1 at 0.1s,
    // and a Chrome killed mid-write leaves a torn leveldb that WhatsApp Web
    // reads as a logged-out device on the next launch — the phone still listing
    // it under Linked Devices.
    //
    // 'CLOSING' keeps the stub (still no isConnected, still 404) but makes the
    // status honest: connection_state.session_closed_after_flush() rejects it,
    // so the poll waits for the `finally` below, which only runs once close()
    // has actually returned.
    const closingMarker = { status: 'CLOSING' };
    (clientsArray as any)[session] = closingMarker;

    try {
      if (req.client && typeof req.client.close === 'function') {
        // Bounded, because 'CLOSING' above is now a state that BLOCKS a
        // concurrent /start-session (createSessionUtil returns early for any
        // status that is neither null nor 'CLOSED'). That block is what we
        // want — starting a browser while the previous one still owns
        // userDataDir/<session> is the "The browser is already running"
        // collision — but only for as long as the close actually lasts.
        //
        // wppconnect's own close() cannot be trusted to settle: it wraps both
        // page.close() and browser.close() in `.catch(() => null)`, so a
        // browser that never dies (a suspended chrome.exe, the known
        // post-hibernation case) leaves the await pending forever, and the
        // `finally` that clears this slot would never run. The session would
        // be permanently unstartable — recoverable only by restarting Node.
        //
        // Racing it keeps the slot self-clearing. WinZapp's own POST budget
        // (_WPP_GRACEFUL_STOP_SECONDS) is 10s, so this has to answer first.
        // Promise.race does not cancel its losing promise. The old inline
        // setTimeout therefore still fired eight seconds after close() had
        // already won, by which time WinZapp could have started a replacement
        // browser under the same session name. forceKillSession(session) then
        // killed that brand-new browser and left the session in INITIALIZING
        // with a detached Puppeteer frame. Keep the handle and explicitly
        // cancel the timeout as soon as close() settles.
        let closeTimeout: ReturnType<typeof setTimeout> | undefined;
        const closeTimeoutPromise = new Promise<void>((resolve) => {
          closeTimeout = setTimeout(() => {
            req.logger?.warn?.(
              `[${session}] close() did not settle in 8s — force killing so ` +
                `the session slot is not left stuck in CLOSING.`
            );
            try {
              SessionUtil.forceKillSession(session, req.logger);
            } catch (e) {}
            resolve();
          }, 8000);
        });
        try {
          await Promise.race([req.client.close(), closeTimeoutPromise]);
        } finally {
          if (closeTimeout !== undefined) clearTimeout(closeTimeout);
        }
      }
    } catch (closeErr) {
      req.logger?.warn?.(
        `[${session}] Error during req.client.close(): ${closeErr}. Force killing session.`
      );
      try {
        SessionUtil.forceKillSession(session, req.logger);
      } catch (e) {}
    } finally {
      // Do not let an old close request erase a replacement client that may
      // have claimed this session slot after an exceptional teardown path.
      if ((clientsArray as any)[session] === closingMarker) {
        (clientsArray as any)[session] = undefined;
      }
    }

    req.io.emit('whatsapp-status', false);
    callWebHook(req.client, req, 'closesession', {
      message: `Session: ${session} disconnected`,
      connected: false,
    });

    return await res
      .status(200)
      .json({ status: true, message: 'Session successfully closed' });
  } catch (error) {
    req.logger.error(error);
    (clientsArray as any)[session] = undefined;
    return await res
      .status(500)
      .json({ status: false, message: 'Error closing session', error });
  }
}

export async function logOutSession(req: Request, res: Response): Promise<any> {
  /**
   * #swagger.tags = ["Auth"]
     #swagger.operationId = 'logoutSession'
   * #swagger.description = 'This route logout and delete session data'
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    const session = req.session;
    await req.client.logout();
    deleteSessionOnArray(req.session);

    setTimeout(async () => {
      const pathUserData = config.customUserDataDir + req.session;
      const pathTokens = __dirname + `../../../tokens/${req.session}.data.json`;

      if (fs.existsSync(pathUserData)) {
        await fs.promises.rm(pathUserData, {
          recursive: true,
          maxRetries: 5,
          force: true,
          retryDelay: 1000,
        });
      }
      if (fs.existsSync(pathTokens)) {
        await fs.promises.rm(pathTokens, {
          recursive: true,
          maxRetries: 5,
          force: true,
          retryDelay: 1000,
        });
      }

      req.io.emit('whatsapp-status', false);
      callWebHook(req.client, req, 'logoutsession', {
        message: `Session: ${session} logged out`,
        connected: false,
      });

      return await res
        .status(200)
        .json({ status: true, message: 'Session successfully closed' });
    }, 500);
    /*try {
      await req.client.close();
    } catch (error) {}*/
  } catch (error) {
    req.logger.error(error);
    res
      .status(500)
      .json({ status: false, message: 'Error closing session', error });
  }
}

export async function checkConnectionSession(
  req: Request,
  res: Response
): Promise<any> {
  /**
   * #swagger.tags = ["Auth"]
     #swagger.operationId = 'CheckConnectionState'
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    await req.client.isConnected();

    res.status(200).json({ status: true, message: 'Connected' });
  } catch (error) {
    res.status(200).json({ status: false, message: 'Disconnected' });
  }
}

// WinZapp patch: nudge WhatsApp Web's own multi-device socket back open.
//
// Reported live: after the OS resumes from sleep, WinZapp's status-session
// probe keeps reporting the WPPConnect session object as "CONNECTED" (that
// string is just cached at session creation — see checkConnectionSession's
// own comment above it), but the *live* isConnected() probe never comes back
// true again, forever — the app is stuck offline until the whole program is
// restarted (a fresh Puppeteer/Chrome + fresh page).
//
// The real WhatsApp Web client re-opens its socket stream via
// WPP.whatsapp.Cmd.openSocketStream() — normally triggered by the page's own
// visibility/focus/online DOM events. This session's Chrome page runs
// headless and is never focused or brought to the foreground, so nothing
// ever fires those events after a suspend/resume cycle — the socket that
// went down during sleep has no trigger left to reconnect it, unlike a real,
// visible browser tab a user might click back into. Calling the same
// internal command directly reproduces whatever a focus/visibility event
// would have triggered on a normal tab.
export async function reconnectSocketStream(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Auth"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    const page = (req.client as any)?.page;
    if (!page || page.isClosed()) {
      return res.status(400).json({
        status: 'error',
        message: 'The WhatsApp session is not active.',
      });
    }
    const result = await page.evaluate(() => {
      try {
        const wpp = (window as any).WPP;
        if (wpp?.whatsapp?.Cmd?.openSocketStream) {
          wpp.whatsapp.Cmd.openSocketStream();
          return { ok: true };
        }
        return {
          ok: false,
          error: 'WPP.whatsapp.Cmd.openSocketStream not available',
        };
      } catch (e: any) {
        return { ok: false, error: e?.message || String(e) };
      }
    });
    if (!result?.ok) {
      req.logger.warn(
        `[reconnectSocketStream] ${result?.error || 'unknown failure'}`
      );
    }
    res.status(200).json({ status: 'success', response: result });
  } catch (error: any) {
    req.logger.error(error);
    res.status(500).json({
      status: 'error',
      message: error?.message || String(error),
    });
  }
}

export async function downloadMediaByMessage(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Messages"]
     #swagger.autoBody=false
     #swagger.operationId = 'downloadMediabyMessage'
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.requestBody = {
      required: true,
      "@content": {
        "application/json": {
          schema: {
            type: "object",
            properties: {
              messageId: { type: "string" },
            }
          },
          example: {
            messageId: '<messageId>'
          }
        }
      }
     }
   */
  const client = req.client;
  const { messageId } = req.body;

  if (!client || typeof client.getMessageById !== 'function') {
    return res.status(400).json({
      status: 'error',
      message: 'The WhatsApp session is not active.',
    });
  }

  let message;

  try {
    if (!messageId.isMedia || !messageId.type) {
      message = await client.getMessageById(messageId);
    } else {
      message = messageId;
    }

    if (!message)
      res.status(400).json({
        status: 'error',
        message: 'Message not found',
      });

    if (!(message['mimetype'] || message.isMedia || message.isMMS))
      res.status(400).json({
        status: 'error',
        message: 'Message does not contain media',
      });

    const buffer = await client.decryptFile(message);

    res
      .status(200)
      .json({ base64: buffer.toString('base64'), mimetype: message.mimetype });
  } catch (e) {
    req.logger.error(e);
    res.status(400).json({
      status: 'error',
      message: 'Decrypt file error',
      error: e,
    });
  }
}

export async function getMediaByMessage(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Messages"]
     #swagger.autoBody=false
     #swagger.operationId = 'getMediaByMessage'
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.parameters["session"] = {
      schema: 'messageId'
     }
   */
  const client = req.client;
  const { messageId } = req.params;

  if (!client || typeof client.getMessageById !== 'function') {
    return res.status(400).json({
      status: 'error',
      message: 'The WhatsApp session is not active.',
    });
  }

  // Clean messageId for WPPConnect store lookups:
  // e.g. "true_120363409931936700@g.us_3EB056E440332317DE11FA_117841202282688:86@lid" -> "true_120363409931936700@g.us_3EB056E440332317DE11FA"
  const cleanMsgId =
    messageId && messageId.includes('_')
      ? messageId.split('_').slice(0, 3).join('_')
      : messageId;

  try {
    let message: any = null;

    // Only bypass Puppeteer lookup if the body contains a complete media download URL (clientUrl or directPath).
    // If only mediaKey is present, we must query Puppeteer to retrieve the full message object with valid CDN URLs.
    const bodyHasUrl = req.body && (req.body.clientUrl || req.body.directPath);
    if (bodyHasUrl && req.body.mediaKey) {
      req.logger.info(
        `Received full media decryption details in body for message ${messageId}. Bypassing Puppeteer lookup.`
      );
      message = req.body;
      if (typeof message.mediaKey === 'object' && message.mediaKey?.data) {
        message.mediaKey = Buffer.from(message.mediaKey.data);
      } else if (typeof message.mediaKey === 'string') {
        message.mediaKey = Buffer.from(message.mediaKey, 'base64');
      }
    } else {
      // The chain that finds a message WhatsApp Web may have unloaded, or
      // filed under its other JID form. Six increasingly expensive attempts,
      // cheapest first — see resolveMessageForMedia(), where it now lives.
      message = await resolveMessageForMedia({
        client,
        messageId,
        cleanMsgId,
        body: req.body,
        logger: req.logger,
      });
    }

    // Normalise clientUrl and directPath from body fields if present
    if (message) {
      if (!message.clientUrl && message.url) {
        message.clientUrl = message.url;
      }
      if (!message.clientUrl && message.directPath) {
        const path = message.directPath.startsWith('/')
          ? message.directPath
          : `/${message.directPath}`;
        message.clientUrl = `https://mmg.whatsapp.net${path}`;
        req.logger.info(
          `Reconstructed clientUrl using directPath for message ${cleanMsgId}: ${message.clientUrl}`
        );
      }
    }

    if (!message) {
      return res.status(400).json({
        status: 'error',
        message: `Message ${messageId} not found`,
      });
    }

    // Ensure client browser context is alive
    if (client.page && client.page.isClosed()) {
      req.logger.warn(
        `Browser page is closed for session when downloading media ${messageId}`
      );
      return res.status(503).json({
        status: 'error',
        message: 'Browser session is closed or re-connecting',
      });
    }

    // Reconstruct clientUrl from directPath if clientUrl is missing
    if (!message.clientUrl && message.directPath) {
      const path = message.directPath.startsWith('/')
        ? message.directPath
        : `/${message.directPath}`;
      message.clientUrl = `https://mmg.whatsapp.net${path}`;
      req.logger.info(
        `Reconstructed clientUrl using directPath for message ${cleanMsgId}: ${message.clientUrl}`
      );
    }

    // Ensure it contains media properties or has mimetype
    const mediaUrl = message.clientUrl || message.deprecatedMms3Url;
    if (!mediaUrl) {
      if (
        typeof (client as any).downloadMedia === 'function' &&
        client.page &&
        !client.page.isClosed()
      ) {
        req.logger.info(
          `Message ${cleanMsgId} does not have clientUrl. Trying client.downloadMedia with 30s timeout...`
        );
        try {
          let timer: any;
          const targetId = cleanMsgId || messageId;
          const downloadPromise = (client as any)
            .downloadMedia(targetId)
            .catch((err: any) => {
              req.logger.warn(
                `client.downloadMedia caught inner error: ${err}`
              );
              return null;
            })
            .finally(() => {
              if (timer) clearTimeout(timer);
            });
          const timeoutPromise = new Promise<null>((resolve) => {
            timer = setTimeout(() => {
              req.logger.warn(
                `Timeout 30000ms reached for client.downloadMedia (${targetId})`
              );
              resolve(null);
            }, 30000);
          });
          let base64: string | null = await Promise.race([
            downloadPromise,
            timeoutPromise,
          ]);
          if (base64) {
            let mimetype = message.mimetype || 'audio/ogg';
            if (base64.startsWith('data:')) {
              const matches = base64.match(/^data:(.*?);base64,(.*)$/);
              if (matches) {
                mimetype = matches[1];
                base64 = matches[2];
              }
            }
            return res.status(200).json({ base64, mimetype });
          }
        } catch (downloadErr) {
          req.logger.error(
            `Error in client.downloadMedia fallback: ${downloadErr}`
          );
        }
      }
      return res.status(400).json({
        status: 'error',
        message: 'Message does not contain media download URL',
      });
    }

    try {
      const buffer = await client.decryptFile(message);
      res.status(200).json({
        base64: buffer.toString('base64'),
        mimetype: message.mimetype || 'audio/ogg',
      });
    } catch (decryptErr) {
      req.logger.error(
        `decryptFile failed, trying browser-side recovery: ${decryptErr}`
      );

      // Attempt browser-side recovery: fetch the message fresh from WhatsApp Web to get updated CDN URLs
      let freshMessage: any = null;
      if (client.page && !client.page.isClosed()) {
        try {
          freshMessage = await client.getMessageById(cleanMsgId);
        } catch (err) {}

        if (!freshMessage && cleanMsgId) {
          const parts = cleanMsgId.split('_');
          if (parts.length >= 2) {
            const chatId = parts[1];
            if (chatId && typeof client.loadEarlierMessages === 'function') {
              try {
                await client.loadEarlierMessages(chatId);
                freshMessage = await client.getMessageById(cleanMsgId);
              } catch (err) {}
            }
          }
        }
      }

      if (freshMessage) {
        try {
          if (!freshMessage.clientUrl && freshMessage.directPath) {
            const path = freshMessage.directPath.startsWith('/')
              ? freshMessage.directPath
              : `/${freshMessage.directPath}`;
            freshMessage.clientUrl = `https://mmg.whatsapp.net${path}`;
          }
          req.logger.info(
            `Found fresh message in browser for ${cleanMsgId}, attempting decryption...`
          );
          const buffer = await client.decryptFile(freshMessage);
          return res.status(200).json({
            base64: buffer.toString('base64'),
            mimetype: freshMessage.mimetype || 'audio/ogg',
          });
        } catch (freshDecryptErr) {
          req.logger.error(
            `Decryption of fresh browser message failed: ${freshDecryptErr}`
          );
        }
      }

      // Direct browser-side WA-JS downloadMedia recovery (bypasses hanging Node-side downloadMedia)
      if (client.page && !client.page.isClosed()) {
        try {
          const targetId = cleanMsgId || messageId;
          req.logger.info(
            `Attempting direct browser WPP.chat.downloadMedia recovery for ${targetId}...`
          );

          let base64: string | null = await client.page
            .evaluate(async (msgId: string) => {
              try {
                const w = window as any;
                if (w.WPP && w.WPP.chat) {
                  // Find message to get its chat ID
                  const msg =
                    typeof w.WPP.chat.getMessageById === 'function'
                      ? await w.WPP.chat.getMessageById(msgId).catch(() => null)
                      : null;
                  const chatId =
                    msg?.chatId?._serialized ||
                    msg?.from ||
                    msgId.split('_')[1];
                  if (chatId && typeof w.WPP.chat.openChatAt === 'function') {
                    await w.WPP.chat.openChatAt(chatId).catch(() => null);
                  }
                  if (typeof w.WPP.chat.downloadMedia === 'function') {
                    const downloadPromise = w.WPP.chat.downloadMedia(msgId);
                    const timeoutPromise = new Promise((res) =>
                      setTimeout(() => res(null), 8000)
                    );
                    const blob = await Promise.race([
                      downloadPromise,
                      timeoutPromise,
                    ]);
                    if (
                      blob &&
                      w.WPP.util &&
                      typeof w.WPP.util.blobToBase64 === 'function'
                    ) {
                      return await w.WPP.util.blobToBase64(blob);
                    }
                  }
                }
              } catch (e) {
                console.error('WPP.chat.downloadMedia evaluate error:', e);
              }
              return null;
            }, targetId)
            .catch((err: any) => {
              req.logger.warn(
                `Direct WPP.chat.downloadMedia evaluate failed: ${err}`
              );
              return null;
            });

          if (base64) {
            req.logger.info(
              `Browser-side WPP.chat.downloadMedia recovery succeeded for ${targetId}!`
            );
            let mimetype = (freshMessage || message).mimetype || 'audio/ogg';
            if (base64.startsWith('data:')) {
              const matches = base64.match(/^data:(.*?);base64,(.*)$/);
              if (matches) {
                mimetype = matches[1];
                base64 = matches[2];
              }
            }
            return res.status(200).json({ base64, mimetype });
          } else {
            req.logger.warn(
              `Browser-side WPP.chat.downloadMedia recovery returned no blob for ${targetId}.`
            );
          }
        } catch (downloadErr) {
          req.logger.error(
            `Error in browser-side downloadMedia recovery: ${downloadErr}`
          );
        }
      }
      throw decryptErr; // rethrow to trigger the 500 block if all recovery attempts failed
    }
  } catch (ex) {
    req.logger.error(ex);
    res.status(500).json({
      status: 'error',
      message: 'Failed to decrypt file',
      error: ex instanceof Error ? ex.message : ex,
    });
  }
}

export async function getSessionState(req: Request, res: Response) {
  /**
     #swagger.tags = ["Auth"]
     #swagger.operationId = 'getSessionState'
     #swagger.summary = 'Retrieve status of a session'
     #swagger.autoBody = false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    const { waitQrCode = false } = req.body;
    const client = req.client;
    const qr =
      client?.urlcode != null && client?.urlcode != ''
        ? await QRCode.toDataURL(client.urlcode)
        : null;

    if ((client == null || client.status == null) && !waitQrCode)
      res.status(200).json({ status: 'CLOSED', qrcode: null });
    else if (client != null)
      res.status(200).json(
        observePayload(
          StatusSessionSchema,
          {
            status: client.status,
            qrcode: qr,
            urlcode: client.urlcode,
            version: version,
          },
          { logger: req.logger, endpoint: 'status-session' }
        )
      );
  } catch (ex) {
    req.logger.error(ex);
    res.status(500).json({
      status: 'error',
      message: 'The session is not active',
      error: ex,
    });
  }
}

export async function getQrCode(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Auth"]
     #swagger.autoBody=false
     #swagger.operationId = 'getQrCode'
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    if (req?.client?.urlcode) {
      // We add options to generate the QR code in higher resolution
      // The /qrcode-session request will now return a readable qrcode.
      const qrOptions = {
        errorCorrectionLevel: 'M' as const,
        type: 'image/png' as const,
        scale: 5,
        width: 500,
      };
      const qr = req.client.urlcode
        ? await QRCode.toDataURL(req.client.urlcode, qrOptions)
        : null;
      const img = Buffer.from(
        (qr as any).replace(/^data:image\/(png|jpeg|jpg);base64,/, ''),
        'base64'
      );
      res.writeHead(200, {
        'Content-Type': 'image/png',
        'Content-Length': img.length,
      });
      res.end(img);
    } else if (typeof req.client === 'undefined') {
      res.status(200).json({
        status: null,
        message:
          'Session not started. Please, use the /start-session route, for initialization your session',
      });
    } else {
      res.status(200).json({
        status: req.client.status,
        message: 'QRCode is not available...',
      });
    }
  } catch (ex) {
    req.logger.error(ex);
    res
      .status(500)
      .json({ status: 'error', message: 'Error retrieving QRCode', error: ex });
  }
}

export async function killServiceWorker(req: Request, res: Response) {
  /**
   * #swagger.ignore=true
   * #swagger.tags = ["Messages"]
     #swagger.operationId = 'killServiceWorkier'
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    res.status(200).json({ status: 'error', response: 'Not implemented yet' });
  } catch (ex) {
    req.logger.error(ex);
    res.status(500).json({
      status: 'error',
      message: 'The session is not active',
      error: ex,
    });
  }
}

export async function restartService(req: Request, res: Response) {
  /**
   * #swagger.ignore=true
   * #swagger.tags = ["Messages"]
     #swagger.operationId = 'restartService'
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    res.status(200).json({ status: 'error', response: 'Not implemented yet' });
  } catch (ex) {
    req.logger.error(ex);
    res.status(500).json({
      status: 'error',
      response: { message: 'The session is not active', error: ex },
    });
  }
}

export async function subscribePresence(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Misc"]
     #swagger.operationId = 'subscribePresence'
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.requestBody = {
      required: true,
      "@content": {
        "application/json": {
          schema: {
            type: "object",
            properties: {
              phone: { type: "string" },
              isGroup: { type: "boolean" },
              all: { type: "boolean" },
            }
          },
          example: {
            phone: '5521999999999',
            isGroup: false,
            all: false,
          }
        }
      }
     }
   */
  try {
    const { phone, isGroup = false, all = false, isLid = false } = req.body;

    const subscribeOne = async (contato: string) => {
      // Prefer the modern WPP.contact.subscribePresence which works with
      // current WhatsApp Web. The legacy req.client.subscribePresence uses
      // the internal WAPI that calls Store.Presence.find() — broken in newer
      // WA versions and returns 500. We fall back to the legacy path if the
      // WPP API is not available.
      const page = (req.client as any).page;
      if (page) {
        try {
          await page.evaluate((id: string) => {
            const wpp = (window as any).WPP;
            if (
              wpp &&
              wpp.contact &&
              typeof wpp.contact.subscribePresence === 'function'
            ) {
              return wpp.contact.subscribePresence(id);
            }
            // Fallback to WPP.whatsapp.PresenceUtils if available
            if (wpp && wpp.whatsapp && wpp.whatsapp.PresenceUtils) {
              return wpp.whatsapp.PresenceUtils.subscribeToPresence(id);
            }
            throw new Error('WPP.contact.subscribePresence not available');
          }, contato);
          req.logger.info(`[subscribePresence] WPP subscribed: ${contato}`);
          return;
        } catch (wppErr) {
          req.logger.warn(
            `[subscribePresence] WPP fallback for ${contato}: ${wppErr}`
          );
        }
      }
      // Legacy fallback
      await req.client.subscribePresence(contato);
    };

    if (all) {
      let contacts;
      if (isGroup) {
        const groups = await req.client.getAllGroups(false);
        contacts = groups.map((p: any) => p.id._serialized);
      } else {
        const chats = await req.client.getAllContacts();
        contacts = chats.map((c: any) => c.id._serialized);
      }
      for (const contato of contacts) {
        await subscribeOne(contato);
      }
    } else {
      for (const contato of contactToArray(phone, isGroup, false, isLid)) {
        await subscribeOne(contato);
      }
    }

    res.status(200).json({
      status: 'success',
      response: { message: 'Subscribe presence executed' },
    });
  } catch (error) {
    req.logger.error(error);
    res.status(500).json({
      status: 'error',
      message: 'Error on subscribe presence',
      error: error,
    });
  }
}

export async function setOnlinePresence(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Misc"]
     #swagger.operationId = 'setOnlinePresence'
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.requestBody = {
      required: true,
      "@content": {
        "application/json": {
          schema: {
            type: "object",
            properties: {
              isOnline: { type: "boolean" },
            }
          },
          example: {
   isOnline: false,
          }
        }
      }
     }
   */
  try {
    const { isOnline = true } = req.body;

    await req.client.setOnlinePresence(isOnline);

    res.status(200).json({
      status: 'success',
      response: { message: 'Set Online Presence Successfully' },
    });
  } catch (error) {
    res.status(500).json({
      status: 'error',
      message: 'Error on set online presence',
      error: error,
    });
  }
}

export async function editBusinessProfile(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Profile"]
     #swagger.operationId = 'editBusinessProfile'
   * #swagger.description = 'Edit your bussiness profile'
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.parameters["obj"] = {
      in: 'body',
      schema: {
        $adress: 'Av. Nossa Senhora de Copacabana, 315',
        $email: 'test@test.com.br',
        $categories: {
          $id: "133436743388217",
          $localized_display_name: "Artes e entretenimento",
          $not_a_biz: false,
        },
        $website: [
          "https://www.wppconnect.io",
          "https://www.teste2.com.br",
        ],
      }
     }
     
     #swagger.requestBody = {
      required: true,
      "@content": {
        "application/json": {
          schema: {
            type: "object",
            properties: {
              adress: { type: "string" },
              email: { type: "string" },
              categories: { type: "object" },
              websites: { type: "array" },
            }
          },
          example: {
            adress: 'Av. Nossa Senhora de Copacabana, 315',
            email: 'test@test.com.br',
            categories: {
              $id: "133436743388217",
              $localized_display_name: "Artes e entretenimento",
              $not_a_biz: false,
            },
            website: [
              "https://www.wppconnect.io",
              "https://www.teste2.com.br",
            ],
          }
        }
      }
     }
   */
  try {
    res.status(200).json(await req.client.editBusinessProfile(req.body));
  } catch (error) {
    res.status(500).json({
      status: 'error',
      message: 'Error on edit business profile',
      error: error,
    });
  }
}
