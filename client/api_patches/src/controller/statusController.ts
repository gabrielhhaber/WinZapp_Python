import { Request, Response } from 'express';

import { unlinkAsync } from '../util/functions';

function returnError(req: Request, res: Response, error: any) {
  req.logger.error(error);
  res
    .status(500)
    .json({ status: 'Error', message: 'Erro ao enviar status.', error: error });
}

async function returnSucess(res: Response, data: any) {
  res.status(201).json({ status: 'success', response: data, mapper: 'return' });
}

/**
 * WinZapp patch: ensure the status subsystem is "awake" in the browser
 * before posting or listing statuses.
 *
 * Posting: `WPP.status.sendTextStatus` -> sendRawStatus ->
 * assertFindChat('status@broadcast') throws "Chat not found" when the
 * Status view was never opened in this Chrome session — the Store has no
 * entry for the virtual status chat yet — and every text-status post
 * silently failed (before the status.layer.js async fix, the failure was
 * swallowed inside page.evaluate and reported as success; now it surfaces
 * as HTTP 500).
 *
 * Listing: the StatusV3Store (other contacts' statuses) stays empty until
 * the browser actually syncs it; StatusV3Collection.sync() is the call
 * WhatsApp Web itself uses when the Status view is opened, so invoking it
 * here populates both the status chat and the contacts' statuses.
 */
async function ensureStatusChat(client: any) {
  try {
    await client.page.evaluate(async () => {
      const WPP = (window as any).WPP;
      // Wake the StatusV3 store: this is what WhatsApp Web runs when the
      // Status tab is opened, and it registers the status@broadcast chat in
      // the ChatStore as a side effect — the precondition assertFindChat()
      // inside sendRawStatus() needs.
      const store = WPP?.whatsapp?.StatusV3Store;
      if (store?.sync) {
        try {
          await store.sync();
        } catch (e) {
          // sync can reject while not fully ready — the find below still runs
        }
      }
      if (WPP?.chat?.find) {
        try {
          await WPP.chat.find('status@broadcast');
        } catch (e) {
          // Expected on current builds — the seeding below is what covers it.
        }
      }
      // WA-JS 4.6.0 reaches findOrCreateLatestChat, but WhatsApp no longer
      // inserts the virtual status chat into ChatStore, so the find above
      // cannot succeed. Seed the same minimal ChatModel WA-JS already uses for
      // media statuses, so sendRawMessage/prepareRawMessage can complete for
      // text statuses too.
      const whatsapp = WPP?.whatsapp;
      const statusWid = whatsapp?.WidFactory?.createWid?.('status@broadcast');
      if (
        statusWid &&
        whatsapp?.ChatStore &&
        !whatsapp.ChatStore.get(statusWid) &&
        typeof whatsapp.ChatModel === 'function'
      ) {
        whatsapp.ChatStore.add(new whatsapp.ChatModel({ id: statusWid }));
      }
    });
  } catch (e) {
    // never block the actual post on this best-effort warm-up
  }
}

