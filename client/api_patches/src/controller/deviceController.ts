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
import { Chat } from '@wppconnect-team/wppconnect';
import { Request, Response } from 'express';

import {
  countJidFallback,
  countStoreRecovery,
  observeEvaluate,
} from '../middleware/instrumentation';
import {
  observePayload,
  SyncChatListSchema,
  SyncContactListSchema,
  SyncMessageListSchema,
} from '../dto/sync';
import { contactToArray, unlinkAsync } from '../util/functions';
import { clientsArray } from '../util/sessionUtil';

function returnSucess(res: any, session: any, phone: any, data: any) {
  res.status(201).json({
    status: 'Success',
    response: {
      message: 'Information retrieved successfully.',
      contact: phone,
      session: session,
      data: data,
    },
  });
}

function returnError(req: Request, res: Response, session: any, error: any) {
  req.logger.error(error);
  // JSON.stringify(new Error(...)) serializes to `{}` — Error's own message/stack
  // properties aren't enumerable — so passing the raw Error object here silently
  // dropped the actual failure text (e.g. "Chat not found for X@c.us") from the
  // HTTP response body. Callers (e.g. WinZapp's mark-as-read @lid retry, which
  // string-matches "not found" in the response) never saw it and could never
  // detect this specific failure to retry with the @lid JID instead.
  const message = error instanceof Error ? error.message : String(error);
  res.status(400).json({
    status: 'Error',
    response: {
      message: 'Error retrieving information',
      session: session,
      log: message,
    },
  });
}

export async function setProfileName(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Profile"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.requestBody = {
      required: false,
      "@content": {
        "application/json": {
          schema: {
            type: "object",
            properties: {
              name: { type: "string" },
            }
          },
          examples: {
            "Default": {
              value: {
                name: "My new name",
              }
            },
          }
        }
      }
     }
   */
  const { name } = req.body;

  if (!name)
    res
      .status(400)
      .json({ status: 'error', message: 'Parameter name is required!' });

  try {
    const result = await req.client.setProfileName(name);
    res.status(200).json({ status: 'success', response: result });
  } catch (error) {
    req.logger.error(error);
    res.status(500).json({
      status: 'error',
      message: 'Error on set profile name.',
      error: error,
    });
  }
}

export async function showAllContacts(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Contacts"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    const contacts = await req.client.getAllContacts();
    res.status(200).json({ status: 'success', response: contacts });
  } catch (error) {
    req.logger.error(error);
    res.status(500).json({
      status: 'error',
      message: 'Error fetching contacts',
      error: error,
    });
  }
}

export async function getAllChats(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Chat"]
   * #swagger.summary = 'Deprecated in favor of 'list-chats'
   * #swagger.deprecated = true
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    const response = await req.client.getAllChats();
    res
      .status(200)
      .json({ status: 'success', response: response, mapper: 'chat' });
  } catch (e) {
    req.logger.error(e);
    res
      .status(500)
      .json({ status: 'error', message: 'Error on get all chats' });
  }
}

/**
 * Read the chat list without trusting WhatsApp Web's in-memory ChatStore to
 * already be hydrated.
 *
 * A large linked account can have every chat safely persisted in the
 * `model-storage` IndexedDB while `WPP.chat.list()` returns [] (or even throws
 * while serialising an undefined collection).  Retrying the same call does not
 * repair that split-brain state: the captured 937-chat session returned an
 * empty list 99 times while get-messages continued to read IndexedDB normally.
 *
 * On that exact failure only, load the persisted chat ids and ask wa-js to
 * `find()` them. find() is the supported API that creates/loads a ChatModel and
 * registers it in ChatStore. Work is chunked, bounded, and single-flight inside
 * the page so Python's retry loop cannot start several 900-chat recoveries at
 * once. The healthy path still performs one ordinary WPP.chat.list() call.
 */
async function listChatsWithStoreRecovery(
  req: Request,
  options: Record<string, any>
): Promise<any[]> {
  const result: any = await observeEvaluate('list-chats', () =>
    req.client.page.evaluate(
    async ({ listOptions }) => {
      const root = globalThis as any;

      const serialise = async () => {
        try {
          const models = await root.WPP?.chat?.list?.(listOptions);
          if (!Array.isArray(models)) {
            return { chats: [], error: 'WPP.chat.list returned a non-array value' };
          }
          const serializer = root.WAPI?._serializeChatObj;
          if (typeof serializer !== 'function') {
            return { chats: [], error: 'WAPI._serializeChatObj is unavailable' };
          }
          return { chats: models.map((chat: any) => serializer(chat)) };
        } catch (error: any) {
          return { chats: [], error: error?.message || String(error) };
        }
      };

      const first = await serialise();
      if (first.chats.length > 0) {
        return { ...first, recovered: false };
      }

      // Every concurrent list-chats request awaits the same recovery, and a
      // later, genuinely new store loss can still be repaired once this one
      // finishes. The in-flight promise alone is not enough to guarantee that:
      // it is cleared the moment the recovery settles, which is BEFORE the
      // requests waiting on it have re-read the store, so a request arriving
      // in that window would start a second 900-chat scan over a store that
      // was just rebuilt. The cooldown closes it — the single-flight promise
      // covers requests that arrive during the scan, the timestamp covers the
      // ones that arrive right after it.
      const RECOVERY_COOLDOWN_MS = 30_000;
      const recoveredAt = root.__winzappChatStoreRecoveredAt || 0;
      const coolingDown = Date.now() - recoveredAt < RECOVERY_COOLDOWN_MS;
      if (!root.__winzappChatStoreRecoveryPromise && !coolingDown) {
        root.__winzappChatStoreRecoveryPromise = (async () => {
          const ids = await new Promise<string[]>((resolve) => {
            let open: IDBOpenDBRequest;
            try {
              open = indexedDB.open('model-storage');
            } catch (_error) {
              resolve([]);
              return;
            }
            open.onerror = () => resolve([]);
            open.onsuccess = () => {
              const db = open.result;
              const found = new Set<string>();
              const jidPattern = /(?:^|_)([^_\s]+@(c\.us|s\.whatsapp\.net|lid|g\.us|newsletter|broadcast))$/;

              const add = (candidate: any) => {
                if (candidate && typeof candidate === 'object') {
                  candidate =
                    candidate._serialized || candidate.id || candidate.jid ||
                    candidate.remoteJid || candidate.chatId;
                }
                if (typeof candidate !== 'string') return;
                const match = candidate.match(jidPattern);
                if (match?.[1]) found.add(match[1]);
              };

              try {
                const transaction = db.transaction('chat', 'readonly');
                const store = transaction.objectStore('chat');
                const cursor = store.openCursor();
                cursor.onerror = () => {
                  db.close();
                  resolve(Array.from(found));
                };
                cursor.onsuccess = () => {
                  const current = cursor.result;
                  if (!current || found.size >= 5000) {
                    db.close();
                    resolve(Array.from(found));
                    return;
                  }
                  const value: any = current.value;
                  add(current.primaryKey);
                  add(current.key);
                  add(value);
                  add(value?.id);
                  add(value?.jid);
                  add(value?.remoteJid);
                  add(value?.chatId);
                  add(value?.key);
                  add(value?.value?.id);
                  add(value?.data?.id);
                  current.continue();
                };
              } catch (_error) {
                db.close();
                resolve([]);
              }
            };
          });

          const findChat = root.WPP?.chat?.find;
          if (typeof findChat !== 'function' || ids.length === 0) {
            return { indexedDbChats: ids.length, rehydrated: 0, failed: ids.length };
          }

          let rehydrated = 0;
          let failed = 0;
          const batchSize = 24;
          for (let offset = 0; offset < ids.length; offset += batchSize) {
            const batch = ids.slice(offset, offset + batchSize);
            const settled = await Promise.allSettled(
              batch.map((jid) => findChat(jid))
            );
            for (const item of settled) {
              if (item.status === 'fulfilled' && item.value) rehydrated++;
              else failed++;
            }
          }
          return { indexedDbChats: ids.length, rehydrated, failed };
        })().finally(() => {
          root.__winzappChatStoreRecoveredAt = Date.now();
          delete root.__winzappChatStoreRecoveryPromise;
        });
      }

      // Read the property once: within the cooldown there is no promise to
      // await, and the store is simply re-read — a scan that just finished is
      // not worth repeating for every request in the retry loop behind it.
      const pending = root.__winzappChatStoreRecoveryPromise;
      const recovery = pending
        ? await pending
        : { skipped: 'recovery ran recently' };
      const second = await serialise();
      return {
        ...second,
        recovered: true,
        firstError: first.error,
        recovery,
      };
    },
    { listOptions: options }
    )
  );

  if (result?.recovered) {
    countStoreRecovery(
      (result?.chats?.length || 0) > 0 ? 'recovered' : 'still_empty'
    );
    req.logger.warn(
      `[listChats] ChatStore was empty/invalid; IndexedDB recovery: ${JSON.stringify(
        result.recovery || {}
      )}; recovered list has ${result?.chats?.length || 0} chat(s)`
    );
  }
  if (result?.error && !result?.chats?.length) {
    req.logger.warn(`[listChats] ${result.error}`);
  }
  return Array.isArray(result?.chats) ? result.chats : [];
}

export async function listChats(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Chat"]
   * #swagger.summary = 'Retrieve a list of chats'
   * #swagger.description = 'This body is not required. Not sent body to get all chats or filter.'
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.requestBody = {
      required: false,
      "@content": {
        "application/json": {
          schema: {
            type: "object",
            properties: {
              id: { type: "string" },
              count: { type: "number" },
              direction: { type: "string" },
              onlyGroups: { type: "boolean" },
              onlyUsers: { type: "boolean" },
              onlyWithUnreadMessage: { type: "boolean" },
              withLabels: { type: "array" },
              ignoreGroupMetadata: { type: "boolean" },
            }
          },
          examples: {
            "All options - Edit this": {
              value: {
                id: "<chatId>",
                count: 20,
                direction: "after",
                onlyGroups: false,
                onlyUsers: false,
                onlyWithUnreadMessage: false,
                withLabels: [],
                ignoreGroupMetadata: true
              }
            },
            "All chats": {
              value: {
              }
            },
            "Chats group": {
              value: {
                onlyGroups: true,
              }
            },
            "Only with unread messages": {
              value: {
                onlyWithUnreadMessage: false,
              }
            },
            "Paginated results": {
              value: {
                id: "<chatId>",
                count: 20,
                direction: "after",
              }
            },
          }
        }
      }
     }
   */
  try {
    const {
      id,
      count,
      direction,
      onlyGroups,
      onlyUsers,
      onlyWithUnreadMessage,
      withLabels,
      ignoreGroupMetadata,
    } = req.body;

    const options: any = {};
    if (id !== undefined) options.id = id;
    if (count !== undefined && count > 0) options.count = count;
    if (direction !== undefined) options.direction = direction;
    if (onlyGroups !== undefined) options.onlyGroups = onlyGroups;
    if (onlyUsers !== undefined) options.onlyUsers = onlyUsers;
    if (onlyWithUnreadMessage !== undefined)
      options.onlyWithUnreadMessage = onlyWithUnreadMessage;
    if (withLabels !== undefined) options.withLabels = withLabels;
    // WPP.chat.list() ends with a *serial* `await GroupMetadataStore.find(id)`
    // over every group chat — one network round-trip each — unless
    // ignoreGroupMetadata is set. Right after pairing, while WhatsApp Web is
    // still running its initial sync, that loop routinely outlives Puppeteer's
    // protocolTimeout, so list-chats never answers and the client gives up with
    // "Read timed out". Forward the flag so callers that don't need group
    // metadata (WinZapp fetches it separately) can skip the loop entirely.
    if (ignoreGroupMetadata !== undefined)
      options.ignoreGroupMetadata = ignoreGroupMetadata;

    const chats = await listChatsWithStoreRecovery(req, options);

    // WhatsApp Web creates one-to-one ChatStore entries for group participants
    // when it receives only an encryption-key notification from them. These are
    // not conversations: they have no activity timestamp, were never opened,
    // have no unread count, and do not belong to an address-book contact.
    const visibleChats = chats.filter((chat: any) => {
      const jid = chat?.id?._serialized || chat?.id || '';
      if (!jid || jid.endsWith('@g.us')) return true;
      const hasActivity =
        Boolean(chat?.t) ||
        Boolean(chat?.lastMessage) ||
        Number(chat?.unreadCount || 0) > 0;
      const isKnownConversation =
        chat?.hasChatBeenOpened === true || chat?.contact?.isMyContact === true;
      return hasActivity || isKnownConversation;
    });

    res.status(200).json(
      observePayload(SyncChatListSchema, visibleChats, {
        logger: req.logger,
        endpoint: 'list-chats',
      })
    );
  } catch (e) {
    req.logger.error(e);
    res
      .status(500)
      .json({ status: 'error', message: 'Error on get all chats' });
  }
}

