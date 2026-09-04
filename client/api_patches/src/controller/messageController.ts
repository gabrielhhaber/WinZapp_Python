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

import { Request, Response } from 'express';

import { unlinkAsync } from '../util/functions';

function returnError(req: Request, res: Response, error: any) {
  req.logger.error(error);
  // JSON.stringify(new Error(...)) serializes to `{}` — Error's own message/
  // stack properties aren't enumerable — so passing the raw Error object
  // here silently dropped the actual failure text. Own enumerable props a
  // custom Error subclass sets itself (e.g. wa-js's own error classes,
  // which explicitly assign `this.name`/`this.level`) still came through,
  // which is why every video-send 500 only ever showed
  // {"name":"t","level":"error"} in the response/log — the one property
  // that would have said *why* (message, inherited from Error.prototype)
  // was the one being dropped. Same fix already applied in this file's
  // deviceController.ts counterpart.
  const detail =
    error instanceof Error
      ? {
          ...error,
          name: error.name,
          message: error.message,
          stack: error.stack,
        }
      : error;
  res.status(500).json({
    status: 'Error',
    message: 'Erro ao enviar a mensagem.',
    error: detail,
  });
}

async function returnSucess(res: any, data: any) {
  res.status(201).json({ status: 'success', response: data, mapper: 'return' });
}

/** Reject the false-success shapes WPPConnect has returned after WA-JS/API
 * changes. ACK 0 is valid (queued locally); a missing id, a negative ACK, an
 * embedded error, or an explicit non-success send result is not. */
function assertSendAccepted(result: any, operation: string) {
  if (result === null || result === undefined || result === false) {
    throw new Error(`${operation} returned no send result`);
  }
  if (typeof result === 'string') {
    if (result.trim()) return result;
    throw new Error(`${operation} returned an empty message id`);
  }
  if (typeof result !== 'object') {
    throw new Error(`${operation} returned unsupported result type ${typeof result}`);
  }

  const embeddedError =
    result.error?.message || result.error || result.erro?.message || result.erro;
  if (embeddedError) {
    throw new Error(`${operation} failed: ${String(embeddedError)}`);
  }
  const ack = result.ack;
  if (ack !== undefined && ack !== null && Number(ack) < 0) {
    throw new Error(`${operation} was rejected (ack=${String(ack)})`);
  }
  const sendResult = result.sendMsgResult?.messageSendResult;
  if (
    sendResult !== undefined &&
    !['SUCCESS', 'OK'].includes(String(sendResult).toUpperCase())
  ) {
    throw new Error(`${operation} was rejected (${String(sendResult)})`);
  }

  const rawId = result.id ?? result.key?.id ?? result.messageId;
  const id =
    typeof rawId === 'string'
      ? rawId
      : rawId?._serialized || rawId?.toString?.() || '';
  if (!id || id === '[object Object]') {
    throw new Error(`${operation} returned success without a message id`);
  }
  return result;
}

async function watchMediaUpload(req: Request, uploadId: string, contact: string) {
  if (!uploadId) return;
  const client: any = req.client;
  const page = client.page;
  client.__winzappProgressIo = req.io;
  if (!client.__winzappProgressBridge) {
    await page.exposeFunction('__winzappMediaProgress', (payload: any) => {
      client.__winzappProgressIo?.emit('media-upload-progress', {
        ...payload,
        session: client.session,
      });
    });
    client.__winzappProgressBridge = true;
  }
  await page.evaluate(({ uploadId, contact }: any) => {
    const onMessage = (message: any) => {
      if (!message?.isSentByMe || message?.to?.toString?.() !== contact) return;
      const report = () => {
        const stage = message.mediaData?.progressiveStage;
        const numeric = Number(stage);
        if (Number.isFinite(numeric)) {
          (window as any).__winzappMediaProgress({
            uploadId,
            progress: numeric > 1 ? Math.min(numeric, 100) / 100 : numeric,
          });
        }
      };
      // WA-JS' own sendFileMessage() uses mediaStage as the upload
      // lifecycle signal.  progressiveStage still exists on MediaDataModel and
      // can carry a numeric fraction on some WhatsApp builds, so listen to both:
      // mediaStage guarantees that report() is re-run as the upload advances,
      // while progressiveStage preserves the real numeric value where available.
      message.on('change:mediaData.mediaStage', report);
      message.on('change:mediaData.progressiveStage', report);
      report();
      (window as any).WPP.whatsapp.MsgStore.off('add', onMessage);
    };
    (window as any).WPP.whatsapp.MsgStore.on('add', onMessage);
  }, { uploadId, contact });
}

export async function sendMessage(req: Request, res: Response) {
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
      required: true,
      "@content": {
        "application/json": {
          schema: {
            type: "object",
            properties: {
              phone: { type: "string" },
              isGroup: { type: "boolean" },
              isNewsletter: { type: "boolean" },
              isLid: { type: "boolean" },
              message: { type: "string" },
              options: { type: "object" },
            }
          },
          examples: {
            "Send message to contact": {
              value: { 
                phone: '5521999999999',
                isGroup: false,
                isNewsletter: false,
                isLid: false,
                message: 'Hi from WPPConnect',
              }
            },
            "Send message with reply": {
              value: { 
                phone: '5521999999999',
                isGroup: false,
                isNewsletter: false,
                isLid: false,
                message: 'Hi from WPPConnect with reply',
                options: {
                  quotedMsg: 'true_...@c.us_3EB01DE65ACC6_out',
                }
              }
            },
            "Send message to group": {
              value: {
                phone: '8865623215244578',
                isGroup: true,
                message: 'Hi from WPPConnect',
              }
            },
          }
        }
      }
     }
   */
  const { phone, message } = req.body;

  const options = req.body.options || {};

  try {
    const results: any = [];
    for (const contato of phone) {
      results.push(
        assertSendAccepted(
          await req.client.sendText(contato, message, options),
          'send-message'
        )
      );
    }

    if (results.length === 0) res.status(400).json('Error sending message');
    req.io.emit('mensagem-enviada', results);
    returnSucess(res, results);
  } catch (error) {
    returnError(req, res, error);
  }
}