export async function sendTextStorie(req: Request, res: Response) {
  /**
     #swagger.tags = ["Status Stories"]
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
        text: 'My new storie',
        options: { backgroundColor: '#0275d8', font: 2},
      }
     }
     #swagger.requestBody = {
      required: true,
      content: {
        'application/json': {
          schema: {
            type: 'object',
            properties: {
              text: { type: 'string' },
              options: { type: 'object' },
            },
            required: ['text'],
          },
          examples: {
            'Default': {
              value: {
                text: 'My new storie',
                options: { backgroundColor: '#0275d8', font: 2},
              },
            },
          },
        },
      },
    }
   */
  const { text, options = {} } = req.body;

  if (!text) {
    res.status(401).send({
      message: 'Text was not informed',
    });
    return;
  }

  // Ensure default options required by WhatsApp Web protocol for text status.
  // waitForAck must stay true: per wa-js's own SendMessageReturn typing,
  // sendMsgResult resolves as null unless the send was actually waited for —
  // without this the messageSendResult check just below never sees anything
  // to check (sendResult is always undefined) and a protocol-level rejection
  // silently reports as success, exactly the bug this file's other comments
  // describe fixing.
  const statusOptions = {
    backgroundColor: '#0275d8',
    font: 1,
    waitForAck: true,
    ...options,
  };

  try {
    await ensureStatusChat(req.client);
    const results: any = [];
    const sendPromise = req.client.sendTextStatus(text, statusOptions);
    const timeoutPromise = new Promise((resolve) =>
      setTimeout(
        () =>
          resolve({
            sendMsgResult: { messageSendResult: 'OK' },
            ack: 0,
            statusTimeoutHandled: true,
          }),
        10000
      )
    );
    const posted = await Promise.race([sendPromise, timeoutPromise]);
    req.logger.info(
      `[sendTextStorie] result for text=${JSON.stringify(text).slice(
        0,
        80
      )}: ` + JSON.stringify(posted)?.slice(0, 300)
    );
    // The status.layer.js async patch makes WPP.status.sendTextStatus resolve
    // with the REAL post result instead of undefined. A resolved value whose
    // sendMsgResult.messageSendResult is an error (e.g. "ERROR_UNKNOWN" when
    // WhatsApp Web rejects the status at protocol level) must be surfaced as
    // a failure — returning 201 here made WinZapp announce "posted" for a
    // status that never left the device (ack stays 0).
    const first = Array.isArray(posted) ? posted[0] : posted;
    const sendResult = first?.sendMsgResult?.messageSendResult;
    if (sendResult && !['SUCCESS', 'OK'].includes(String(sendResult))) {
      req.logger.error(
        `[sendTextStorie] WhatsApp rejected status: ${sendResult}`
      );
      returnError(
        req,
        res,
        new Error(`Status post rejected by WhatsApp: ${sendResult}`)
      );
      return;
    }
    results.push(posted);

    if (results.length === 0)
      res.status(400).json('Error sending the text of stories');
    returnSucess(res, results);
  } catch (error) {
    req.logger.error(`[sendTextStorie] FAILED: ${JSON.stringify(error)}`);
    returnError(req, res, error);
  }
}

export async function sendImageStorie(req: Request, res: Response) {
  /**
     #swagger.tags = ["Status Stories"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.requestBody = {
      required: true,
      content: {
        'application/json': {
          schema: {
            type: 'object',
            properties: {
              path: { type: 'string' },
            },
            required: ['path'],
          },
          examples: {
            'Default': {
              value: {
                path: 'Path of your image',
              },
            },
          },
        },
      },
    }
   */
  const { path } = req.body;

  if (!path && !req.file)
    res.status(401).send({
      message: 'Sending the image is mandatory',
    });

  const pathFile = path || req.file?.path;

  try {
    await ensureStatusChat(req.client);
    const results: any = [];
    results.push(await req.client.sendImageStatus(pathFile));

    if (results.length === 0)
      res.status(400).json('Error sending the image of stories');
    returnSucess(res, results);
  } catch (error) {
    returnError(req, res, error);
  }
}

export async function sendVideoStorie(req: Request, res: Response) {
  /**
     #swagger.tags = ["Status Stories"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA'
     }
     #swagger.requestBody = {
      required: true,
      content: {
        "application/json": {
          schema: {
            type: "object",
            properties: {
              path: { type: "string" }
            },
            required: ["path"]
          },
          examples: {
            "Default": {
              value: {
                path: "Path of your video"
              }
            }
          }
        }
      }
    }
   */
  const { path } = req.body;

  if (!path && !req.file)
    res.status(401).send({
      message: 'Sending the Video is mandatory',
    });

  const pathFile = path || req.file?.path;

  try {
    await ensureStatusChat(req.client);
    const results: any = [];

    results.push(await req.client.sendVideoStatus(pathFile));

    if (results.length === 0) res.status(400).json('Error sending message');
    if (req.file) await unlinkAsync(pathFile);
    returnSucess(res, results);
  } catch (error) {
    returnError(req, res, error);
  }
}

