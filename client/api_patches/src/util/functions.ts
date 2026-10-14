/*
 * Copyright 2023 WPPConnect Team
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
import api from 'axios';
import Crypto from 'crypto';
import { Request } from 'express';
import fs from 'fs';
import mimetypes from 'mime-types';
import os from 'os';
import path from 'path';
import { promisify } from 'util';

import config from '../config';
import { convert } from '../mapper/index';
import { ServerOptions } from '../types/ServerOptions';
import { WhatsAppServer } from '../types/WhatsAppServer';

let mime: any, crypto: any; //, aws: any;
if (config.webhook.uploadS3) {
  mime = config.webhook.uploadS3 ? mimetypes : null;
  crypto = config.webhook.uploadS3 ? Crypto : null;
}
if (config?.websocket?.uploadS3) {
  mime = config.websocket.uploadS3 ? mimetypes : null;
  crypto = config.websocket.uploadS3 ? Crypto : null;
}

export function contactToArray(
  number: any,
  isGroup?: boolean,
  isNewsletter?: boolean,
  isLid?: boolean
) {
  const localArr: any = [];
  // WinZapp calls every endpoint that flows through this function as
  // multipart/form-data (see main.py's send_media_attachment) — a wire
  // format with no native boolean type, so isGroup/isNewsletter/isLid
  // routinely arrive as the literal STRING "false", not the boolean
  // `false`. `isGroup || isNewsletter` treats ANY non-empty string
  // (including "false") as truthy, so this silently misrouted every
  // multipart contact — e.g. a plain 1:1 send got its digits treated as a
  // group id and suffixed "@g.us" instead of "@c.us". Coercing explicitly
  // (matching either the real boolean or its string form) fixes this
  // without touching genuine JSON callers, which already send real
  // booleans and still compare equal here.
  const wantsGroup = isGroup === true || (isGroup as any) === 'true';
  const wantsNewsletter =
    isNewsletter === true || (isNewsletter as any) === 'true';
  const wantsLid = isLid === true || (isLid as any) === 'true';
  if (Array.isArray(number)) {
    for (let contact of number) {
      const isLidContact =
        typeof contact === 'string' && contact.endsWith('@lid');
      wantsGroup || wantsNewsletter
        ? (contact = contact.split('@')[0])
        : (contact = contact.split('@')[0]?.replace(/[^\w ]/g, ''));
      if (contact !== '')
        if (wantsGroup) (localArr as any).push(`${contact}@g.us`);
        else if (wantsNewsletter)
          (localArr as any).push(`${contact}@newsletter`);
        else if (wantsLid || isLidContact)
          (localArr as any).push(`${contact}@lid`);
        else (localArr as any).push(`${contact}@c.us`);
    }
  } else {
    // number can arrive as a non-string (e.g. a bare number) via
    // multipart/form-data field coercion — String(...) guards .split()
    // from throwing on it instead of only ever expecting a real string.
    const arrContacts =
      typeof number === 'string'
        ? number.split(/\s*[,;]\s*/g)
        : [String(number ?? '')];
    for (let contact of arrContacts) {
      const isLidContact =
        typeof contact === 'string' && contact.endsWith('@lid');
      wantsGroup || wantsNewsletter
        ? (contact = contact.split('@')[0])
        : (contact = contact.split('@')[0]?.replace(/[^\w ]/g, ''));
      if (contact !== '')
        if (wantsGroup) (localArr as any).push(`${contact}@g.us`);
        else if (wantsNewsletter)
          (localArr as any).push(`${contact}@newsletter`);
        else if (wantsLid || isLidContact)
          (localArr as any).push(`${contact}@lid`);
        else (localArr as any).push(`${contact}@c.us`);
    }
  }

  return localArr;
}

export function groupToArray(group: any) {
  const localArr: any = [];
  if (Array.isArray(group)) {
    for (let contact of group) {
      contact = contact.split('@')[0];
      if (contact !== '') (localArr as any).push(`${contact}@g.us`);
    }
  } else {
    const arrContacts = group.split(/\s*[,;]\s*/g);
    for (let contact of arrContacts) {
      contact = contact.split('@')[0];
      if (contact !== '') (localArr as any).push(`${contact}@g.us`);
    }
  }

  return localArr;
}