export async function editMessage(req: Request, res: Response) {
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
      required: true,
      "@content": {
        "application/json": {
          schema: {
            type: "object",
            properties: {
              id: { type: "string" },
              newText: { type: "string" },
              options: { type: "object" },
            }
          },
          examples: {
            "Edit a message": {
              value: { 
                id: 'true_5521999999999@c.us_3EB04FCAA1527EB6D9DEC8',
                newText: 'New text for message'
              }
            },
          }
        }
      }
     }
   */
  const { id, newText } = req.body;

  const options = req.body.options || {};
  try {
    const edited = await (req.client as any).editMessage(id, newText, options);

    req.io.emit('edited-message', edited);
    returnSucess(res, edited);
  } catch (error) {
    returnError(req, res, error);
  }
}

export async function sendFile(req: Request, res: Response) {
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
      required: true,
      "@content": {
        "application/json": {
            schema: {
                type: "object",
                properties: {
                    "phone": { type: "string" },
                    "isGroup": { type: "boolean" },
                    "isNewsletter": { type: "boolean" },
                    "isLid": { type: "boolean" },
                    "filename": { type: "string" },
                    "caption": { type: "string" },
                    "base64": { type: "string" }
                }
            },
            examples: {
                "Default": {
                    value: {
                        "phone": "5521999999999",
                        "isGroup": false,
                        "isNewsletter": false,
                        "isLid": false,
                        "filename": "file name lol",
                        "caption": "caption for my file",
                        "base64": "<base64> string"
                    }
                }
            }
        }
      }
    }
   */
  const {
    phone,
    path,
    base64,
    filename = 'file',
    message,
    caption,
    quotedMessageId,
    type,
    uploadId,
  } = req.body;

  const options = req.body.options || {};

  if (!path && !req.file && !base64)
    res.status(401).send({
      message: 'Sending the file is mandatory',
    });

  const pathFile = path || base64 || req.file?.path;
  const msg = message || caption;

  try {
    const results: any = [];
    for (const contact of phone) {
      await watchMediaUpload(req, uploadId, contact);
      results.push(
        assertSendAccepted(
          await req.client.sendFile(contact, pathFile, {
          filename: filename,
          caption: msg,
          quotedMsg: quotedMessageId,
          // Without this, sendFile() defaults to type: 'auto-detect', which
          // picks the WhatsApp message kind from the file's mimetype — so an
          // .mp3/.jpg sent via the "Document" attachment option uploaded as
          // a playable audio/photo message instead of a document, unlike
          // the official client (which only auto-detects for the Photos &
          // Videos/Audio menu options, and always uploads as a document
          // otherwise). Respect the type WinZapp explicitly requested.
          type: type || 'auto-detect',
          // The bounded/chunked transfer sender.layer.js's patched sendFile()
          // uses for large uploads (see
          // client/core/wppconnect_sender_layer_patch.py) rebuilds the file
          // entirely from raw bytes in the browser — it never sees multer's
          // own req.file.mimetype, only options.mimetype, which nothing set
          // before this. Without it the reconstructed File always fell back
          // to 'application/octet-stream', so a large video/audio/image
          // routed through that path arrived with the wrong content type
          // (still tagged the right WhatsApp message TYPE via options.type
          // above, but not necessarily playable/previewable as one on the
          // receiving end). multer already knows the real one from the
          // multipart upload's own Content-Type.
          mimetype: req.file?.mimetype,
          ...options,
          }),
          'send-file'
        )
      );
    }

    if (results.length === 0) res.status(400).json('Error sending message');
    returnSucess(res, results);
  } catch (error) {
    returnError(req, res, error);
  } finally {
    if (req.file) {
      await unlinkAsync(pathFile).catch(() => {});
    }
  }
}

export async function sendVoice(req: Request, res: Response) {
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
        required: true,
        "@content": {
            "application/json": {
                schema: {
                    type: "object",
                    properties: {
                        "phone": { type: "string" },
                        "isGroup": { type: "boolean" },
                        "path": { type: "string" },
                        "quotedMessageId": { type: "string" }
                    }
                },
                examples: {
                    "Default": {
                        value: {
                            "phone": "5521999999999",
                            "isGroup": false,
                            "path": "<path_file>",
                            "quotedMessageId": "message Id"
                        }
                    }
                }
            }
        }
    }
   */
  const {
    phone,
    path,
    filename = 'Voice Audio',
    message,
    quotedMessageId,
  } = req.body;

  try {
    const results: any = [];
    for (const contato of phone) {
      results.push(
        assertSendAccepted(
          await req.client.sendPtt(
          contato,
          path,
          filename,
          message,
          quotedMessageId
          ),
          'send-voice'
        )
      );
    }

    if (results.length === 0) res.status(400).json('Error sending message');
    returnSucess(res, results);
  } catch (error) {
    returnError(req, res, error);
  }
}