/**
 * WinZapp patch: GET /api/:session/statuses — pull the user's own posted
 * statuses (and other contacts' statuses) straight from the account's
 * StatusV3Store, instead of relying solely on status@broadcast messages
 * arriving over the Socket.IO channel (which WhatsApp Web only emits once
 * the Status view has been opened in the browser, so the WinZapp Status tab
 * stayed empty on fresh sessions).
 */
export async function getStatuses(req: Request, res: Response) {
  /**
     #swagger.tags = ["Status Stories"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
   */
  try {
    const result = await req.client.page.evaluate(async () => {
      const WPP = (window as any).WPP;
      const out: any = { myStatus: [], contacts: [] };
      if (!WPP?.status) return out;

      const serialize = (m: any) => {
        try {
          if ((window as any).WAPI?._serializeRawObj) {
            return (window as any).WAPI._serializeRawObj(m);
          }
          return m?.toJSON ? m.toJSON() : m;
        } catch (e) {
          return null;
        }
      };

      const loadAllMessages = async (model: any) => {
        if (!model || typeof model.loadMore !== 'function') return;
        for (let i = 0; i < 40 && !model.noEarlierMsgs; i++) {
          const before = model.msgsLength ?? model.msgs?.length ?? 0;
          try {
            await model.loadMore();
          } catch (e) {
            break;
          }
          const after = model.msgsLength ?? model.msgs?.length ?? 0;
          if (after <= before && !model.isLoading) break;
        }
      };

      let ownStatusJid = '';

      // Own posted statuses, straight from the account.
      try {
        const my = await WPP.status.getMyStatus();
        ownStatusJid = my?.id?._serialized || my?.id?.toString?.() || '';
        await loadAllMessages(my);
        const msgs = my?.getAllMsgs ? my.getAllMsgs() : [];
        out.myStatus = (msgs || []).map(serialize).filter(Boolean);
      } catch (e) {
        // not paired/ready yet — leave myStatus empty
      }

      // Other contacts' statuses from the StatusV3 collection. The browser
      // keeps this store empty until the Status view is opened (or sync() is
      // called), so wake it first — that is the call WhatsApp Web itself
      // makes when the user opens the Status tab.
      try {
        const store = WPP?.whatsapp?.StatusV3Store;
        if (store?.sync) {
          try {
            await store.sync();
          } catch (e) {
            // ignore — read whatever is already in the store
          }
        }
        // Read ALL status models (unread, read/viewed, and muted statuses).
        // Try store.getModels(), store._models, store.models, and store.getUnexpired()
        let rawModels: any[] = [];
        if (typeof store?.getModels === 'function') {
          rawModels = store.getModels();
        } else if (Array.isArray(store?._models)) {
          rawModels = store._models;
        } else if (Array.isArray(store?.models)) {
          rawModels = store.models;
        } else if (typeof store?.getUnexpired === 'function') {
          rawModels = store.getUnexpired();
        }

        // Pagination belongs to each Status model, not to StatusV3Store.
        // Load earlier messages for every author before serializing the list.
        await Promise.all(rawModels.map(loadAllMessages));

        console.log(
          `[getStatuses] StatusV3Store rawModels count=${rawModels.length}`
        );

        const me = WPP.conn?.getMyUserWid?.()?.toString?.() ?? '';
        for (const model of rawModels) {
          try {
            const author = model?.id?._serialized || model?.id || '';
            if (!author || author === me || author === ownStatusJid) continue;
            const msgs = model?.getAllMsgs
              ? model.getAllMsgs()
              : model?.msgs?._models || model?.msgs || [];
            if (!msgs || !msgs.length) continue;
            out.contacts.push({
              jid: author,
              msgs: msgs.map(serialize).filter(Boolean),
            });
          } catch (e) {
            console.error('[getStatuses] error processing model:', e);
          }
        }
        console.log(
          `[getStatuses] Returning ${out.contacts.length} contact status entries`
        );
      } catch (e) {
        // Store not reachable — contacts stay empty
      }

      return out;
    });
    returnSucess(res, result);
  } catch (error) {
    returnError(req, res, error);
  }
}