export function groupNameToArray(group: any) {
  const localArr: any = [];
  if (Array.isArray(group)) {
    for (const contact of group) {
      if (contact !== '') (localArr as any).push(`${contact}`);
    }
  } else {
    const arrContacts = group.split(/\s*[,;]\s*/g);
    for (const contact of arrContacts) {
      if (contact !== '') (localArr as any).push(`${contact}`);
    }
  }

  return localArr;
}

export async function callWebHook(
  client: any,
  req: Request,
  event: any,
  data: any
) {
  const requestedWebhook = client?.config.webhook;
  const configured = req.serverOptions.webhook;
  // Session configuration can select only destinations explicitly configured
  // by the server operator. Return the trusted configuration value itself.
  const webhook = requestedWebhook
    ? [configured.url, ...(configured.allowedUrls || [])].find(
        (url) => url === requestedWebhook
      )
    : configured.url;
  if (requestedWebhook && !webhook) {
    req.logger.warn('Session webhook URL is not in the configured allowlist.');
    return;
  }
  if (webhook) {
    if (
      req.serverOptions.webhook?.ignore &&
      (req.serverOptions.webhook.ignore.includes(event) ||
        req.serverOptions.webhook.ignore.includes(data?.from) ||
        req.serverOptions.webhook.ignore.includes(data?.type))
    )
      return;
    if (req.serverOptions.webhook.autoDownload)
      await autoDownload(client, req, data);
    try {
      const chatId =
        data.from ||
        data.chatId ||
        (data.chatId ? data.chatId._serialized : null);
      data = Object.assign({ event: event, session: client.session }, data);
      if (req.serverOptions.mapper.enable)
        data = await convert(req.serverOptions.mapper.prefix, data);
      const options = req.serverOptions.webhook;
      const attempts = webhookNumber(options.retryAttempts, 3, 1, 10);
      const delay = webhookNumber(options.retryDelay, 5000, 0, 60000);
      const timeout = webhookNumber(options.requestTimeout, 30000, 1, 120000);
      for (let attempt = 0; attempt < attempts; attempt++) {
        try {
          await api.post(webhook, data, { timeout });
        } catch (error) {
          const code =
            error && typeof error === 'object' && 'code' in error
              ? error.code
              : undefined;
          const transient =
            !(
              error &&
              typeof error === 'object' &&
              'response' in error &&
              error.response
            ) &&
            typeof code === 'string' &&
            [
              'ECONNREFUSED',
              'ETIMEDOUT',
              'ECONNABORTED',
              'ECONNRESET',
              'EAI_AGAIN',
            ].includes(code);
          if (!transient || attempt + 1 === attempts) {
            req.logger.warn('Error calling Webhook.', error);
            return;
          }
          await new Promise((resolve) =>
            setTimeout(resolve, Math.min(delay * 2 ** attempt, 60000))
          );
          continue;
        }
        try {
          const events = ['unreadmessages', 'onmessage'];
          if (events.includes(event) && options.readMessage) {
            await client.sendSeen(chatId);
          }
        } catch (error) {
          req.logger.warn('Error marking webhook message as read.', error);
        }
        return;
      }
    } catch (e) {
      req.logger.error(e);
    }
  }
}

function webhookNumber(
  value: number | undefined,
  fallback: number,
  min: number,
  max: number
) {
  return typeof value === 'number' && Number.isFinite(value)
    ? Math.min(max, Math.max(min, Math.floor(value)))
    : fallback;
}