export async function sendVoice64(req: Request, res: Response) {
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
        required: true,
        "@content": {
            "application/json": {
                schema: {
                    type: "object",
                    properties: {
                        "phone": { type: "string" },
                        "isGroup": { type: "boolean" },
                        "base64Ptt": { type: "string" }
                    }
                },
                examples: {
                    "Default": {
                        value: {
                            "phone": "5521999999999",
                            "isGroup": false,
                            "base64Ptt": "<base64_string>"
                        }
                    }
                }
            }
        }
    }
   */
  const { phone, base64Ptt, quotedMessageId } = req.body;

  try {
    const results: any = [];
    const page = (req.client as any).page;

    // Inject the base64 payload and quotedMsg directly into the page context
    // as a global variable to avoid serialising ~100 KB through the CDP IPC
    // channel as a JSON argument on every page.evaluate() call.
    const tempVar = `__wz_ptt_${Date.now()}`;
    await page.evaluate(
      ({
        varName,
        base64,
        quotedMsg,
      }: {
        varName: string;
        base64: string;
        quotedMsg: string | undefined;
      }) => {
        (window as any)[varName] = { base64, quotedMsg };
      },
      { varName: tempVar, base64: base64Ptt, quotedMsg: quotedMessageId }
    );

    for (const contato of phone) {
      results.push(
        assertSendAccepted(
        // Use evaluateAndReturn directly so we can set waitForAck: false.
        // sendPttFromBase64 has waitForAck hardcoded to true, which blocks
        // the HTTP response until WhatsApp Web receives the upload ACK —
        // causing 1–10 s of "pending" delay visible to the user.
        // The payload is read from a page-side global (set above) so only
        // a short string (contact JID + var name) crosses the CDP channel here.
        await page.evaluate(
          ({ to, varName }: { to: string; varName: string }) => {
            try {
              const { base64, quotedMsg } = (window as any)[varName] as {
                base64: string;
                quotedMsg: string | undefined;
              };
              return (window as any).WPP.chat
                .sendFileMessage(to, base64, {
                  type: 'audio',
                  isPtt: true,
                  filename: 'Voice Audio',
                  caption: '',
                  quotedMsg,
                  waitForAck: false,
                })
                .then((result: any) => ({
                  id: result?.id?.toString?.() ?? null,
                  ack: result?.ack ?? 0,
                }))
                .catch((err: any) => ({
                  error: err?.message || String(err),
                  ack: -1,
                }));
            } catch (err: any) {
              return Promise.resolve({
                error: err?.message || String(err),
                ack: -1,
              });
            }
          },
          { to: contato, varName: tempVar }
        ),
        'send-voice-base64'
        )
      );
    }

    // Clean up the temporary global so it doesn't linger in the page context.
    await page.evaluate(
      ({ varName }: { varName: string }) => {
        delete (window as any)[varName];
      },
      { varName: tempVar }
    );

    if (results.length === 0) res.status(400).json('Error sending message');
    returnSucess(res, results);
  } catch (error) {
    returnError(req, res, error);
  }
}

export async function sendLinkPreview(req: Request, res: Response) {
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
        required: true,
        "@content": {
            "application/json": {
                schema: {
                    type: "object",
                    properties: {
                        "phone": { type: "string" },
                        "isGroup": { type: "boolean" },
                        "url": { type: "string" },
                        "caption": { type: "string" }
                    }
                },
                examples: {
                    "Default": {
                        value: {
                            "phone": "5521999999999",
                            "isGroup": false,
                            "url": "http://www.link.com",
                            "caption": "Text for describe link"
                        }
                    }
                }
            }
        }
    }
   */
  const { phone, url, caption } = req.body;

  try {
    const results: any = [];
    for (const contato of phone) {
      results.push(
        await req.client.sendLinkPreview(`${contato}`, url, caption)
      );
    }

    if (results.length === 0) res.status(400).json('Error sending message');
    returnSucess(res, results);
  } catch (error) {
    returnError(req, res, error);
  }
}

export async function sendLocation(req: Request, res: Response) {
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
        required: true,
        "@content": {
            "application/json": {
                schema: {
                    type: "object",
                    properties: {
                        "phone": { type: "string" },
                        "isGroup": { type: "boolean" },
                        "lat": { type: "string" },
                        "lng": { type: "string" },
                        "title": { type: "string" },
                        "address": { type: "string" }
                    }
                },
                examples: {
                    "Default": {
                        value: {
                            "phone": "5521999999999",
                            "isGroup": false,
                            "lat": "-89898322",
                            "lng": "-545454",
                            "title": "Rio de Janeiro",
                            "address": "Av. N. S. de Copacabana, 25, Copacabana"
                        }
                    }
                }
            }
        }
    }
   */
  const { phone, lat, lng, title, address } = req.body;

  try {
    const results: any = [];
    for (const contato of phone) {
      results.push(
        await req.client.sendLocation(contato, {
          lat: lat,
          lng: lng,
          address: address,
          name: title,
        })
      );
    }

    if (results.length === 0) res.status(400).json('Error sending message');
    returnSucess(res, results);
  } catch (error) {
    returnError(req, res, error);
  }
}