export async function getAllChatsWithMessages(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Chat"]
   * #swagger.summary = 'Deprecated in favor of list-chats'
   * #swagger.deprecated = true
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    const response = await req.client.listChats();
    res.status(200).json({ status: 'success', response: response });
  } catch (e) {
    req.logger.error(e);
    res.status(500).json({
      status: 'error',
      message: 'Error on get all chats whit messages',
      error: e,
    });
  }
}
/**
 * Depreciado em favor de getMessages
 */
export async function getAllMessagesInChat(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Chat"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.parameters["phone"] = {
      schema: '5521999999999'
     }
     #swagger.parameters["isGroup"] = {
      schema: 'false'
     }
     #swagger.parameters["includeMe"] = {
      schema: 'true'
     }
     #swagger.parameters["includeNotifications"] = {
      schema: 'true'
     }
   */
  try {
    const { phone } = req.params;
    const {
      isGroup = false,
      includeMe = true,
      includeNotifications = true,
    } = req.query;

    let response;
    for (const contato of contactToArray(phone, isGroup as boolean)) {
      response = await req.client.getAllMessagesInChat(
        contato,
        includeMe as boolean,
        includeNotifications as boolean
      );
    }

    res.status(200).json({ status: 'success', response: response });
  } catch (e) {
    req.logger.error(e);
    res.status(500).json({
      status: 'error',
      message: 'Error on get all messages in chat',
      error: e,
    });
  }
}

export async function getAllNewMessages(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Chat"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    const response = await req.client.getAllNewMessages();
    res.status(200).json({ status: 'success', response: response });
  } catch (e) {
    req.logger.error(e);
    res.status(500).json({
      status: 'error',
      message: 'Error on get all messages in chat',
      error: e,
    });
  }
}

export async function getAllUnreadMessages(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Chat"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    const response = await req.client.getAllUnreadMessages();
    res.status(200).json({ status: 'success', response: response });
  } catch (e) {
    req.logger.error(e);
    res.status(500).json({
      status: 'error',
      message: 'Error on get all messages in chat',
      error: e,
    });
  }
}

export async function getChatById(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Chat"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.parameters["phone"] = {
      schema: '5521999999999'
     }
     #swagger.parameters["isGroup"] = {
      schema: 'false'
     }
   */
  const { phone } = req.params;
  const { isGroup } = req.query;

  try {
    let result = {} as Chat;
    if (isGroup) {
      result = await req.client.getChatById(`${phone}@g.us`);
    } else {
      result = await req.client.getChatById(`${phone}@c.us`);
    }

    res.status(200).json(result);
  } catch (e) {
    req.logger.error(e);
    res.status(500).json({
      status: 'error',
      message: 'Error changing chat by Id',
      error: e,
    });
  }
}

export async function getMessageById(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Chat"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.parameters["messageId"] = {
      required: true,
      schema: '<message_id>'
     }
   */
  const session = req.session;
  const { messageId } = req.params;

  try {
    const result = await req.client.getMessageById(messageId);

    returnSucess(res, session, (result as any).chatId.user, result);
  } catch (error) {
    returnError(req, res, session, error);
  }
}

export async function getBatteryLevel(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Misc"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    const response = await req.client.getBatteryLevel();
    res.status(200).json({ status: 'Success', response: response });
  } catch (e) {
    req.logger.error(e);
    res.status(500).json({
      status: 'error',
      message: 'Error retrieving battery status',
      error: e,
    });
  }
}

export async function getHostDevice(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Misc"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    const response = await req.client.getHostDevice();
    const phoneNumber = await req.client.getWid();
    res.status(200).json({
      status: 'success',
      response: { ...response, phoneNumber },
      mapper: 'device',
    });
  } catch (e) {
    req.logger.error(e);
    res.status(500).json({
      status: 'error',
      message: 'Erro ao recuperar dados do telefone',
      error: e,
    });
  }
}

export async function getPhoneNumber(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Misc"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    const phoneNumber = await req.client.getWid();
    res
      .status(200)
      .json({ status: 'success', response: phoneNumber, mapper: 'device' });
  } catch (e) {
    req.logger.error(e);
    res.status(500).json({
      status: 'error',
      message: 'Error retrieving phone number',
      error: e,
    });
  }
}

export async function getBlockList(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Misc"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  const response = await req.client.getBlockList();

  try {
    const blocked = response.map((contato: any) => {
      return { phone: contato ? contato.split('@')[0] : '' };
    });

    res.status(200).json({ status: 'success', response: blocked });
  } catch (e) {
    req.logger.error(e);
    res.status(500).json({
      status: 'error',
      message: 'Error retrieving blocked contact list',
      error: e,
    });
  }
}

export async function deleteChat(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Chat"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.requestBody = {
      required: false,
      "@content": {
        "application/json": {
          schema: {
            type: "object",
            properties: {
              phone: { type: "string" },
              isGroup: { type: "boolean" },
            }
          },
          examples: {
            "Default": {
              value: {
                phone: "5521999999999",
                isGroup: false,
              }
            },
          }
        }
      }
     }
   */
  const { phone } = req.body;
  const session = req.session;

  try {
    const results: any = {};
    for (const contato of phone) {
      results[contato] = await req.client.deleteChat(contato);
    }
    returnSucess(res, session, phone, results);
  } catch (error) {
    returnError(req, res, session, error);
  }
}
export async function deleteAllChats(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Chat"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    const chats = await req.client.getAllChats();
    for (const chat of chats) {
      await req.client.deleteChat((chat as any).chatId);
    }
    res.status(200).json({ status: 'success' });
  } catch (error) {
    req.logger.error(error);
    res.status(500).json({
      status: 'error',
      message: 'Error on delete all chats',
      error: error,
    });
  }
}

export async function clearChat(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Chat"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     
     #swagger.requestBody = {
      required: false,
      "@content": {
        "application/json": {
          schema: {
            type: "object",
            properties: {
              phone: { type: "string" },
              isGroup: { type: "boolean" },
            }
          },
          examples: {
            "Default": {
              value: {
                phone: "5521999999999",
                isGroup: false,
              }
            },
          }
        }
      }
     }
   */
  const { phone } = req.body;
  const session = req.session;

  try {
    const results: any = {};
    for (const contato of phone) {
      results[contato] = await req.client.clearChat(contato);
    }
    returnSucess(res, session, phone, results);
  } catch (error) {
    returnError(req, res, session, error);
  }
}

export async function clearAllChats(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Chat"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    const chats = await req.client.getAllChats();
    for (const chat of chats) {
      await req.client.clearChat(`${(chat as any).chatId}`);
    }
    res.status(201).json({ status: 'success' });
  } catch (e) {
    req.logger.error(e);
    res
      .status(500)
      .json({ status: 'error', message: 'Error on clear all chats', error: e });
  }
}

export async function archiveChat(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Chat"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     
     #swagger.requestBody = {
      required: false,
      "@content": {
        "application/json": {
          schema: {
            type: "object",
            properties: {
              phone: { type: "string" },
              isGroup: { type: "boolean" },
              value: { type: "boolean" },
            }
          },
          examples: {
            "Default": {
              value: {
                phone: "5521999999999",
                isGroup: false,
                value: true,
              }
            },
          }
        }
      }
     }
   */
  const { phone, value = true } = req.body;

  try {
    const response = await req.client.archiveChat(`${phone}`, value);
    res.status(201).json({ status: 'success', response: response });
  } catch (e) {
    req.logger.error(e);
    res
      .status(500)
      .json({ status: 'error', message: 'Error on archive chat', error: e });
  }
}

export async function archiveAllChats(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Chat"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    const chats = await req.client.getAllChats();
    for (const chat of chats) {
      await req.client.archiveChat(`${(chat as any).chatId}`, true);
    }
    res.status(201).json({ status: 'success' });
  } catch (e) {
    req.logger.error(e);
    res.status(500).json({
      status: 'error',
      message: 'Error on archive all chats',
      error: e,
    });
  }
}

