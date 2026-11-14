import type { Request, Response } from 'express';
import { createEvent, editEvent, readEventResponses, writeEventResponse } from '../util/eventsRuntime';

// WhatsApp events (util/eventsRuntime.ts). The Neonize bridge answers the same
// routes with the same bodies (neonize_bridge/events.py); the client reads
// them in client/core/events.py. Errors are static codes: a native error can
// carry phone numbers and the event's text.

const MESSAGE_ID = /^(true|false)_[\w.:-]+@[\w.]+_[\w]+(_[\w.:-]+@[\w.]+)?$/;
const CHAT_ID = /^[\w.:-]+@(c\.us|s\.whatsapp\.net|lid|g\.us)$/;
const MAX_NAME = 256;
const MAX_TEXT = 2048;

function pageOf(req: Request) {
  const page = (req.client as any)?.page;
  return page && !page.isClosed() ? page : undefined;
}

function fail(res: Response, outcome: any, fallback: string) {
  const code = outcome?.error || fallback;
  const status = /invalid|not_mine|canceled|not_found/.test(code) ? 400 : 503;
  return res.status(status).json({ status: 'error', code });
}

/** The event fields of a body, or null when one is malformed. */
export function eventFields(body: any, needName = true) {
  const out: any = {};
  if ('name' in body || needName) {
    if (typeof body.name !== 'string' || !body.name.trim() || body.name.length > MAX_NAME) return null;
    out.name = body.name;
  }
  for (const key of ['description', 'location']) {
    if (key in body) {
      if (body[key] !== null && (typeof body[key] !== 'string' || body[key].length > MAX_TEXT)) return null;
      out[key] = body[key] ?? '';
    }
  }
  for (const key of ['startTime', 'endTime']) {
    if (key in body && body[key] === null) {
      // a null end is the end date cleared in the edit window; a start is never cleared
      if (key === 'endTime') out.endTime = null;
    } else if (key in body) {
      if (!Number.isSafeInteger(body[key]) || body[key] <= 0) return null;
      out[key] = body[key];
    }
  }
  if (needName && !out.startTime) return null;
  if (out.startTime && out.endTime && out.endTime < out.startTime) return null;
  if ('callType' in body) {
    if (![null, 'none', 'voice', 'video'].includes(body.callType)) return null;
    out.callType = body.callType === 'none' ? null : body.callType;
  }
  if ('extraGuestsAllowed' in body) {
    if (typeof body.extraGuestsAllowed !== 'boolean') return null;
    out.extraGuestsAllowed = body.extraGuestsAllowed;
  }
  return out;
}

export async function readEventResponsesRoute(req: Request, res: Response) {
  const { messageIds } = req.body || {};
  if (!Array.isArray(messageIds) || messageIds.length < 1 || messageIds.length > 50
      || !messageIds.every((id) => typeof id === 'string' && MESSAGE_ID.test(id))) {
    return res.status(400).json({ status: 'error', code: 'invalid_message_ids' });
  }
  const page = pageOf(req);
  if (!page) return res.status(503).json({ status: 'error', code: 'event_responses_unavailable' });
  try {
    const response = await page.evaluate(readEventResponses, { messageIds });
    return res.status(200).json({ status: 'success', response });
  } catch (_) {
    return res.status(503).json({ status: 'error', code: 'event_responses_unavailable' });
  }
}

export async function respondEventRoute(req: Request, res: Response) {
  const { messageId, response } = req.body || {};
  if (typeof messageId !== 'string' || !MESSAGE_ID.test(messageId) || ![1, 2, 3].includes(response)) {
    return res.status(400).json({ status: 'error', code: 'event_response_invalid' });
  }
  const page = pageOf(req);
  if (!page) return res.status(503).json({ status: 'error', code: 'event_response_unavailable' });
  try {
    const outcome = await page.evaluate(writeEventResponse, { messageId, response });
    if (!outcome?.ok) return fail(res, outcome, 'event_response_failed');
    return res.status(200).json({ status: 'success' });
  } catch (_) {
    return res.status(503).json({ status: 'error', code: 'event_response_failed' });
  }
}

export async function sendEventRoute(req: Request, res: Response) {
  const body = req.body || {};
  const chatId = String(body.phone || body.chatId || '');
  const fields = eventFields(body);
  if (!CHAT_ID.test(chatId) || !fields) {
    return res.status(400).json({ status: 'error', code: 'event_invalid' });
  }
  const page = pageOf(req);
  if (!page) return res.status(503).json({ status: 'error', code: 'event_create_unavailable' });
  try {
    const outcome = await page.evaluate(createEvent, { chatId, ...fields });
    if (!outcome?.ok) return fail(res, outcome, 'event_create_failed');
    return res.status(200).json({ status: 'success', response: { id: outcome.id || '' } });
  } catch (_) {
    return res.status(503).json({ status: 'error', code: 'event_create_failed' });
  }
}

export async function editEventRoute(req: Request, res: Response) {
  const body = req.body || {};
  const messageId = String(body.messageId || '');
  const cancel = body.cancel === true;
  const fields = cancel ? {} : eventFields(body, false);
  if (!MESSAGE_ID.test(messageId) || !fields) {
    return res.status(400).json({ status: 'error', code: 'event_invalid' });
  }
  const page = pageOf(req);
  if (!page) return res.status(503).json({ status: 'error', code: 'event_edit_unavailable' });
  try {
    const outcome = await page.evaluate(editEvent, { messageId, ...fields, isEventCanceled: cancel });
    if (!outcome?.ok) return fail(res, outcome, 'event_edit_failed');
    return res.status(200).json({ status: 'success' });
  } catch (_) {
    return res.status(503).json({ status: 'error', code: 'event_edit_failed' });
  }
}