export async function sendButtons(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Messages"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA',
     }
     #swagger.deprecated=true
   */
  const { phone, message, options } = req.body;

  try {
    const results: any = [];

    for (const contact of phone) {
      results.push(await req.client.sendText(contact, message, options));
    }

    if (results.length === 0)
      return returnError(req, res, 'Error sending message with buttons');

    returnSucess(res, phone);
  } catch (error) {
    returnError(req, res, error);
  }
}

export async function sendListMessage(req: Request, res: Response) {
  /**
   * #swagger.tags = ["Messages"]
     #swagger.autoBody=false
     #swagger.security = [{
            "bearerAuth": []
     }]
     #swagger.parameters["session"] = {
      schema: 'NERDWHATS_AMERICA',
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
              description: { type: "string" },
              sections: { type: "array" },
              buttonText: { type: "string" },
            }
          },
          examples: {
            "Send list message": {
              value: { 
                phone: '5521999999999',
                isGroup: false,
                description: 'Desc for list',
                buttonText: 'Select a option',
                sections: [
                  {
                    title: 'Section 1',
                    rows: [
                      {
                        rowId: 'my_custom_id',
                        title: 'Test 1',
                        description: 'Description 1',
                      },
                      {
                        rowId: '2',
                        title: 'Test 2',
                        description: 'Description 2',
                      },
                    ],
                  },
                ],
              }
            },
          }
        }
      }
     }
   */
  const {
    phone,
    description = '',
    sections,
    buttonText = 'SELECIONE UMA OPÇÃO',
  } = req.body;

  try {
    const results: any = [];

    for (const contact of phone) {
      results.push(
        await req.client.sendListMessage(contact, {
          buttonText: buttonText,
          description: description,
          sections: sections,
        })
      );
    }

    if (results.length === 0)
      return returnError(req, res, 'Error sending list buttons');

    returnSucess(res, results);
  } catch (error) {
    returnError(req, res, error);
  }
}

export async function sendOrderMessage(req: Request, res: Response) {
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
      required: true,
      "@content": {
        "application/json": {
          schema: {
            type: "object",
            properties: {
              phone: { type: "string" },
              isGroup: { type: "boolean" },
              items: { type: "object" },
              options: { type: "object" },
            }
          },
          examples: {
            "Send with custom items": {
              value: { 
                phone: '5521999999999',
                isGroup: false,
                items: [
                  {
                    type: 'custom',
                    name: 'Item test',
                    price: 120000,
                    qnt: 2,
                  },
                  {
                    type: 'custom',
                    name: 'Item test 2',
                    price: 145000,
                    qnt: 2,
                  },
                ],
              }
            },
            "Send with product items": {
              value: { 
                phone: '5521999999999',
                isGroup: false,
                items: [
                  {
                    type: 'product',
                    id: '37878774457',
                    price: 148000,
                    qnt: 2,
                  },
                ],
              }
            },
            "Send with custom items and options": {
              value: { 
                phone: '5521999999999',
                isGroup: false,
                items: [
                  {
                    type: 'custom',
                    name: 'Item test',
                    price: 120000,
                    qnt: 2,
                  },
                ],
                options: {
                  tax: 10000,
                  shipping: 4000,
                  discount: 10000,
                }
              }
            },
          }
        }
      }
     }
   */
  const { phone, items } = req.body;

  const options = req.body.options || {};

  try {
    const results: any = [];
    for (const contato of phone) {
      results.push(await req.client.sendOrderMessage(contato, items, options));
    }

    if (results.length === 0)
      res.status(400).json('Error sending order message');
    req.io.emit('mensagem-enviada', results);
    returnSucess(res, results);
  } catch (error) {
    returnError(req, res, error);
  }
}

export async function sendPollMessage(req: Request, res: Response) {
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
        required: true,
        "@content": {
            "application/json": {
                schema: {
                    type: "object",
                    properties: {
                        phone: { type: "string" },
                        isGroup: { type: "boolean" },
                        name: { type: "string" },
                        choices: { type: "array" },
                        options: { type: "object" },
                    }
                },
                examples: {
                    "Default": {
                        value: {
                          phone: '5521999999999',
                          isGroup: false,
                          name: 'Poll name',
                          choices: ['Option 1', 'Option 2', 'Option 3'],
                          options: {
                            selectableCount: 1,
                          }
                        }
                    },
                }
            }
        }
    }
   */
  const { phone, name, choices, options } = req.body;

  try {
    const results: any = [];

    for (const contact of phone) {
      results.push(
        await req.client.sendPollMessage(contact, name, choices, options)
      );
    }

    if (results.length === 0)
      return returnError(req, res, 'Error sending poll message');

    returnSucess(res, results);
  } catch (error) {
    returnError(req, res, error);
  }
}

export async function sendStatusText(req: Request, res: Response) {
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
      required: true,
      content: {
        'application/json': {
          schema: {
            type: 'object',
            properties: {
              phone: { type: 'string' },
              isGroup: { type: 'boolean' },
              message: { type: 'string' },
              messageId: { type: 'string' }
            },
            required: ['phone', 'isGroup', 'message']
          },
          examples: {
            Default: {
              value: {
                phone: '5521999999999',
                isGroup: false,
                message: 'Reply to message',
                messageId: '<id_message>'
              }
            }
          }
        }
      }
    }
   */
  const { message } = req.body;

  try {
    const results: any = [];
    results.push(await req.client.sendText('status@broadcast', message));

    if (results.length === 0) res.status(400).json('Error sending message');
    returnSucess(res, results);
  } catch (error) {
    returnError(req, res, error);
  }
}