export async function getAllChatsArchiveds(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Chat"]
   * #swagger.description = 'Retrieves all archived chats.'
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    const chats = await req.client.getAllChats();
    const archived = [] as any;
    for (const chat of chats) {
      if (chat.archive === true) {
        archived.push(chat);
      }
    }
    res.status(201).json(archived);
  } catch (e) {
    req.logger.error(e);
    res.status(500).json({
      status: 'error',
      message: 'Error on archive all chats',
      error: e,
    });
  }
}
export async function deleteMessage(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Messages"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     
     #swagger.requestBody = {
      required: false,
      "@content": {
        "application/json": {
          schema: {
            type: "object",
            properties: {
              phone: { type: "string" },
              isGroup: { type: "boolean" },
              messageId: { type: "string" },
              onlyLocal: { type: "boolean" },
              deleteMediaInDevice: { type: "boolean" },
            }
          },
          examples: {
            "Delete message to all": {
              value: {
                phone: "5521999999999",
                isGroup: false,
                messageId: "<messageId>",
                deleteMediaInDevice: true,
              }
            },
            "Delete message only me": {
              value: {
                phone: "5521999999999",
                isGroup: false,
                messageId: "<messageId>",
              }
            },
          }
        }
      }
     }
   */
  const { phone, messageId, deleteMediaInDevice, onlyLocal } = req.body;

  try {
    const result = await req.client.deleteMessage(
      `${phone}`,
      messageId,
      onlyLocal,
      deleteMediaInDevice
    );
    if (result) {
      return res
        .status(200)
        .json({ status: 'success', response: { message: 'Message deleted' } });
    }
    return res.status(401).json({
      status: 'error',
      response: { message: 'Error unknown on delete message' },
    });
  } catch (e) {
    req.logger.error(e);
    res
      .status(500)
      .json({ status: 'error', message: 'Error on delete message', error: e });
  }
}
export async function reactMessage(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Messages"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.requestBody = {
      required: false,
      "@content": {
        "application/json": {
          schema: {
            type: "object",
            properties: {
              msgId: { type: "string" },
              reaction: { type: "string" },
            }
          },
          examples: {
            "Default": {
              value: {
                msgId: "<messageId>",
                reaction: "😜",
              }
            },
          }
        }
      }
     }
   */
  const { msgId, reaction: rawReaction } = req.body;
  // wa-js's sendReactionToMessage() documents `false` (not an empty
  // string) as the "remove the existing reaction" signal — WinZapp's own
  // status-unlike flow (status_panel.py) sends an empty string for that,
  // which used to be passed straight through as `reaction || ''` below
  // instead of being normalized to the value the library actually expects.
  const reaction: string | false =
    rawReaction === '' || rawReaction === undefined || rawReaction === null
      ? false
      : rawReaction;

  try {
    if (typeof msgId === 'string' && msgId.includes('status@broadcast')) {
      req.logger.info(
        `[status-reaction] begin msgId=${msgId} action=${
          reaction === false ? 'remove' : 'set'
        }`
      );
      // wa-js's WPP.chat.sendReactionToMessage(msgId, reaction) resolves a
      // string id via getMessageById(), which — for any @broadcast id —
      // unconditionally looks the message up in
      // StatusV3Store.getMyStatus().msgs, i.e. only YOUR OWN posted
      // statuses. Liking another contact's status therefore always failed
      // to even locate the message, before a reaction was ever attempted.
      //
      // Confirmed live (via the error-detail fix above, which used to hide
      // this): the general Store.Msg.models collection this used to search
      // does NOT contain other people's status messages at all — WhatsApp
      // Web keeps those in a separate per-poster StatusV3Model, reachable
      // via WPP.status.get(posterJid).getAllMsgs(). The serialized id's
      // trailing segment (see _serialize_msg_id() in main.py, which always
      // appends the poster's JID as "participant" for @broadcast ids) is
      // exactly that poster JID. Store.Msg.models is kept as a fallback in
      // case a status is ever ALSO mirrored there on some WhatsApp Web
      // version.
      const outcome = await req.client.page.evaluate(
        async ({ msgId, reaction }) => {
          const parts = msgId.split('_');
          const rawId = parts.length > 2 ? parts[2] : msgId;
          const posterJid = parts.length > 3 ? parts[3] : null;
          const WPP = (window as any).WPP;
          let model: any = null;

          const asModels = (value: any): any[] => {
            if (!value) return [];
            if (Array.isArray(value)) return value;
            if (typeof value.getModelsArray === 'function') {
              return value.getModelsArray() || [];
            }
            if (typeof value.values === 'function') {
              try {
                return Array.from(value.values());
              } catch (_) {
                return [];
              }
            }
            return [];
          };

          const matchesStatusId = (item: any): boolean => {
            if (!item?.id) return false;
            const serialized = String(item.id._serialized ?? item.id.$1 ?? '');
            const itemId = String(item.id.id ?? item.id.$1 ?? '');
            return (
              itemId === rawId ||
              serialized === msgId ||
              Boolean(rawId && serialized.includes(rawId))
            );
          };

          // Use the same lookup strategy as the real status-reply route:
          // prefer the serialized poster, but scan StatusV3Store as well.
          // Status collections may be keyed by @c.us while Python knows the
          // same contact as @lid (or vice versa), so WPP.status.get(posterJid)
          // alone can miss a status that is visibly open in WinZapp.
          const statusModels: any[] = [];
          const expected = posterJid ? WPP?.status?.get?.(posterJid) : null;
          if (expected) statusModels.push(expected);
          const statusStore = WPP?.whatsapp?.StatusV3Store;
          if (typeof statusStore?.sync === 'function') {
            try {
              await statusStore.sync();
            } catch (_) {
              // The already-loaded collection is still useful when sync fails.
            }
          }
          const storedModels = asModels(
            (typeof statusStore?.getModels === 'function' &&
              statusStore.getModels()) ||
              statusStore?._models ||
              statusStore?.models ||
              (typeof statusStore?.getUnexpired === 'function' &&
                statusStore.getUnexpired())
          );
          for (const statusChat of storedModels) {
            if (statusChat && !statusModels.includes(statusChat)) {
              statusModels.push(statusChat);
            }
          }

          for (const statusChat of statusModels) {
            if (!model) {
              const msgs = asModels(
                (typeof statusChat.getAllMsgs === 'function' &&
                  statusChat.getAllMsgs()) ||
                  statusChat.msgs?._models ||
                  statusChat.msgs?.models ||
                  statusChat.msgs
              );
              model = msgs.find(matchesStatusId);
            }
          }

          if (
            !model &&
            (window as any).Store &&
            (window as any).Store.Msg &&
            (window as any).Store.Msg.models
          ) {
            const models = (window as any).Store.Msg.models;
            model = asModels(models).find(matchesStatusId);
          }
          if (!model) {
            return {
              ok: false,
              detail: `message-not-found; models=${statusModels.length}`,
            };
          }
          // Status likes have their own WhatsApp Web action. It converts the
          // author's LID/phone-number identity, builds the addon payload and
          // fans it out directly to both the author's devices and our linked
          // devices. The generic status-post path uses the account's status
          // privacy audience instead, which can acknowledge the local copy
          // while never delivering the like to this status author.
          const modelAuthor =
            model.author || model.id?.participant || posterJid || null;
          const authorText = String(
            modelAuthor?._serialized ?? modelAuthor?.$1 ?? modelAuthor ?? ''
          );
          if (!authorText) {
            return { ok: false, detail: 'status-author-not-found' };
          }

          const pageWindow = window as any;
          let statusReactionAction: any = null;
          try {
            statusReactionAction = pageWindow.require?.(
              'WAWebSendStatusReactionAction'
            );
          } catch (error) {
            return {
              ok: false,
              detail: `native-status-reaction-module-error: ${String(
                (error as any)?.message || error
              )}`,
            };
          }
          if (typeof statusReactionAction?.sendStatusReaction !== 'function') {
            return {
              ok: false,
              detail: 'native-status-reaction-action-not-found',
            };
          }
          await statusReactionAction.sendStatusReaction(model, reaction || '');
          return {
            ok: true,
            detail: `native-status-reaction-completed; author=${authorText}`,
          };
        },
        { msgId, reaction }
      );
      if (!outcome?.ok) {
        throw new Error(
          `Status reaction failed for ${msgId}: ${
            outcome?.detail || 'unknown browser result'
          }`
        );
      }
      req.logger.info(
        `[status-reaction] accepted msgId=${msgId} detail=${
          outcome.detail || 'none'
        }`
      );
    } else {
      await req.client.sendReactionToMessage(msgId, reaction);
    }

    res
      .status(200)
      .json({ status: 'success', response: { message: 'Reaction sended' } });
  } catch (e: any) {
    // A bare `error: e` here used to serialize to almost nothing useful:
    // Error.message/.stack aren't enumerable, so JSON.stringify(e) drops
    // them — and req.logger.error(e) (winston) can even mutate the object
    // in place with only a `level` field surviving, which is exactly what
    // showed up in WinZapp's log.log for every failed status reaction
    // ({"level":"error"}, no actual cause). Pull message/stack out
    // explicitly so the next failure is actually diagnosable.
    req.logger.error(
      `[status-reaction] failed msgId=${String(msgId)} error=${
        e && e.message ? e.message : String(e)
      }`
    );
    res.status(500).json({
      status: 'error',
      message: 'Error on send reaction to message',
      error: e && e.message ? e.message : String(e),
      stack: e && e.stack ? String(e.stack) : undefined,
    });
  }
}

export async function reply(req: Request, res: Response) {
  /**
   * #swagger.deprecated=true
     #swagger.tags = ["Messages"]
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
              messageid: { type: "string" },
              text: { type: "string" },
            }
          },
          examples: {
            "Default": {
              value: {
              phone: "5521999999999",
              isGroup: false,
              messageid: "<messageId>",
              text: "Text to reply",
              }
            },
          }
        }
      }
     }
   */
  const { phone, text, messageid } = req.body;

  try {
    const response = await req.client.reply(`${phone}@c.us`, text, messageid);
    res.status(200).json({ status: 'success', response: response });
  } catch (e) {
    req.logger.error(e);
    res
      .status(500)
      .json({ status: 'error', message: 'Error replying message', error: e });
  }
}

export async function forwardMessages(req: Request, res: Response) {
  /**
     #swagger.tags = ["Messages"]
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
              messageId: { type: "string" },
            }
          },
          examples: {
            "Default": {
              value: {
                phone: "5521999999999",
                isGroup: false,
                messageId: "<messageId>",
              }
            },
          }
        }
      }
     }
   */
  const { phone, messageId, isGroup = false } = req.body;

  try {
    let response;

    if (!isGroup) {
      response = await req.client.forwardMessagesV2(`${phone[0]}`, messageId);
    } else {
      response = await req.client.forwardMessagesV2(`${phone[0]}`, messageId);
    }

    res.status(201).json({ status: 'success', response: response });
  } catch (e) {
    req.logger.error(e);
    res
      .status(500)
      .json({ status: 'error', message: 'Error forwarding message', error: e });
  }
}

export async function markUnseenMessage(req: Request, res: Response) {
  /**
     #swagger.tags = ["Messages"]
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
            }
          },
          examples: {
            "Default": {
              value: {
                phone: "5521999999999",
                isGroup: false,
              }
            },
          }
        }
      }
     }
   */
  const { phone } = req.body;

  try {
    await req.client.markUnseenMessage(`${phone}`);
    res
      .status(200)
      .json({ status: 'success', response: { message: 'unseen checked' } });
  } catch (e) {
    req.logger.error(e);
    res
      .status(500)
      .json({ status: 'error', message: 'Error on mark unseen', error: e });
  }
}

export async function blockContact(req: Request, res: Response) {
  /**
     #swagger.tags = ["Misc"]
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
            }
          },
          examples: {
            "Default": {
              value: {
              phone: "5521999999999",
              isGroup: false,
              }
            },
          }
        }
      }
     }
   */
  const { phone } = req.body;

  try {
    await req.client.blockContact(`${phone}`);
    res
      .status(200)
      .json({ status: 'success', response: { message: 'Contact blocked' } });
  } catch (e) {
    req.logger.error(e);
    res
      .status(500)
      .json({ status: 'error', message: 'Error on block contact', error: e });
  }
}

export async function unblockContact(req: Request, res: Response) {
  /**
     #swagger.tags = ["Misc"]
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
            }
          },
          examples: {
            "Default": {
              value: {
              phone: "5521999999999",
              isGroup: false,
              }
            },
          }
        }
      }
     }
   */
  const { phone } = req.body;

  try {
    await req.client.unblockContact(`${phone}`);
    res
      .status(200)
      .json({ status: 'success', response: { message: 'Contact UnBlocked' } });
  } catch (e) {
    req.logger.error(e);
    res
      .status(500)
      .json({ status: 'error', message: 'Error on unlock contact', error: e });
  }
}

export async function pinChat(req: Request, res: Response) {
  /**
     #swagger.tags = ["Chat"]
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
        $phone: '5521999999999',
        $isGroup: false,
        $state: true,
      }
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
              state: { type: "boolean" },
            }
          },
          examples: {
            "Default": {
              value: {
              phone: "5521999999999",
              state: true,
              }
            },
          }
        }
      }
     }
   */
  const { phone, state } = req.body;

  try {
    for (const contato of phone) {
      await req.client.pinChat(contato, state === 'true', false);
    }

    res
      .status(200)
      .json({ status: 'success', response: { message: 'Chat fixed' } });
  } catch (e: any) {
    req.logger.error(e);
    res.status(500).json({
      status: 'error',
      message: e.text || 'Error on pin chat',
      error: e,
    });
  }
}