export async function autoDownload(client: any, req: any, message: any) {
  try {
    if (message && (message['mimetype'] || message.isMedia || message.isMMS)) {
      const uploadS3 =
        req.serverOptions.webhook.uploadS3 ||
        req.serverOptions?.websocket?.uploadS3;
      // Inlined base64 grows the payload ~33% over the file size, so large
      // media can exceed the receiver's body limit and the event is lost.
      const maxSize = req.serverOptions?.webhook?.autoDownloadMaxSize;
      if (!uploadS3 && maxSize > 0 && message.size > maxSize) {
        message.autoDownloadSkipped = true;
        return;
      }
      const buffer = await client.decryptFile(message);
      if (uploadS3) {
        const { CreateBucketCommand, PutObjectCommand, PutPublicAccessBlockCommand, S3Client } =
          require('@aws-sdk/client-s3') as typeof import('@aws-sdk/client-s3');
        const { bucketAlreadyExists } = require('./bucketAlreadyExists') as typeof import('./bucketAlreadyExists');
        const hashName = crypto.randomBytes(24).toString('hex');

        if (
          !config?.aws_s3?.region ||
          !config?.aws_s3?.access_key_id ||
          !config?.aws_s3?.secret_key
        )
          throw new Error('Please, configure your aws configs');
        const s3Client = new S3Client({
          region: config?.aws_s3?.region,
          endpoint: config?.aws_s3?.endpoint || undefined,
          forcePathStyle: config?.aws_s3?.forcePathStyle || undefined,
        });
        let bucketName = config?.aws_s3?.defaultBucketName
          ? config?.aws_s3?.defaultBucketName
          : client.session;
        bucketName = bucketName
          .normalize('NFD')
          .replace(/[\u0300-\u036f]|[— _.,?!]/g, '')
          .toLowerCase();
        bucketName =
          bucketName.length < 3
            ? bucketName +
              `${Math.floor(Math.random() * (999 - 100 + 1)) + 100}`
            : bucketName;
        const fileName = `${
          config.aws_s3.defaultBucketName ? client.session + '/' : ''
        }${hashName}.${mime.extension(message.mimetype)}`;

        if (
          !config.aws_s3.defaultBucketName &&
          !(await bucketAlreadyExists(bucketName))
        ) {
          await s3Client.send(
            new CreateBucketCommand({
              Bucket: bucketName,
              ObjectOwnership: 'ObjectWriter',
            })
          );
          await s3Client.send(
            new PutPublicAccessBlockCommand({
              Bucket: bucketName,
              PublicAccessBlockConfiguration: {
                BlockPublicAcls: false,
                IgnorePublicAcls: false,
                BlockPublicPolicy: false,
              },
            })
          );
        }

        await s3Client.send(
          new PutObjectCommand({
            Bucket: bucketName,
            Key: fileName,
            Body: buffer,
            ContentType: message.mimetype,
            ACL: 'public-read',
          })
        );

        message.fileUrl = `https://${bucketName}.s3.amazonaws.com/${fileName}`;
      } else {
        message.body = await buffer.toString('base64');
      }
    }
  } catch (e) {
    req.logger.error(e);
  }
}

export async function startAllSessions(config: any, logger: any) {
  try {
    await api.post(
      `${config.host}:${config.port}/api/${config.secretKey}/start-all`
    );
  } catch (e) {
    logger.error(e);
  }
}

export async function startHelper(client: any, req: any) {
  if (req.serverOptions.webhook.allUnreadOnStart) await sendUnread(client, req);

  if (req.serverOptions.archive.enable) await archive(client, req);
}

async function sendUnread(client: any, req: any) {
  req.logger.info(`${client.session} : Inicio enviar mensagens não lidas`);

  try {
    const chats = await client.getAllChatsWithMessages(true);

    if (chats && chats.length > 0) {
      for (let i = 0; i < chats.length; i++)
        for (let j = 0; j < chats[i].msgs.length; j++) {
          callWebHook(client, req, 'unreadmessages', chats[i].msgs[j]);
        }
    }

    req.logger.info(`${client.session} : Fim enviar mensagens não lidas`);
  } catch (ex) {
    req.logger.error(ex);
  }
}

async function archive(client: any, req: any) {
  async function sleep(time: number) {
    return new Promise((resolve) => setTimeout(resolve, time * 10));
  }

  req.logger.info(`${client.session} : Inicio arquivando chats`);

  try {
    let chats = await client.getAllChats();
    if (chats && Array.isArray(chats) && chats.length > 0) {
      chats = chats.filter((c) => !c.archive);
    }
    if (chats && Array.isArray(chats) && chats.length > 0) {
      for (let i = 0; i < chats.length; i++) {
        const date = new Date(chats[i].t * 1000);

        if (DaysBetween(date) > req.serverOptions.archive.daysToArchive) {
          await client.archiveChat(
            chats[i].id.id || chats[i].id._serialized,
            true
          );
          await sleep(
            Math.floor(Math.random() * req.serverOptions.archive.waitTime + 1)
          );
        }
      }
    }
    req.logger.info(`${client.session} : Fim arquivando chats`);
  } catch (ex) {
    req.logger.error(ex);
  }
}