export async function sendStatusVoice64(req: Request, res: Response) {
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
              base64Ptt: { type: "string" },
            }
          },
          examples: {
            "Default": {
              value: {
                base64Ptt: "<base64_string>",
              }
            },
          }
        }
      }
     }
   */
  // WinZapp-added: post a voice-note status. There is no dedicated
  // WPP.status.sendVoiceStatus()/sendAudioStatus() in wa-js/wppconnect
  // (status.layer.js only wraps sendTextStatus/sendImageStatus/
  // sendVideoStatus) and sendPtt()/sendPttFromBase64() (used for a normal
  // 1-on-1 voice message, see sendVoice64() above) both target an arbitrary
  // chat id via the same generic WPP.chat.sendFileMessage(to, base64, {
  // type: 'audio', isPtt: true, ...}) call — this reuses that exact
  // primitive, targeting 'status@broadcast' the same way sendStatusText()
  // above targets it via req.client.sendText(). Mirrors sendVoice64()'s own
  // page-context-injection technique (avoids serialising the full base64
  // payload through the CDP IPC channel as a plain JSON argument).
  const { base64Ptt } = req.body;

  if (!base64Ptt)
    return res.status(401).send({
      message: 'base64Ptt is mandatory',
    });

  try {
    const page = (req.client as any).page;
    const tempVar = `__wz_status_ptt_${Date.now()}`;
    await page.evaluate(
      ({ varName, base64 }: { varName: string; base64: string }) => {
        (window as any)[varName] = base64;
      },
      { varName: tempVar, base64: base64Ptt }
    );

    const result = await page.evaluate(
      ({ varName }: { varName: string }) => {
        try {
          const base64 = (window as any)[varName] as string;
          return (window as any).WPP.chat
            .sendFileMessage('status@broadcast', base64, {
              type: 'audio',
              isPtt: true,
              filename: 'Voice Status',
              waitForAck: true,
            })
            .then((r: any) => ({
              ok: true,
              id: r?.id?.toString?.() ?? null,
              ack: r?.ack ?? 0,
            }))
            .catch((err: any) => ({
              ok: false,
              error: err?.message || String(err),
            }));
        } catch (err: any) {
          return Promise.resolve({
            ok: false,
            error: err?.message || String(err),
          });
        }
      },
      { varName: tempVar }
    );

    await page.evaluate(
      ({ varName }: { varName: string }) => {
        delete (window as any)[varName];
      },
      { varName: tempVar }
    );

    if (!result || !result.ok) {
      return res.status(500).json({
        status: 'error',
        message: (result && result.error) || 'Error posting voice status',
      });
    }

    returnSucess(res, result);
  } catch (error) {
    returnError(req, res, error);
  }
}

/**
 * Parse a JSON string handed back from a page.evaluate() call, throwing a
 * descriptive error (including `label`) on malformed input instead of a
 * bare JSON.parse SyntaxError.
 */
function parseEvaluateJson(raw: string | null | undefined, label: string): any {
  try {
    return typeof raw === 'string' ? JSON.parse(raw) : null;
  } catch (parseError) {
    throw new Error(`Invalid ${label}: ${String(parseError)}`);
  }
}

/**
 * WinZapp patch: reply to a contact's status (WhatsApp "story") with a real
 * quote back to the original status, instead of a plain DM.
 *
 * A contact's status is not indexed in the ordinary MsgStore used by
 * client.reply()/getMessageById().  WhatsApp Web keeps it in that contact's
 * StatusV3Model, so resolve the real MsgModel there and serialize that model
 * as WA-JS's supported quotedMsgPayload.  Current WhatsApp Web models from
 * StatusV3Store incorrectly expose isStatusV3=false; passing the live model
 * as quotedMsg therefore hits WA-JS's ordinary-chat canReply guard.
 * Rehydrated payloads bypass that stale flag and still use
 * MsgModel.msgContextInfo(), which emits the correct status stanza,
 * participant and status@broadcast JID.
 */