export async function setProfilePic(req: Request, res: Response) {
  /**
     #swagger.tags = ["Profile"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.consumes = ['multipart/form-data']  
      #swagger.parameters['file'] = {
          in: 'formData',
          type: 'file',
          required: 'true',
      }
   */
  if (!req.file)
    res
      .status(400)
      .json({ status: 'Error', message: 'File parameter is required!' });

  try {
    const { path: pathFile } = req.file as any;

    await req.client.setProfilePic(pathFile);
    await unlinkAsync(pathFile);

    res.status(200).json({
      status: 'success',
      response: { message: 'Profile photo successfully changed' },
    });
  } catch (e) {
    req.logger.error(e);
    res.status(500).json({
      status: 'error',
      message: 'Error changing profile photo',
      error: e,
    });
  }
}

export async function getUnreadMessages(req: Request, res: Response) {
  /**
     #swagger.deprecated=true
     #swagger.tags = ["Messages"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    const response = await req.client.getUnreadMessages(false, false, true);
    res.status(200).json({ status: 'success', response: response });
  } catch (e) {
    req.logger.error(e);
    res
      .status(500)
      .json({ status: 'error', response: 'Error on open list', error: e });
  }
}

export async function getChatIsOnline(req: Request, res: Response) {
  /**
     #swagger.tags = ["Chat"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.parameters["phone"] = {
      schema: '5521999999999',
     }
   */
  const { phone } = req.params;
  try {
    const response = await req.client.getChatIsOnline(`${phone}@c.us`);
    res.status(200).json({ status: 'success', response: response });
  } catch (e) {
    req.logger.error(e);
    res.status(500).json({
      status: 'error',
      response: 'Error on get chat is online',
      error: e,
    });
  }
}

export async function getLastSeen(req: Request, res: Response) {
  /**
     #swagger.tags = ["Chat"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.parameters["phone"] = {
      schema: '5521999999999',
     }
   */
  const { phone } = req.params;
  try {
    const response = await req.client.getLastSeen(`${phone}@c.us`);

    res.status(200).json({ status: 'success', response: response });
  } catch (error) {
    req.logger.error(error);
    res.status(500).json({
      status: 'error',
      response: 'Error on get chat last seen',
      error: error,
    });
  }
}

export async function getListMutes(req: Request, res: Response) {
  /**
     #swagger.tags = ["Chat"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.parameters["type"] = {
      schema: 'all',
     }
   */
  const { type = 'all' } = req.params;
  try {
    const response = await req.client.getListMutes(type);

    res.status(200).json({ status: 'success', response: response });
  } catch (error) {
    req.logger.error(error);
    res.status(500).json({
      status: 'error',
      response: 'Error on get list mutes',
      error: error,
    });
  }
}

export async function loadAndGetAllMessagesInChat(req: Request, res: Response) {
  /**
     #swagger.deprecated=true
     #swagger.tags = ["Chat"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.parameters["phone"] = {
      schema: '5521999999999'
     }
     #swagger.parameters["includeMe"] = {
      schema: 'true'
     }
     #swagger.parameters["includeNotifications"] = {
      schema: 'false'
     }
   */
  const { phone, includeMe = true, includeNotifications = false } = req.params;
  try {
    const response = await req.client.loadAndGetAllMessagesInChat(
      `${phone}@c.us`,
      includeMe as boolean,
      includeNotifications as boolean
    );

    res.status(200).json({ status: 'success', response: response });
  } catch (error) {
    req.logger.error(error);
    res
      .status(500)
      .json({ status: 'error', response: 'Error on open list', error: error });
  }
}
export async function getMessages(req: Request, res: Response) {
  /**
     #swagger.tags = ["Messages"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.parameters["phone"] = {
      schema: '5521999999999@c.us'
     }
     #swagger.parameters["count"] = {
      schema: '20'
     }
     #swagger.parameters["direction"] = {
      schema: 'before'
     }
     #swagger.parameters["id"] = {
      schema: '<message_id_to_use_direction>'
     }
   */
  const { phone } = req.params;
  const { count = 20, direction = 'before', id = null } = req.query;
  try {
    let response: any;
    const targetCount = parseInt(count as string);
    if (direction === 'before' && id) {
      req.logger.info(
        `Fetching older messages before ${id} for ${phone} using browser-side sync...`
      );
      response = await req.client.page.evaluate(
        async ({ chatId, targetCount, id }) => {
          console.log(
            `[browser-evaluate] Starting getMessages for ${chatId}, targetCount=${targetCount}, anchorId=${id}`
          );
          const getMsgSafe = async (msgId: string) => {
            try {
              if (!msgId) return null;
              let m = (window as any).WPP.chat.getMessageById
                ? await (window as any).WPP.chat.getMessageById(msgId)
                : null;
              if (m) return m;

              // Fallback 1: replace @c.us with @s.whatsapp.net or vice versa
              if (msgId.includes('@c.us')) {
                m = await (window as any).WPP.chat.getMessageById(
                  msgId.replace(/@c\.us/g, '@s.whatsapp.net')
                );
                if (m) return m;
              }

              // Fallback 2: strip participant suffix for group messages (first 3 parts: prefix_chatId_id)
              const parts = msgId.split('_');
              if (parts.length > 3) {
                const strippedId = parts.slice(0, 3).join('_');
                m = await (window as any).WPP.chat.getMessageById(strippedId);
                if (m) return m;
              }

              // Fallback 3: search Store.Msg.models by raw message ID or prefix match (group messages with participant suffix)
              const rawId = parts.length > 2 ? parts[2] : msgId;
              if (
                (window as any).Store &&
                (window as any).Store.Msg &&
                (window as any).Store.Msg.models
              ) {
                const models = (window as any).Store.Msg.models;
                const found = models.find((item: any) => {
                  if (!item || !item.id) return false;
                  const ser = item.id._serialized || '';
                  const itemId = item.id.id || '';
                  return (
                    itemId === rawId ||
                    ser === msgId ||
                    (rawId && ser.includes(rawId))
                  );
                });
                if (found) return found;
              }
              return null;
            } catch (e) {
              console.log(
                `[browser-evaluate] getMsgSafe error for ${msgId}: ${e}`
              );
              return null;
            }
          };

          // Ensure the chat is loaded. (There is deliberately no
          // "loadEarlierMessages" call here any more: WPP.chat has no such
          // method — the guard `if (WPP.chat.loadEarlierMessages)` was simply
          // always false, so this block only ever did the find(). Pulling more
          // history is not something the page can do on its own; it needs an
          // on-demand request to the phone — see requestOlderMessages below.)
          try {
            if ((window as any).WPP.chat && (window as any).WPP.chat.find) {
              await (window as any).WPP.chat.find(chatId);
            }
          } catch (e) {
            // Ignore
          }

          // 1. Check if the target anchor message exists in the browser Store
          let anchorExists = false;
          if (id) {
            const msg = await getMsgSafe(id);
            if (msg) {
              anchorExists = true;
            }
          }

          // 2. If the anchor doesn't exist, load history from the server page-by-page
          let attempts = 0;
          const maxAttempts = 2;
          let oldestId = null;
          let originalOldestId = null;

          // Get initial oldest message currently loaded
          if (id && !anchorExists) {
            console.log(
              `[browser-evaluate] Anchor not found in store. Fetching current messages to find oldest...`
            );
            const currentMsgs = await (window as any).WPP.chat.getMessages(
              chatId,
              { count: 100 }
            );
            console.log(
              `[browser-evaluate] Current messages in store count: ${
                currentMsgs ? currentMsgs.length : 0
              }`
            );
            if (currentMsgs && currentMsgs.length > 0) {
              let oldestMsg = currentMsgs[0];
              for (const m of currentMsgs) {
                if (m.t < oldestMsg.t) {
                  oldestMsg = m;
                }
              }
              oldestId = oldestMsg.id._serialized || oldestMsg.id;
              originalOldestId = oldestId;
              console.log(
                `[browser-evaluate] Oldest loaded message JID/ID: ${oldestId}`
              );
            }
          }

          while (id && !anchorExists && oldestId && attempts < maxAttempts) {
            console.log(
              `[browser-evaluate] Walkback attempt ${
                attempts + 1
              }/${maxAttempts} from oldestId=${oldestId}...`
            );
            const loaded = await (window as any).WPP.chat.getMessages(chatId, {
              count: 100,
              direction: 'before',
              id: oldestId,
            });

            console.log(
              `[browser-evaluate] Walkback returned ${
                loaded ? loaded.length : 0
              } messages`
            );
            if (!loaded || loaded.length === 0) {
              break;
            }

            // Find the new oldest message from the loaded batch
            let oldestMsg = loaded[0];
            for (const m of loaded) {
              if (m.t < oldestMsg.t) {
                oldestMsg = m;
              }
            }
            oldestId = oldestMsg.id._serialized || oldestMsg.id;

            const checkMsg = await getMsgSafe(id);
            if (checkMsg) {
              anchorExists = true;
              console.log(`[browser-evaluate] Anchor found during walkback!`);
              break;
            }

            attempts++;
          }

          // 3. Now query the final response
          let queryId = id;
          if (id && !anchorExists) {
            if (originalOldestId) {
              queryId = originalOldestId;
              console.log(
                `[browser-evaluate] Anchor not found after walkback. Falling back to originalOldestId: ${queryId}`
              );
            } else {
              console.log(
                `[browser-evaluate] Anchor not found, and no originalOldestId resolved. Fetching default messages...`
              );
              const currentMsgs = await (window as any).WPP.chat.getMessages(
                chatId,
                { count: 100 }
              );
              if (currentMsgs && currentMsgs.length > 0) {
                let oldestMsg = currentMsgs[0];
                for (const m of currentMsgs) {
                  if (m.t < oldestMsg.t) {
                    oldestMsg = m;
                  }
                }
                queryId = oldestMsg.id._serialized || oldestMsg.id;
              }
            }
          }

          console.log(
            `[browser-evaluate] Final query using WAPI.getMessages with anchor: ${queryId}`
          );
          const result = await (window as any).WAPI.getMessages(chatId, {
            count: targetCount,
            direction: 'before',
            id: queryId,
          });
          console.log(
            `[browser-evaluate] WAPI.getMessages returned ${
              result ? result.length : 0
            } messages`
          );
          return result;
        },
        { chatId: phone, targetCount, id: id as string }
      );
    } else {
      // WinZapp patch: no anchor id — this is the plain "give me up to
      // `count` messages" call sync_chat_messages() makes for every chat on
      // every sync.
      //
      // A previous version of this branch looped on WAPI.loadEarlierMessages()
      // to pull more history into the Store. That loop never ran even once:
      // WAPI.loadEarlierMessages() calls chat.loadEarlierMsgs(), a method
      // current WhatsApp Web builds no longer have, so the very first call
      // threw `TypeError: t.loadEarlierMsgs is not a function` and the
      // `catch { break; }` swallowed it — the whole thing was dead code that
      // looked like a fix. (Measured directly against the live page: four
      // iterations, four identical TypeErrors, store length unchanged at 1.)
      //
      // What actually works is anchored paging. WAPI.getMessages() with an
      // explicit `id` resolves through msgFindBefore(), which is a query
      // against WhatsApp Web's *IndexedDB*, not against the in-memory
      // chat.msgs collection — so walking the anchor backwards can surface
      // messages the collection has not materialised. Verified live: a chat
      // whose in-memory collection held 15 messages answered a `before
      // <newest>` query with the other 14, and `msgFindBefore` reports
      // status 200 with an empty array when the DB genuinely ends there
      // (i.e. "no more history" is distinguishable from a failure).
      //
      // Note what this can and cannot do. It exhausts everything WhatsApp Web
      // has locally. It cannot conjure history WhatsApp Web never ingested —
      // that requires an on-demand request to the phone
      // (requestOlderMessages below) plus a working history-sync pipeline.
      //
      // Deliberately WAPI throughout, matching what this branch called before
      // (client.getMessages() → WAPI.getMessages() — see wppconnect's own
      // whatsapp.js). An earlier patch swapped in WPP.chat.getMessages() and
      // it came back EMPTY for at least one real group chat. An empty (not
      // failed, not null — a valid but empty array) response is
      // indistinguishable from "every message was deleted on the phone" to
      // WinZapp's own _fetch_remote_message_ids() /
      // _reconcile_active_conversation_with_remote(), and was reported live
      // as a group's entire history vanishing from the open conversation
      // mid-read, "recovering" only once a new live message forced a repaint.
      response = await req.client.page.evaluate(
        async ({ chatId, targetCount }) => {
          const keyOf = (m: any) =>
            (m && m.id && (m.id._serialized || m.id)) || null;
          const stampOf = (m: any) => Number(m?.t ?? m?.timestamp ?? 0) || 0;

          const fetchBatch = async (anchor: string | null) =>
            (window as any).WAPI.getMessages(chatId, {
              count: targetCount,
              direction: 'before',
              id: anchor,
            });

          // First page: no anchor, so wa-js anchors on the chat's last received
          // message and hands back the newest window it can.
          let result = await fetchBatch(null);
          if (!Array.isArray(result)) return result;

          const seen = new Set<string>();
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

          result.sort((a: any, b: any) => stampOf(a) - stampOf(b));
          if (result.length > targetCount) {
            // Keep the newest `targetCount` — the caller asked for a window
            // ending at "now", and the final page can overshoot.
            result = result.slice(result.length - targetCount);
          }
          console.log(
            `[browser-evaluate] getMessages ${chatId}: ${result.length} msg(s) after ${pages} extra page(s)`
          );
          return result;
        },
        { chatId: phone, targetCount }
      );
    }
    res.status(200).json({
      status: 'success',
      response: observePayload(SyncMessageListSchema, response, {
        logger: req.logger,
        endpoint: 'get-messages',
      }),
    });
  } catch (e: any) {
    // Every internal failure used to answer 401 "Error on open list", so a
    // chat that does not exist, a page whose WhatsApp layer is not injected
    // yet, and a genuine crash were indistinguishable from the caller's side
    // — and 401 claims "unauthenticated", which none of them are. The Python
    // client retries 401, 404 and 500 alike, so naming them costs nothing in
    // behaviour and makes both the log and the response say what happened.
    const detail = String(e?.message || e || '');
    const notFound = /not found|no such chat|chat not exist/i.test(detail);
    // The page not being ready is the one case the original message was
    // actually about.
    const notReady =
      /WAPI is not defined|not connected|Session (closed|not active)|Execution context/i.test(
        detail
      );
    const status = notFound ? 404 : notReady ? 401 : 500;
    const reason = notFound
      ? 'chat_not_found'
      : notReady
        ? 'session_not_ready'
        : 'internal_error';
    req.logger.error(
      `Error in getMessages (${reason}, HTTP ${status}): ${detail}\nStack: ${
        e?.stack || ''
      }`
    );
    res.status(status).json({
      status: 'error',
      reason,
      response: 'Error on open list',
      error: {
        message: detail,
        stack: e?.stack || '',
      },
    });
  }
}