function DaysBetween(StartDate: Date) {
  const endDate = new Date();
  // The number of milliseconds in all UTC days (no DST)
  const oneDay = 1000 * 60 * 60 * 24;

  // A day in UTC always lasts 24 hours (unlike in other time formats)
  const start = Date.UTC(
    endDate.getFullYear(),
    endDate.getMonth(),
    endDate.getDate()
  );
  const end = Date.UTC(
    StartDate.getFullYear(),
    StartDate.getMonth(),
    StartDate.getDate()
  );

  // so it's safe to divide by 24 hours
  return (start - end) / oneDay;
}

export function createFolders() {
  const __dirname = path.resolve(path.dirname(''));
  const dirFiles = path.resolve(__dirname, 'WhatsAppImages');
  if (!fs.existsSync(dirFiles)) {
    fs.mkdirSync(dirFiles);
  }

  const dirUpload = path.resolve(__dirname, 'uploads');
  if (!fs.existsSync(dirUpload)) {
    fs.mkdirSync(dirUpload);
  }
}

export function strToBool(s: string) {
  return /^(true|1)$/i.test(s);
}

export function getIPAddress() {
  const interfaces = os.networkInterfaces();
  for (const devName in interfaces) {
    const iface: any = interfaces[devName];
    for (let i = 0; i < iface.length; i++) {
      const alias = iface[i];
      if (
        alias.family === 'IPv4' &&
        alias.address !== '127.0.0.1' &&
        !alias.internal
      )
        return alias.address;
    }
  }
  return '0.0.0.0';
}

export function setMaxListners(serverOptions: ServerOptions) {
  if (serverOptions && Number.isInteger(serverOptions.maxListeners)) {
    process.setMaxListeners(serverOptions.maxListeners);
  }
}

export const unlinkAsync = promisify(fs.unlink);

export function createCatalogLink(session: any) {
  const [wid] = session.split('@');
  return `https://wa.me/c/${wid}`;
}

/**
 * `client.isConnected()`, given at most `budgetMs` to answer. Resolves to
 * undefined when the budget runs out first.
 *
 * wppconnect 2.3.2 made isConnected() await waitForPageLoad(), which sits on
 * puppeteer's default 30s waiting for WPP.isReady — so on a WhatsApp Web
 * reload this call stops being the cheap probe both of its callers were
 * written around, and each of them has its own deadline it must answer
 * within. Neither can wait 30s for it.
 *
 * The losing probe is left to settle on its own with a rejection handler
 * already attached: an isConnected() that throws after the race was decided
 * would otherwise be an unhandled rejection, and Node exits the process on
 * those. On a page that never becomes ready that probe never settles either,
 * so every timed-out call leaves one page.waitForFunction() pending inside
 * Chromium — accepted deliberately: the callers are request- and tick-driven,
 * so the count is bounded by the request rate, and each one is discarded with
 * the page the moment the session is restarted (which is precisely what a
 * bounded probe reporting Disconnected is there to bring about). Concretely,
 * the health check polls every ~30s and two consecutive strikes restart the
 * session, so a stuck page accumulates ~2-3 of them before it is torn down.
 */
export async function probeIsConnected(
  client: WhatsAppServer,
  budgetMs: number
): Promise<boolean | undefined> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const probe = Promise.resolve(client.isConnected());
  probe.catch(() => undefined);
  try {
    return await Promise.race<boolean | undefined>([
      probe,
      new Promise<undefined>((resolve) => {
        timer = setTimeout(() => resolve(undefined), Math.max(0, budgetMs));
      }),
    ]);
  } finally {
    if (timer) {
      clearTimeout(timer);
    }
  }
}