async function replyToStatusMessage(
  req: Request,
  contato: string,
  message: string,
  serializedId: string
): Promise<any> {
  const probeKey = `winzapp_status_reply_${Date.now()}_${Math.random()
    .toString(36)
    .slice(2)}`;
  const directJson = await req.client.page.evaluate(
    async ({ to, content, serializedId, probeKey }) => {
      const pageWindow = window as any;
      const WPP = pageWindow.WPP;
      const parts = serializedId.split('_');
      const rawId = parts.length > 2 ? parts[2] : serializedId;
      const posterJid = parts.length > 3 ? parts[3] : '';
      const store = WPP?.whatsapp?.StatusV3Store;
      const probes = (pageWindow.__winzappStatusReplyProbes ||= {});
      const probe: any = (probes[probeKey] = {
        phase: 'started',
        statusId: rawId,
        posterJid,
        destination: to,
      });

      try {
        // Prefer the expected poster, but also scan StatusV3Store.  The
        // Python side deliberately prefers @lid for sends while the
        // status collection can still be keyed by the legacy @c.us JID;
        // limiting lookup to WPP.status.get(posterJid) would therefore
        // miss a status that is visibly present in the panel.
        const statusModels: any[] = [];
        const expected = posterJid
          ? WPP?.status?.get?.(posterJid)
          : undefined;
        if (expected) statusModels.push(expected);
        const storedModels =
          (typeof store?.getModels === 'function' && store.getModels()) ||
          (Array.isArray(store?._models) && store._models) ||
          (Array.isArray(store?.models) && store.models) ||
          [];
        for (const model of storedModels) {
          if (model && !statusModels.includes(model)) {
            statusModels.push(model);
          }
        }

        let quoted: any = null;
        let matchedPoster = '';
        let messagesSeen = 0;
        for (const model of statusModels) {
          const msgs =
            (typeof model?.getAllMsgs === 'function' &&
              model.getAllMsgs()) ||
            (typeof model?.msgs?.getModelsArray === 'function' &&
              model.msgs.getModelsArray()) ||
            [];
          messagesSeen += msgs.length;
          quoted = msgs.find((item: any) => {
            const itemId = item?.id?.id || '';
            const itemSerialized = item?.id?._serialized || '';
            return itemId === rawId || itemSerialized === serializedId;
          });
          if (quoted) {
            matchedPoster =
              model?.id?._serialized || model?.id?.toString?.() || '';
            break;
          }
        }
        if (!quoted) {
          throw new Error(
            `Status message not found for reply: id=${rawId}, ` +
              `poster=${posterJid || '<missing>'}, ` +
              `models=${statusModels.length}, messages=${messagesSeen}`
          );
        }
        Object.assign(probe, {
          phase: 'status-found',
          statusModelsSeen: statusModels.length,
          statusMessagesSeen: messagesSeen,
          matchedPoster,
          quotedIsStatusV3: Boolean(quoted.isStatusV3),
        });
        const quotedPayload = JSON.stringify(
          typeof quoted.toJSON === 'function' ? quoted.toJSON() : quoted
        );
        const sendResult = await WPP.chat.sendTextMessage(to, content, {
          quotedMsgPayload: quotedPayload,
          linkPreview: false,
          waitForAck: true,
        });
        const sentId =
          typeof sendResult?.id === 'string'
            ? sendResult.id
            : sendResult?.id?.toString?.() || '';
        const ack = Number(sendResult?.ack ?? 0);
        const messageSendResult = String(
          sendResult?.sendMsgResult?.messageSendResult ?? ''
        );
        const sendError = String(
          sendResult?.sendMsgResult?.error ??
            sendResult?.sendMsgResult?.errorCode ??
            ''
        );
        Object.assign(probe, {
          phase: 'send-returned',
          id: sentId,
          ack,
          messageSendResult,
          error: sendError,
        });
        if (!sentId) {
          throw new Error(
            `Status reply returned no message id: status=${rawId}, ` +
              `poster=${matchedPoster || posterJid}, ` +
              `result=${JSON.stringify(sendResult)}`
          );
        }
        if (!['SUCCESS', 'OK'].includes(messageSendResult) || ack < 1) {
          throw new Error(
            `Status reply rejected by WhatsApp: status=${rawId}, ` +
              `result=${messageSendResult || '<missing>'}, ack=${ack}, ` +
              `error=${sendError || '<missing>'}`
          );
        }
        probe.phase = 'validated';
      } catch (error) {
        probe.phase = 'error';
        probe.error = String((error as any)?.message || error);
        probe.stack = String((error as any)?.stack || '');
      }

      // The return value after sendTextMessage is lost by this
      // WPPConnect/Puppeteer combination. Keep returning it for versions
      // where it works, but the Node side also reads the probe in a
      // separate evaluate call below when it is.
      //
      // The common case (this return value actually arrives) cleans up
      // after itself right here — the fallback evaluate below is the only
      // one that still needs to read AND delete the stored probe.
      delete probes[probeKey];
      return JSON.stringify(probe);
    },
    {
      to: contato,
      content: message,
      serializedId,
      probeKey,
    }
  );
  // Only pay for a second Puppeteer round-trip when the first evaluate's
  // return value was actually lost (see the comment inside it above) — the
  // common case already has everything it needs in directJson, and already
  // cleaned up its own probe entry.
  const probeJson =
    typeof directJson === 'string'
      ? null
      : await req.client.page.evaluate(
          ({ probeKey }) => {
            const pageWindow = window as any;
            const probes = pageWindow.__winzappStatusReplyProbes;
            const probe = probes?.[probeKey] ?? null;
            if (probes) delete probes[probeKey];
            return JSON.stringify(probe);
          },
          { probeKey }
        );
  const sentJson = typeof directJson === 'string' ? directJson : probeJson;
  const sent = parseEvaluateJson(sentJson, 'serialized status reply result');
  if (
    !sent ||
    sent.phase !== 'validated' ||
    typeof sent.id !== 'string' ||
    !sent.id ||
    Number(sent.ack) < 1 ||
    !['SUCCESS', 'OK'].includes(String(sent.messageSendResult))
  ) {
    throw new Error(
      `Invalid status reply result returned by WhatsApp Web: ${JSON.stringify(
        sent
      )}`
    );
  }
  const storedJson = await req.client.page.evaluate(
    async ({ messageId }) => {
      // MsgStore may not have indexed the just-sent message yet the instant
      // sendTextMessage() resolves — retry briefly before giving up, rather
      // than reporting an already-confirmed send (ack>=1, SUCCESS/OK, just
      // validated above) as a failure.
      let stored: any = null;
      for (let attempt = 0; attempt < 3 && !stored; attempt++) {
        if (attempt > 0) {
          await new Promise((resolve) => setTimeout(resolve, 300));
        }
        stored = await (window as any).WAPI?.getMessageById?.(messageId);
      }
      return JSON.stringify(stored ?? null);
    },
    { messageId: sent.id }
  );
  const stored = parseEvaluateJson(storedJson, 'stored status reply result');
  if (!stored || stored.erro === true || stored.error === true) {
    throw new Error(
      `Status reply was acknowledged but not found in MsgStore: ${JSON.stringify(
        stored
      )}`
    );
  }
  req.logger.info(
    `[status-reply] sent ${serializedId}: ` + JSON.stringify(sent)
  );
  return sent;
}