/**
 * Ask the phone for messages older than the ones this device holds.
 *
 * WhatsApp's multi-device design keeps older history on the primary phone and
 * only pushes a bounded window to a linked device. WhatsApp Web's own UI
 * exposes this as the "Click here to get older messages from your phone"
 * banner at the top of a conversation; underneath, that button sends a peer
 * data operation request of type HISTORY_SYNC_ON_DEMAND. There is no
 * WPPConnect or wa-js wrapper for it, so this reaches into WhatsApp Web's
 * module registry directly (window.require, the Haste loader the page already
 * exposes) — the same registry wa-js itself uses.
 *
 * The phone answers asynchronously: it does NOT come back in this response.
 * The reply arrives as a new history-sync notification chunk which WhatsApp
 * Web then decodes into its message store, after which an ordinary
 * get-messages call will see the older messages. Verified live: one request
 * took the history-sync-notification store from 21 to 22 chunks.
 *
 * Which means this endpoint is only half of the feature. If the backend
 * worker bridge is down, the chunk it produces will sit unprocessed like all
 * the others and the message count will not move — check
 * /history-sync-status before concluding the request itself failed.
 *
 * REFUSES to send while the recent history sync is still incomplete, and that
 * refusal is the whole reason this guard exists. WhatsApp Web processes the
 * notification queue strictly by descending syncType, so an ON_DEMAND chunk
 * (6) always sorts ahead of every RECENT one (3) — and the gate an ON_DEMAND
 * chunk has to pass is `historySyncStatus.recentCompleted === true`, which
 * cannot become true until those RECENT chunks are processed. One on-demand
 * request sent too early therefore parks a chunk at the head of the queue that
 * can never be processed and can never be overtaken: getNextUnprocessed keeps
 * picking it, keeps failing the gate, and returns "no chunk found" forever.
 *
 * That is not theory. Four such chunks (from this endpoint being called during
 * the initial sync) held 22 RECENT chunks — about 30MB of real history —
 * frozen at 'notification_stored'. Dropping the four and restarting the loop
 * took WhatsApp Web's message store from 1,526 to 6,014 rows in 40 seconds.
 * /unblock-history-sync below clears a queue that is already in that state.
 */
export async function requestOlderMessages(req: Request, res: Response) {
  /**
     #swagger.tags = ["Messages"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.parameters["phone"] = {
      schema: '5521999999999@c.us'
     }
   */
  const { phone } = req.params;
  try {
    const result = await req.client.page.evaluate(
      async ({ chatId }) => {
        const out: any = { chatId };
        const req_ = (window as any).require;
        if (typeof req_ !== 'function') {
          out.error =
            'WhatsApp Web module registry (window.require) unavailable';
          return out;
        }
        let sender: any;
        let gating: any;
        let utils: any;
        try {
          sender = req_('WAWebSendNonMessageDataRequest');
          gating = req_('WAWebSyncGatingUtils');
          utils = req_('WAWebNonMessageDataRequestHistorySyncOnDemandUtils');
        } catch (e) {
          out.error = `module lookup failed: ${e}`;
          return out;
        }
        if (typeof sender?.sendPeerDataOperationRequest !== 'function') {
          out.error = 'sendPeerDataOperationRequest missing from this build';
          return out;
        }

        out.onDemandEnabled = gating?.isHistorySyncOnDemandEnabled?.() ?? null;
        // WhatsApp Web trips this itself after repeated failures and then stops
        // sending; honouring it keeps us from hammering the phone.
        out.sendingDisabled =
          utils?.historySyncOnDemandRequestsFailureRecord
            ?.disableRequestSending ?? null;
        if (out.sendingDisabled === true) {
          out.error =
            'WhatsApp Web has disabled on-demand requests after repeated failures';
          return out;
        }

        // The queue-deadlock guard (see the block comment above). WhatsApp Web's
        // own UI only offers the "older messages" banner once the recent sync is
        // done, so this refusal keeps us to what the real client would do.
        try {
          const prefs = req_('WAWebUserPrefsHistorySync');
          const status = await prefs.getHistorySyncStatus();
          out.recentCompleted = status?.recentCompleted === true;
          if (out.recentCompleted !== true) {
            const table = req_(
              'WAWebSchemaHistorySyncNotification'
            ).getHistorySyncNotificationTable();
            const rows = await table.equals(['processed'], 0, {
              shouldDecrypt: false,
            });
            out.unprocessed = rows.length;

            // Interrupted RECENT syncs can leave the persisted flag false even
            // after every notification was applied. In that exact state the
            // old guard made short chats permanent: no work remained that
            // could ever flip the flag, while on-demand recovery was refused.
            // Repair the getter used by WhatsApp's own ON_DEMAND processor, but
            // only after the notification table proves there is no chunk that
            // could be overtaken or deadlocked.
            if (rows.length === 0) {
              const original = prefs.getHistorySyncStatus.bind(prefs);
              prefs.getHistorySyncStatus = async (...args: any[]) => {
                const current = await original(...args);
                return { ...(current || {}), recentCompleted: true };
              };
              const repaired = await prefs.getHistorySyncStatus();
              out.recentCompleted = repaired?.recentCompleted === true;
              out.recentCompletedRepaired = out.recentCompleted;
            }
          }
        } catch (e) {
          out.recentCompleted = null;
        }
        if (out.recentCompleted !== true) {
          out.error =
            'recent history sync is not complete yet — sending an on-demand ' +
            'request now would park a chunk the queue can never get past';
          return out;
        }

        const chat = (window as any).WAPI?.getChat?.(chatId);
        out.endOfHistoryTransferType = chat?.endOfHistoryTransferType ?? null;
        try {
          out.primaryHasMore = req_(
            'WAWebHistorySyncUtils'
          ).primaryHasMoreMessagesReadyToLoad(chat?.endOfHistoryTransferType);
        } catch (e) {
          out.primaryHasMore = null;
        }

        let wid;
        try {
          wid = (window as any).WPP.whatsapp.WidFactory.createWid(chatId);
        } catch (e) {
          out.error = `invalid chat id: ${e}`;
          return out;
        }
        try {
          const oldestMsg = await utils.getOldestMsgInChatFromDB(wid);
          const oldestMsgKey = oldestMsg?.id ?? oldestMsg?.key ?? oldestMsg;
          if (!oldestMsgKey) {
            out.error = 'oldest message key unavailable for on-demand request';
            return out;
          }
          out.oldestMsgKey = String(
            oldestMsgKey?._serialized ?? oldestMsgKey
          );
          // 3 === Message$PeerDataOperationRequestType.HISTORY_SYNC_ON_DEMAND.
          // Read from the protobuf enum when available so a renumbering in a
          // future build does not silently send the wrong request type.
          let kind = 3;
          try {
            const pb = req_('WAWebProtobufsE2E.pb');
            const v =
              pb?.Message$PeerDataOperationRequestType?.HISTORY_SYNC_ON_DEMAND;
            if (typeof v === 'number') kind = v;
          } catch (e) {
            /* keep the literal */
          }
          out.requestType = kind;
          let chatModel = req_('WAWebCollections')?.Chat?.get?.(wid);
          if (!chatModel) {
            try {
              chatModel = await req_('WAWebCollections')?.Chat?.find?.(wid);
            } catch (e) {
              /* handled below */
            }
          }
          // Prefer the resolved chat model's own canonical id when available:
          // it may carry a different JID form (@lid vs @c.us) than the wid
          // just built above, and sendPeerDataOperationRequest needs whatever
          // form WhatsApp Web itself already tracks this chat under. But a
          // chat WAWebCollections.Chat hasn't hydrated yet must still get its
          // request sent with the raw wid rather than being refused outright
          // — @lid-only chats routinely bridge into this collection late (see
          // this file's own comments elsewhere on @lid/@c.us mismatches), and
          // silently never requesting their older history is worse than
          // sending with the same wid this endpoint always used before the
          // chat-model lookup existed.
          const requestChatId = chatModel?.id ?? wid;
          out.requestPayloadMode = chatModel?.id ? 'chatModelId' : 'wid';
          await sender.sendPeerDataOperationRequest(kind, {
            chatId: requestChatId,
          });
          out.requested = true;
        } catch (e: any) {
          out.error = `send failed: ${e?.message || e}`;
        }
        return out;
      },
      { chatId: phone }
    );

    req.logger.info(
      `[requestOlderMessages] ${phone}: ${JSON.stringify(result)}`
    );
    res.status(result?.error ? 500 : 200).json({
      status: result?.error ? 'error' : 'success',
      response: result,
    });
  } catch (e: any) {
    req.logger.error(
      `Error in requestOlderMessages: ${e?.message || e}\nStack: ${
        e?.stack || ''
      }`
    );
    res.status(500).json({
      status: 'error',
      response: 'Error on request older messages',
      error: { message: e?.message || String(e) },
    });
  }
}