export async function replyMessage(req: Request, res: Response) {
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
      required: true,
      "@content": {
        "application/json": {
          schema: {
            type: "object",
            properties: {
              "phone": { type: "string" },
              "isGroup": { type: "boolean" },
              "message": { type: "string" },
              "messageId": { type: "string" }
            }
          },
          examples: {
            "Default": {
              value: {
                "phone": "5521999999999",
                "isGroup": false,
                "message": "Reply to message",
                "messageId": "<id_message>"
              }
            }
          }
        }
      }
    }
   */
  const { phone, message, messageId } = req.body;

  try {
    const results: any = [];
    for (const contato of phone) {
      if (
        typeof messageId === 'string' &&
        messageId.includes('status@broadcast')
      ) {
        results.push(
          await replyToStatusMessage(req, contato, message, messageId)
        );
      } else {
        results.push(
          assertSendAccepted(
            await req.client.reply(contato, message, messageId),
            'send-reply'
          )
        );
      }
    }

    if (results.length === 0) res.status(400).json('Error sending message');
    req.io.emit('mensagem-enviada', { message: message, to: phone });
    returnSucess(res, results);
  } catch (error) {
    returnError(req, res, error);
  }
}

export async function sendMentioned(req: Request, res: Response) {
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
  required: true,
  "@content": {
    "application/json": {
      schema: {
        type: "object",
        properties: {
          "phone": { type: "string" },
          "isGroup": { type: "boolean" },
          "message": { type: "string" },
          "mentioned": { type: "array", items: { type: "string" } }
        },
        required: ["phone", "message", "mentioned"]
      },
      examples: {
        "Default": {
          value: {
            "phone": "groupId@g.us",
            "isGroup": true,
            "message": "Your text message",
            "mentioned": ["556593077171@c.us"]
          }
        }
      }
    }
  }
}
   */
  const { phone, message, mentioned } = req.body;

  try {
    let response;
    for (const contato of phone) {
      response = assertSendAccepted(
        await req.client.sendMentioned(`${contato}`, message, mentioned),
        'send-mentioned'
      );
    }

    res.status(201).json({ status: 'success', response: response });
  } catch (error) {
    req.logger.error(error);
    res.status(500).json({
      status: 'error',
      message: 'Error on send message mentioned',
      error: error,
    });
  }
}
export async function sendImageAsSticker(req: Request, res: Response) {
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
      required: true,
      "@content": {
        "application/json": {
          schema: {
            type: "object",
            properties: {
              "phone": { type: "string" },
              "isGroup": { type: "boolean" },
              "path": { type: "string" }
            },
            required: ["phone", "path"]
          },
          examples: {
            "Default": {
              value: {
                "phone": "5521999999999",
                "isGroup": true,
                "path": "<path_file>"
              }
            }
          }
        }
      }
    }
   */
  const { phone, path } = req.body;

  if (!path && !req.file)
    res.status(401).send({
      message: 'Sending the file is mandatory',
    });

  const pathFile = path || req.file?.path;

  try {
    const results: any = [];
    for (const contato of phone) {
      results.push(await req.client.sendImageAsSticker(contato, pathFile));
    }

    if (results.length === 0) res.status(400).json('Error sending message');
    returnSucess(res, results);
  } catch (error) {
    returnError(req, res, error);
  } finally {
    // Moved out of the try body's happy path: when the send call itself threw,
    // the old code never reached the unlinkAsync() call at all, leaking the
    // multer-uploaded temp file on every failed send — worse under load,
    // since repeated failures never got cleaned up.
    if (req.file) {
      await unlinkAsync(pathFile).catch(() => {});
    }
  }
}
export async function sendImageAsStickerGif(req: Request, res: Response) {
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
      required: true,
      content: {
        'application/json': {
          schema: {
            type: 'object',
            properties: {
              phone: { type: 'string' },
              isGroup: { type: 'boolean' },
              path: { type: 'string' },
            },
            required: ['phone', 'path'],
          },
          examples: {
            'Default': {
              value: {
                phone: '5521999999999',
                isGroup: true,
                path: '<path_file>',
              },
            },
          },
        },
      },
    }
   */
  const { phone, path } = req.body;

  if (!path && !req.file)
    res.status(401).send({
      message: 'Sending the file is mandatory',
    });

  const pathFile = path || req.file?.path;

  try {
    const results: any = [];
    for (const contato of phone) {
      results.push(await req.client.sendImageAsStickerGif(contato, pathFile));
    }

    if (results.length === 0) res.status(400).json('Error sending message');
    returnSucess(res, results);
  } catch (error) {
    returnError(req, res, error);
  } finally {
    // Moved out of the try body's happy path: when the send call itself threw,
    // the old code never reached the unlinkAsync() call at all, leaking the
    // multer-uploaded temp file on every failed send — worse under load,
    // since repeated failures never got cleaned up.
    if (req.file) {
      await unlinkAsync(pathFile).catch(() => {});
    }
  }
}