/**
 * Report whether WhatsApp Web can actually ingest history at all.
 *
 * Everything here is read-only and cheap, and it exists because the failure
 * mode it describes is completely silent from the outside: get-messages keeps
 * answering 200 with a short list, which is indistinguishable from a chat
 * that really is that short. The three fields that matter:
 *
 *   backendWorkerBridgeReady — false means the chunk decoder is not running.
 *     Every history-sync chunk will stay at 'notification_stored' forever and
 *     no amount of syncing or on-demand requesting will add a single message.
 *   unprocessedChunks / chunkStatus — chunks the phone already delivered that
 *     are still waiting. A nonzero count next to a ready bridge is normal and
 *     transient; next to a dead bridge it is the whole bug.
 *   storedMessages / storedChats — WhatsApp Web's own message count. When
 *     this is ~1 per chat, WinZapp is not losing messages, it is faithfully
 *     reporting that WhatsApp Web has none.
 */
export async function getHistorySyncStatus(req: Request, res: Response) {
  /**
     #swagger.tags = ["Messages"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    const result = await req.client.page.evaluate(async () => {
      const out: any = {};
      const req_ = (window as any).require;
      if (typeof req_ !== 'function') {
        out.error = 'WhatsApp Web module registry (window.require) unavailable';
        return out;
      }
      const safe = async (label: string, fn: () => any) => {
        try {
          out[label] = await fn();
        } catch (e: any) {
          out[label] = `err: ${e?.message || e}`;
        }
      };

      await safe('backendWorkerBridgeReady', () =>
        req_('WAWebBackendWorkerClient').isBackendWorkerBridgeReady()
      );
      // The four below answer questions a captured wppconnect.log currently
      // cannot, in the one failure that matters most: a freshly paired session
      // where the phone shows "keep the app open" for three minutes, nothing
      // at all arrives (chats 0, messages 0, unprocessed 0, contacts just
      // "You"), and WhatsApp then unlinks the device. Nothing in the log says
      // whether the page considers itself visible, nor which build the code
      // actually running came from — and both are live hypotheses:
      //
      //   pageVisibility / pageHasFocus — WinZapp runs Chrome headless and
      //     the page never gets a real window. If WhatsApp Web gates the
      //     history handshake on being active, this is where it shows.
      //   webVersion — the document is served from the pinned build
      //     (@wppconnect/wa-version's newest), but every sub-resource,
      //     including the workers, is fetched live and unintercepted. A page
      //     reporting a version other than the one start.js logged pinning is
      //     a version split, which is exactly the shape of a handshake that
      //     stalls with no error.
      //   serviceWorker — a controlling service worker can serve the current
      //     build's assets past the document-only interception.
      await safe('pageVisibility', () => document.visibilityState);
      await safe('pageHasFocus', () => document.hasFocus());
      // WAPI.getWAVersion() is the same reader host.layer.js uses for its
      // "WhatsApp WEB version: ..." log line, so the two are directly
      // comparable against start.js's "Pinning WhatsApp Web to ...".
      await safe('webVersion', () => (window as any).WAPI?.getWAVersion?.());
      await safe('serviceWorker', () => {
        const controller = navigator.serviceWorker?.controller;
        return controller
          ? { scriptURL: controller.scriptURL, state: controller.state }
          : null;
      });
      await safe('persistedStorage', () => navigator.storage.persisted());
      await safe(
        'notificationApi',
        () => typeof (globalThis as any).Notification
      );
      await safe('chunkStatus', () =>
        req_('WAWebUserPrefsHistorySync').getRecentSyncSingleChunkStatus()
      );
      await safe('initialSyncComplete', () =>
        req_('WAWebUserPrefsHistorySync').getInitialHistorySyncComplete()
      );
      // Distinct from initialSyncComplete: the bulk "recent" sync finishing is
      // what lets on-demand requests through (see requestOlderMessages). Read
      // as a diagnostic only — a session whose recent sync was interrupted
      // leaves this false forever, so nothing schedules work off it.
      await safe('recentCompleted', async () => {
        const s = await req_(
          'WAWebUserPrefsHistorySync'
        ).getHistorySyncStatus();
        return s?.recentCompleted === true;
      });
      await safe('earliestDate', () =>
        req_('WAWebUserPrefsHistorySync').getHistorySyncEarliestDate()
      );
      await safe('unprocessedChunks', async () => {
        const list = await req_(
          'WAWebHistorySyncNotificationUtils'
        ).getUnprocessedRecentSyncNotifications();
        return Array.isArray(list) ? list.length : list;
      });
      await safe('onDemandEnabled', () =>
        req_('WAWebSyncGatingUtils').isHistorySyncOnDemandEnabled()
      );

      // Counts straight out of WhatsApp Web's own IndexedDB. This is the
      // ground truth WinZapp's sync is limited by.
      await safe(
        'storeCounts',
        () =>
          new Promise((resolve) => {
            const open = indexedDB.open('model-storage');
            open.onerror = () => resolve('cannot open model-storage');
            open.onsuccess = () => {
              const db = open.result;
              const counts: any = {};
              const stores = ['message', 'chat', 'history-sync-notification'];
              let left = stores.length;
              const done = () => {
                if (--left === 0) {
                  db.close();
                  resolve(counts);
                }
              };
              for (const s of stores) {
                try {
                  const q = db
                    .transaction(s, 'readonly')
                    .objectStore(s)
                    .count();
                  q.onsuccess = () => {
                    counts[s] = q.result;
                    done();
                  };
                  q.onerror = () => {
                    counts[s] = 'err';
                    done();
                  };
                } catch (e) {
                  counts[s] = 'missing';
                  done();
                }
              }
            };
          })
      );
      return out;
    });

    res.status(200).json({ status: 'success', response: result });
  } catch (e: any) {
    req.logger.error(
      `Error in getHistorySyncStatus: ${e?.message || e}\nStack: ${
        e?.stack || ''
      }`
    );
    res.status(500).json({
      status: 'error',
      response: 'Error on get history sync status',
      error: { message: e?.message || String(e) },
    });
  }
}

/**
 * Clear a history-sync queue that has deadlocked, then restart the loop.
 *
 * WhatsApp Web picks the next chunk to process by sorting the unprocessed
 * notifications by *descending* syncType and taking the first one. ON_DEMAND
 * (6) therefore outranks RECENT (3) — and an ON_DEMAND chunk is only allowed
 * through when `historySyncStatus.recentCompleted === true`, which stays false
 * until the RECENT chunks it is standing in front of have been processed.
 * Nothing breaks the tie: getNextUnprocessedNotification picks the same
 * ON_DEMAND row on every pass, fails the same gate, and the loop reports "no
 * chunk found" while a full backlog sits behind it. WinZapp created exactly
 * that state by calling /request-older-messages during the initial sync (now
 * refused at the source — see requestOlderMessages).
 *
 * So this drops the ON_DEMAND rows that cannot be processed and kicks the
 * progressive-processing job. What is lost is small and unusable anyway: those
 * chunks were a few KB each and would never have been decoded. What it frees
 * is the entire recent backlog — measured live, 1,526 → 6,014 messages inside
 * 40 seconds, with chunks moving through 'message_preprocessed' to 'applied'.
 *
 * A no-op when the queue is healthy: with recentCompleted true, on-demand
 * chunks are legitimate and are left exactly where they are.
 */
export async function unblockHistorySync(req: Request, res: Response) {
  /**
     #swagger.tags = ["Messages"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    const result = await req.client.page.evaluate(async () => {
      const out: any = { removed: [] };
      const req_ = (window as any).require;
      if (typeof req_ !== 'function') {
        out.error = 'WhatsApp Web module registry (window.require) unavailable';
        return out;
      }

      let api: any;
      let table: any;
      let pb: any;
      try {
        api = req_('WAWebApiHistorySyncNotification');
        table = req_(
          'WAWebSchemaHistorySyncNotification'
        ).getHistorySyncNotificationTable();
        pb = req_('WAWebProtobufsHistorySync.pb');
      } catch (e) {
        out.error = `module lookup failed: ${e}`;
        return out;
      }

      // Read the enum rather than hardcoding 3/6: a renumbering in a future
      // build would otherwise make this delete the wrong rows.
      const ON_DEMAND = pb?.HistorySync$HistorySyncType?.ON_DEMAND;
      const RECENT = pb?.HistorySync$HistorySyncType?.RECENT;
      if (typeof ON_DEMAND !== 'number' || typeof RECENT !== 'number') {
        out.error = 'HistorySyncType enum not readable from this build';
        return out;
      }

      const status = await req_(
        'WAWebUserPrefsHistorySync'
      ).getHistorySyncStatus();
      out.recentCompleted = status?.recentCompleted === true;

      // Same query the processing loop runs (a scalar 0, not [0] — the
      // compound-index form silently matches nothing).
      const rows = await table.equals(['processed'], 0, {
        shouldDecrypt: false,
      });
      out.unprocessed = rows.length;
      out.recentWaiting = rows.filter((r: any) => r.syncType === RECENT).length;
      out.onDemandPending = rows.filter(
        (r: any) => r.syncType === ON_DEMAND
      ).length;

      if (out.recentCompleted === true) {
        out.skipped =
          'recent sync complete — on-demand chunks can be processed';
      } else {
        for (const row of rows) {
          if (row.syncType !== ON_DEMAND) continue;
          // WhatsApp Web's own drop path: clears the in-flight marker and
          // removes the row.
          await api.updateCurrentlyProcessed(
            row.msgKey,
            row.syncType,
            row.chunkOrder
          );
          out.removed.push(String(row.msgKey));
        }
      }

      if (out.removed.length > 0 || out.unprocessed > 0) {
        try {
          const boot =
            (window as any).requireInterop?.('WAWebSyncBootstrap') ??
            req_('WAWebSyncBootstrap')?.default;
          const source = req_(
            'WAWebHistorySyncNotificationUtils'
          ).HistorySyncScheduleSource;
          const continueSync =
            boot?.continueProgressiveHistorySyncProcessingV2;
          if (typeof continueSync !== 'function') {
            throw new Error(
              'continueProgressiveHistorySyncProcessingV2 unavailable'
            );
          }
          // A page reload creates a fresh bootstrap instance after the
          // persisted initial sync has already completed. Its in-memory
          // initialChatHistory flag then stays false, and every manual restart
          // silently exits without touching the queued RECENT chunks.
          const initialComplete = await req_(
            'WAWebUserPrefsHistorySync'
          ).getInitialHistorySyncComplete();
          if (
            initialComplete === true &&
            typeof boot?.setInitialChatHistorySynced === 'function'
          ) {
            await boot.setInitialChatHistorySynced();
            out.bootstrapRehydrated = true;
          }
          // Fire-and-forget: the job runs for as long as it needs (chunks are
          // ~1.4MB each), and this response must not wait for it.
          continueSync.call(boot, source.ManualRestart);
          out.restarted = true;
        } catch (e) {
          out.restartError = String(e);
        }
      }
      return out;
    });

    req.logger.info(`[unblockHistorySync] ${JSON.stringify(result)}`);
    res.status(result?.error ? 500 : 200).json({
      status: result?.error ? 'error' : 'success',
      response: result,
    });
  } catch (e: any) {
    req.logger.error(
      `Error in unblockHistorySync: ${e?.message || e}\nStack: ${
        e?.stack || ''
      }`
    );
    res.status(500).json({
      status: 'error',
      response: 'Error on unblock history sync',
      error: { message: e?.message || String(e) },
    });
  }
}

export async function sendContactVcard(req: Request, res: Response) {
  /**
     #swagger.tags = ["Messages"]
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
              name: { type: "string" },
              contactsId: { type: "array" },
            }
          },
          examples: {
            "Default": {
              value: {
                phone: "5521999999999",
                isGroup: false,
                name: 'Name of contact',
                contactsId: ['5521999999999'],
              }
            },
          }
        }
      }
     }
   */
  const { phone, contactsId, name = null, isGroup = false } = req.body;
  try {
    let response;
    for (const contato of contactToArray(phone, isGroup)) {
      response = await req.client.sendContactVcard(
        `${contato}`,
        contactsId,
        name
      );
    }

    res.status(200).json({ status: 'success', response: response });
  } catch (error) {
    req.logger.error(error);
    res.status(500).json({
      status: 'error',
      message: 'Error on send contact vcard',
      error: error,
    });
  }
}

export async function sendMute(req: Request, res: Response) {
  /**
     #swagger.tags = ["Chat"]
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
              time: { type: "number" },
              type: { type: "string" },
            }
          },
          examples: {
            "Default": {
              value: {
                phone: "5521999999999",
                isGroup: false,
                time: 1,
                type: 'hours',
              }
            },
          }
        }
      }
     }
   */
  const { phone, time, type = 'hours', isGroup = false } = req.body;

  /*
   * WinZapp patch — do not call req.client.sendMute().
   *
   * That layer runs the legacy `WAPI.sendMute` shim, which drives
   * `window.Store.SendMute.sendConversationMute()` directly and then decides
   * success purely from `response.status === 200`. On current WhatsApp Web
   * builds that call no longer answers with the shape the shim expects, so
   * every mute came back as
   *   {"erro":true,"text":"This chat is already mute","type":"sendMute"}
   * — a hardcoded string the shim emits for *any* non-200, regardless of
   * whether the chat was actually muted. WPPConnect's own layer rethrows that
   * object, so this route answered 500 and no chat could ever be muted.
   * (The `to` field in those errors is a MsgKey, not a chat — more evidence
   * that the shim's `getchatId()` lookup is resolving against internals that
   * moved.)
   *
   * `WPP.chat.mute()`/`WPP.chat.unmute()` from the bundled wa-js are the
   * maintained equivalents and take an absolute expiration (or a duration in
   * seconds), so they are also immune to the "already muted" state the old
   * path tripped over: re-muting an already-muted chat simply moves the
   * expiration.
   */
  const seconds = (() => {
    const n = Number(time) || 0;
    switch (String(type)) {
      case 'minutes':
        return n * 60;
      case 'day':
      case 'days':
        return n * 86400;
      case 'year':
      case 'years':
        return n * 31536000;
      case 'hours':
      default:
        return n * 3600;
    }
  })();

  try {
    let response;
    for (const contato of contactToArray(phone, isGroup)) {
      response = await req.client.page.evaluate(
        async ({ chatId, duration }) => {
          const WPP = (window as any).WPP;
          if (!WPP?.chat?.mute || !WPP?.chat?.unmute) {
            throw new Error('WPP.chat.mute is unavailable in this wa-js build');
          }
          if (duration <= 0) {
            await WPP.chat.unmute(chatId);
            return { wid: chatId, isMuted: false, expiration: 0 };
          }
          const result = await WPP.chat.mute(chatId, { duration });
          if (!result?.isMuted) {
            throw new Error('WhatsApp did not confirm the mute operation');
          }
          // The Wid instance does not survive serialization to Node.
          return {
            wid: String(result?.wid?._serialized ?? chatId),
            isMuted: !!result?.isMuted,
            expiration: Number(result?.expiration ?? 0),
          };
        },
        { chatId: `${contato}`, duration: seconds }
      );
    }

    res.status(200).json({ status: 'success', response: response });
  } catch (error: any) {
    req.logger.error(error);
    res.status(500).json({
      status: 'error',
      message: 'Error on send mute',
      error: error?.message || error,
    });
  }
}

export async function sendSeen(req: Request, res: Response) {
  /**
     #swagger.tags = ["Chat"]
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
            }
          },
          examples: {
            "Default": {
              value: {
                phone: "5521999999999",
                isGroup: false,
              }
            },
          }
        }
      }
     }
   */
  const { phone, unread = false } = req.body;
  const session = req.session;

  try {
    const results: any = [];
    const phoneList = Array.isArray(phone) ? phone : [phone];
    for (const contato of phoneList) {
      // Use WA-JS directly for both states. WPPConnect exposes sendSeen(), but
      // has no matching mark-unread wrapper, and its return value can resolve
      // without proving that the browser operation succeeded.
      const changed = await req.client.page.evaluate(
        async ({ chatId, markUnread }: { chatId: string; markUnread: boolean }) => {
          const WPP = (window as any).WPP;
          if (!WPP?.chat) throw new Error('WA-JS chat API is unavailable');
          // find() registers the chat in WA-JS's in-browser Store so the
          // markIs*() call below has something to resolve: assertGetChat()
          // throws "Chat not found" for any chat the Store hasn't loaded, and
          // WinZapp routinely syncs chats over REST that this Chrome session
          // never opened (reported as conversations staying unread on both the
          // app and the phone right after being opened).
          //
          // This used to be wrapped in a try/catch that swallowed find()
          // failures and let markIsRead() surface its own error. It is now
          // deliberately unguarded: the caller needs a definite success or
          // failure to decide whether to roll the local unread state back
          // (_sync_conversation_read_state in client/main.py), and a find()
          // that failed makes the following call's outcome untrustworthy
          // rather than merely uninformative. A genuine transient failure
          // surfaces as a normal error and is retried from the Python side —
          // 3 attempts across both JID aliases, see _sync_conversation_read_state.
          if (WPP.chat.find) await WPP.chat.find(chatId);
          const operation = markUnread
            ? WPP.chat.markIsUnread
            : WPP.chat.markIsRead;
          if (typeof operation !== 'function') {
            throw new Error(
              markUnread ? 'markIsUnread is unavailable' : 'markIsRead is unavailable'
            );
          }
          const result = await operation(chatId);
          if (!result) throw new Error('WhatsApp did not confirm the read-state change');
          return true;
        },
        { chatId: contato, markUnread: Boolean(unread) }
      );
      results.push(changed);
    }
    returnSucess(res, session, phone, results);
  } catch (error) {
    returnError(req, res, session, error);
  }
}

export async function setChatState(req: Request, res: Response) {
  /**
     #swagger.deprecated=true
     #swagger.tags = ["Chat"]
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
              chatstate: { type: "string" },
            }
          },
          examples: {
            "Default": {
              value: {
                phone: "5521999999999",
                isGroup: false,
                chatstate: "1",
              }
            },
          }
        }
      }
     }
   */
  const { phone, chatstate, isGroup = false } = req.body;

  try {
    let response;
    for (const contato of contactToArray(phone, isGroup)) {
      response = await req.client.setChatState(`${contato}`, chatstate);
    }

    res.status(200).json({ status: 'success', response: response });
  } catch (error) {
    req.logger.error(error);
    res.status(500).json({
      status: 'error',
      message: 'Error on send chat state',
      error: error,
    });
  }
}

export async function setTemporaryMessages(req: Request, res: Response) {
  /**
     #swagger.tags = ["Messages"]
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
              value: { type: "boolean" },
            }
          },
          examples: {
            "Default": {
              value: {
                phone: "5521999999999",
                isGroup: false,
                value: true,
              }
            },
          }
        }
      }
     }
   */
  const { phone, value = true, isGroup = false } = req.body;

  try {
    let response;
    for (const contato of contactToArray(phone, isGroup)) {
      response = await req.client.setTemporaryMessages(`${contato}`, value);
    }

    res.status(200).json({ status: 'success', response: response });
  } catch (error) {
    req.logger.error(error);
    res.status(500).json({
      status: 'error',
      message: 'Error on set temporary messages',
      error: error,
    });
  }
}

export async function setTyping(req: Request, res: Response) {
  /**
     #swagger.tags = ["Chat"]
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
              value: { type: "boolean" },
            }
          },
          examples: {
            "Default": {
              value: {
                phone: "5521999999999",
                isGroup: false,
                value: true,
              }
            },
          }
        }
      }
     }
   */
  const { phone, value = true, isGroup = false } = req.body;
  // Fire-and-forget: respond immediately — the client never uses this response.
  res.status(200).json({ status: 'success' });

  const getActiveJid = async (targetJid: string): Promise<string> => {
    try {
      return await (req.client as any).page.evaluate((jid: string) => {
        if ((window as any).WPP?.chat?.get(jid)) {
          return jid;
        }
        const contact = (window as any).WPP?.contact?.get(jid);
        if (contact) {
          if (jid.endsWith('@c.us') && contact.lid) {
            const lidStr =
              typeof contact.lid === 'string'
                ? contact.lid
                : contact.lid?._serialized || contact.lid?.toString() || '';
            if (lidStr && (window as any).WPP?.chat?.get(lidStr)) return lidStr;
          }
          if (jid.endsWith('@lid') && contact.id) {
            const idStr =
              typeof contact.id === 'string'
                ? contact.id
                : contact.id?._serialized || contact.id?.toString() || '';
            if (idStr && (window as any).WPP?.chat?.get(idStr)) return idStr;
          }
        }
        return jid;
      }, targetJid);
    } catch {
      return targetJid;
    }
  };

  for (const contato of contactToArray(phone, isGroup)) {
    (async () => {
      const resolvedContato = await getActiveJid(contato);
      req.logger.warn(
        `[setTyping] contato: ${contato}, resolvedContato: ${resolvedContato}, value: ${value}`
      );
      const p = value
        ? req.client.startTyping(resolvedContato)
        : req.client.stopTyping(resolvedContato);
      await p;
    })().catch((err: any) => {
      const msg: string = err?.message ?? String(err);
      req.logger.warn('[setTyping] Error: ' + msg);
    });
  }
}

export async function setRecording(req: Request, res: Response) {
  /**
     #swagger.tags = ["Chat"]
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
              duration: { type: "number" },
              value: { type: "boolean" },
            }
          },
          examples: {
            "Default": {
              value: {
                phone: "5521999999999",
                isGroup: false,
                duration: 5,
                value: true,
              }
            },
          }
        }
      }
     }
   */
  const { phone, value = true, duration, isGroup = false } = req.body;
  // Fire-and-forget: respond immediately — the client never uses this response.
  res.status(200).json({ status: 'success' });

  const getActiveJid = async (targetJid: string): Promise<string> => {
    try {
      return await (req.client as any).page.evaluate((jid: string) => {
        if ((window as any).WPP?.chat?.get(jid)) {
          return jid;
        }
        const contact = (window as any).WPP?.contact?.get(jid);
        if (contact) {
          if (jid.endsWith('@c.us') && contact.lid) {
            const lidStr =
              typeof contact.lid === 'string'
                ? contact.lid
                : contact.lid?._serialized || contact.lid?.toString() || '';
            if (lidStr && (window as any).WPP?.chat?.get(lidStr)) return lidStr;
          }
          if (jid.endsWith('@lid') && contact.id) {
            const idStr =
              typeof contact.id === 'string'
                ? contact.id
                : contact.id?._serialized || contact.id?.toString() || '';
            if (idStr && (window as any).WPP?.chat?.get(idStr)) return idStr;
          }
        }
        return jid;
      }, targetJid);
    } catch {
      return targetJid;
    }
  };

  for (const contato of contactToArray(phone, isGroup)) {
    (async () => {
      const resolvedContato = await getActiveJid(contato);
      req.logger.warn(
        `[setRecording] contato: ${contato}, resolvedContato: ${resolvedContato}, value: ${value}`
      );
      const p = value
        ? req.client.startRecording(resolvedContato, duration)
        : req.client.stopRecording(resolvedContato);
      await p;
    })().catch((err: any) => {
      const msg: string = err?.message ?? String(err);
      req.logger.warn('[setRecording] Error: ' + msg);
    });
  }
}

export async function checkNumberStatus(req: Request, res: Response) {
  /**
     #swagger.tags = ["Misc"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.parameters["phone"] = {
      schema: '5521999999999'
     }
   */
  const { phone } = req.params;
  try {
    let response;
    for (const contato of contactToArray(phone, false)) {
      response = await req.client.checkNumberStatus(`${contato}`);
    }

    res.status(200).json({ status: 'success', response: response });
  } catch (error) {
    req.logger.error(error);
    res.status(500).json({
      status: 'error',
      message: 'Error on check number status',
      error: error,
    });
  }
}

export async function getContact(req: Request, res: Response) {
  /**
     #swagger.tags = ["Chat"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.parameters["phone"] = {
      schema: '5521999999999'
     }
   */
  const { phone = true } = req.params;
  try {
    let response;
    for (const contato of contactToArray(phone as string, false)) {
      response = await req.client.getContact(contato);
    }

    return res.status(200).json({ status: 'success', response: response });
  } catch (error) {
    req.logger.error(error);
    return res
      .status(500)
      .json({ status: 'error', message: 'Error on get contact', error: error });
  }
}

export async function getAllContacts(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Contact"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
   */
  try {
    let response;
    try {
      response = await req.client.getAllContacts();
    } catch (firstError: any) {
      // ContactStore and ChatStore disappear together in the captured broken
      // session. Rehydrating the persisted chats also restores their contact
      // models; retry once instead of turning a transient store loss into a
      // permanent 500 for the whole sync.
      req.logger.warn(
        `[getAllContacts] Initial read failed; attempting ChatStore recovery: ${
          firstError?.message || firstError
        }`
      );
      await listChatsWithStoreRecovery(req, { ignoreGroupMetadata: true });
      response = await req.client.getAllContacts();
    }

    if (Array.isArray(response)) {
      // Was req.client.getAllChats() — the deprecated, heavier WAPI call
      // (no ignoreGroupMetadata option, does its own per-group metadata
      // prefetch). On an account with hundreds of chats it competes for
      // WhatsApp Web's single JS thread with the real chat sync's own
      // list-chats (WPP.chat.list) calls running around the same time —
      // and get_remote_contacts() (WinZapp's Python caller) retries THIS
      // endpoint up to 5 times on its own whenever it gets 0 contacts back,
      // so one call here could become five. Confirmed live: a sync that
      // never finished syncing more than a few dozen of several hundred
      // chats logged 54 "getAllChats deprecated" warnings in ~15 minutes,
      // vs. 1 for the same account's next, successful sync — this endpoint
      // was starving the chat-list fetch of the page's JS thread it needed
      // to actually finish. listChats() wraps the same modern, non-
      // deprecated WPP.chat.list() the list-chats route already uses, with
      // the same ignoreGroupMetadata skip.
      const chats = await listChatsWithStoreRecovery(req, {
        ignoreGroupMetadata: true,
      });
      const activeChatIds = new Set(
        chats.map((c: any) => c?.id?._serialized || c?.id).filter(Boolean)
      );

      response = response.filter((c: any) => {
        if (!c) return false;
        const jid = c.id?._serialized || c.id;
        return (
          c.isMyContact === true || c.isMe === true || activeChatIds.has(jid)
        );
      });
    }

    res.status(200).json({
      status: 'success',
      response: observePayload(SyncContactListSchema, response, {
        logger: req.logger,
        endpoint: 'all-contacts',
      }),
    });
  } catch (error) {
    req.logger.error(error);
    res.status(500).json({
      status: 'error',
      message: 'Error on get all constacts',
      error: error,
    });
  }
}

export async function getNumberProfile(req: Request, res: Response) {
  /**
     #swagger.deprecated=true
     #swagger.tags = ["Chat"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.parameters["phone"] = {
      schema: '5521999999999'
     }
   */
  const { phone = true } = req.params;
  try {
    let response;
    for (const contato of contactToArray(phone as string, false)) {
      response = await req.client.getNumberProfile(contato);
    }

    res.status(200).json({ status: 'success', response: response });
  } catch (error) {
    req.logger.error(error);
    res.status(500).json({
      status: 'error',
      message: 'Error on get number profile',
      error: error,
    });
  }
}

export async function getProfilePicFromServer(req: Request, res: Response) {
  /**
     #swagger.tags = ["Contact"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.parameters["phone"] = {
      schema: '5521999999999'
     }
   */
  const { phone = true } = req.params;
  const { isGroup = false } = req.query;
  try {
    let response;
    for (const contato of contactToArray(phone as string, isGroup as boolean)) {
      response = await req.client.getProfilePicFromServer(contato);
    }

    res.status(200).json({ status: 'success', response: response });
  } catch (error) {
    req.logger.error(error);
    res.status(500).json({
      status: 'error',
      message: 'Error on  get profile pic',
      error: error,
    });
  }
}

export async function getStatus(req: Request, res: Response) {
  /**
     #swagger.tags = ["Contact"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.parameters["phone"] = {
      schema: '5521999999999'
     }
   */
  const { phone = true } = req.params;
  try {
    let response;
    for (const contato of contactToArray(phone as string, false)) {
      response = await req.client.getStatus(contato);
    }
    res.status(200).json({ status: 'success', response: response });
  } catch (error) {
    req.logger.error(error);
    res
      .status(500)
      .json({ status: 'error', message: 'Error on  get status', error: error });
  }
}

export async function setProfileStatus(req: Request, res: Response) {
  /**
     #swagger.tags = ["Profile"]
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
        $status: 'My new status',
      }
     }
     
     #swagger.requestBody = {
      required: true,
      "@content": {
        "application/json": {
          schema: {
            type: "object",
            properties: {
              status: { type: "string" },
            }
          },
          examples: {
            "Default": {
              value: {
                status: "My new status",
              }
            },
          }
        }
      }
     }
   */
  const { status } = req.body;
  try {
    const response = await req.client.setProfileStatus(status);

    res.status(200).json({ status: 'success', response: response });
  } catch (e) {
    req.logger.error(e);
    res
      .status(500)
      .json({ status: 'error', message: 'Error on set profile status' });
  }
}
export async function rejectCall(req: Request, res: Response) {
  /**
     #swagger.tags = ["Misc"]
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
              callId: { type: "string" },
            }
          },
          examples: {
            "Default": {
              value: {
                callId: "<callid>",
              }
            },
          }
        }
      }
     }
   */
  const { callId } = req.body;
  try {
    const response = await req.client.rejectCall(callId);

    res.status(200).json({ status: 'success', response: response });
  } catch (e) {
    req.logger.error(e);
    res
      .status(500)
      .json({ status: 'error', message: 'Error on rejectCall', error: e });
  }
}

export async function starMessage(req: Request, res: Response) {
  /**
     #swagger.tags = ["Messages"]
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
              messageId: { type: "string" },
              star: { type: "boolean" },
            }
          },
          examples: {
            "Default": {
              value: {
                messageId: "5521999999999",
                star: true,
              }
            },
          }
        }
      }
     }
   */
  const { messageId, star = true } = req.body;
  try {
    const response = await req.client.starMessage(messageId, star);

    res.status(200).json({ status: 'success', response: response });
  } catch (error) {
    req.logger.error(error);
    res.status(500).json({
      status: 'error',
      message: 'Error on  start message',
      error: error,
    });
  }
}

export async function getReactions(req: Request, res: Response) {
  /**
     #swagger.tags = ["Messages"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.parameters["messageId"] = {
      schema: '<messageId>'
     }
   */
  const messageId = req.params.id;
  try {
    const response = await req.client.getReactions(messageId);

    res.status(200).json({ status: 'success', response: response });
  } catch (error) {
    req.logger.error(error);
    res.status(500).json({
      status: 'error',
      message: 'Error on get reactions',
      error: error,
    });
  }
}

export async function getVotes(req: Request, res: Response) {
  /**
     #swagger.tags = ["Messages"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.parameters["messageId"] = {
      schema: '<messageId>'
     }
   */
  const messageId = req.params.id;
  try {
    const response = await req.client.getVotes(messageId);

    res.status(200).json({ status: 'success', response: response });
  } catch (error) {
    req.logger.error(error);
    res
      .status(500)
      .json({ status: 'error', message: 'Error on get votes', error: error });
  }
}
export async function chatWoot(req: Request, res: Response): Promise<any> {
  /**
     #swagger.tags = ["Misc"]
     #swagger.description = 'You can point your Chatwoot to this route so that it can perform functions.'
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
              event: { type: "string" },
              private: { type: "string" },
            }
          },
          examples: {
            "Default": {
              value: {
                messageId: "conversation_status_changed",
                private: "false",
              }
            },
          }
        }
      }
     }
   */
  const { session } = req.params;
  const client: any = clientsArray[session];
  if (client == null || client.status !== 'CONNECTED') return;
  try {
    if (await client.isConnected()) {
      const event = req.body.event;
      const is_private = req.body.private || req.body.is_private;

      if (
        event == 'conversation_status_changed' ||
        event == 'conversation_resolved' ||
        is_private
      ) {
        return res
          .status(200)
          .json({ status: 'success', message: 'Success on receive chatwoot' });
      }

      const {
        message_type,
        phone = req.body.conversation.meta.sender.phone_number.replace('+', ''),
        message = req.body.conversation.messages[0],
      } = req.body;

      if (event != 'message_created' && message_type != 'outgoing')
        return res
          .status(200)
          .json({ status: 'success', message: 'Success on receive chatwoot' });
      for (const contato of contactToArray(phone, false)) {
        if (message_type == 'outgoing') {
          if (message.attachments) {
            const base_url = `${
              client.config.chatWoot.baseURL
            }/${message.attachments[0].data_url.substring(
              message.attachments[0].data_url.indexOf('/rails/') + 1
            )}`;

            // Check if attachments is Push-to-talk and send this
            if (message.attachments[0].file_type === 'audio') {
              await client.sendPtt(
                `${contato}`,
                base_url,
                'Voice Audio',
                message.content
              );
            } else {
              await client.sendFile(
                `${contato}`,
                base_url,
                'file',
                message.content
              );
            }
          } else {
            await client.sendText(contato, message.content);
          }
        }
      }
      res
        .status(200)
        .json({ status: 'success', message: 'Success on  receive chatwoot' });
    }
  } catch (e) {
    console.log(e);
    res.status(400).json({
      status: 'error',
      message: 'Error on  receive chatwoot',
      error: e,
    });
  }
}
export async function getPlatformFromMessage(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Misc"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.parameters["messageId"] = {
      schema: '<messageId>'
     }
   */
  try {
    const result = await req.client.getPlatformFromMessage(
      req.params.messageId
    );
    res.status(200).json(result);
  } catch (e) {
    req.logger.error(e);
    res.status(500).json({
      status: 'error',
      message: 'Error on get get platform from message',
      error: e,
    });
  }
}