export async function sendPixMessage(req: Request, res: Response) {
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
      required: true,
      "@content": {
        "application/json": {
          schema: {
            type: "object",
            required: ["phone", "keyType", "name", "key"],
            properties: {
              phone: { type: "string" },
              keyType: { type: "string" },
              name: { type: "string" },
              key: { type: "string" },
              instructions: { type: "string" },
              options: { type: "object" },
            }
          },
          examples: {
            "Send PIX key to contact": {
              value: { 
                phone: "5521999999999",
                keyType: "PHONE",
                name: "WPPCONNECT-TEAM",
                key: "+5567123456789",
                instructions: "some instructions about the pix",
                options: {}
              }
            },
          }
        }
      }
     }
   */

  const { phone, keyType, name, key, instructions } = req.body;

  const options = req.body.options || {};

  try {
    const results: any = [];
    for (const contato of phone) {
      results.push(
        await req.client.sendPixKey(
          contato,
          { keyType, name, key, instructions },
          options
        )
      );
    }

    if (results.length === 0) res.status(400).json('Error sending message');
    req.io.emit('mensagem-enviada', results);
    returnSucess(res, results);
  } catch (error) {
    returnError(req, res, error);
  }
}

// WinZapp patch: pin/unpin an individual message in a chat.
//
// @wppconnect-team/wppconnect's controls.layer.ts never wraps this — it only
// exposes WPP.chat.pin()/pinChat() for pinning a whole CHAT (see
// deviceController.pinChat), not WPP.chat.pinMsg()/unpinMsg() for pinning
// one MESSAGE within it. Both exist in the underlying wa-js bundle
// (window.WPP.chat.pinMsg(msgId, pin)), so this calls it directly through
// page.evaluate() the same way the patched subscribePresence() does in
// sessionController.ts, rather than waiting on an upstream wrapper.
export async function pinMessage(req: Request, res: Response) {
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
              pin: { type: "boolean" },
            }
          },
          examples: {
            "Default": {
              value: {
                messageId: "true_5521999999999@c.us_3EB0...",
                pin: true,
              }
            },
          }
        }
      }
     }
   */
  const { messageId, pin = true } = req.body;
  const page = (req.client as any)?.page;

  if (!page || page.isClosed()) {
    return res.status(400).json({
      status: 'error',
      message: 'The WhatsApp session is not active.',
    });
  }

  try {
    const result = await page.evaluate(
      async ({ messageId, pin }: { messageId: string; pin: boolean }) => {
        try {
          const wpp = (window as any).WPP;
          const r = await wpp.chat.pinMsg(messageId, pin);
          return { ok: true, pinned: r?.pinned ?? pin };
        } catch (err: any) {
          return { ok: false, error: err?.message || String(err) };
        }
      },
      { messageId, pin }
    );

    if (!result || !result.ok) {
      return res.status(500).json({
        status: 'error',
        message: (result && result.error) || 'Error on pin message',
      });
    }

    res.status(200).json({
      status: 'success',
      response: { messageId, pinned: result.pinned },
    });
  } catch (error) {
    req.logger.error(error);
    res.status(500).json({
      status: 'error',
      message: 'Error on pin message',
      error,
    });
  }
}

export async function markPlayed(req: Request, res: Response) {
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
            }
          },
          examples: {
            "Default": {
              value: {
                messageId: "false_5521999999999@c.us_3EB0...",
              }
            },
          }
        }
      }
     }
   */
  // WinZapp-added: mark a received voice message as actually played, so
  // the sender's own client shows the played indicator — the same
  // WPP.chat.markPlayed() WhatsApp Web's own UI calls when a voice note is
  // listened to there. No equivalent route existed anywhere in WPPConnect
  // Server; client.markPlayed() (sender.layer.js) is a thin, unguarded
  // page.evaluate() with no try/catch of its own, so this wraps the call
  // itself here — same pattern as pinMessage() above — instead of calling
  // it through that wrapper directly.
  const { messageId } = req.body;
  const page = (req.client as any)?.page;

  if (!messageId) {
    return res.status(400).json({
      status: 'error',
      message: 'messageId is required',
    });
  }
  if (!page || page.isClosed()) {
    return res.status(400).json({
      status: 'error',
      message: 'The WhatsApp session is not active.',
    });
  }

  try {
    const result = await page.evaluate(
      async ({ messageId }: { messageId: string }) => {
        try {
          const wpp = (window as any).WPP;
          await wpp.chat.markPlayed(messageId);
          return { ok: true };
        } catch (err: any) {
          return { ok: false, error: err?.message || String(err) };
        }
      },
      { messageId }
    );

    if (!result || !result.ok) {
      return res.status(500).json({
        status: 'error',
        message: (result && result.error) || 'Error on mark played',
      });
    }

    res.status(200).json({ status: 'success', response: { messageId } });
  } catch (error) {
    req.logger.error(error);
    res.status(500).json({
      status: 'error',
      message: 'Error on mark played',
      error,
    });
  }
}
