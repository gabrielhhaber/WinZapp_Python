import type { Request, Response } from 'express';

import { communityComments } from '../util/communityCommentsRuntime';

async function run(req: Request, res: Response, operation: 'read' | 'send') {
  const messageId = req.params.messageId;
  const text = req.body?.text;
  if (typeof messageId !== 'string' || !messageId.trim()
      || (operation === 'send' && (typeof text !== 'string' || !text.trim()))) {
    return res.status(400).json({ status: 'error', code: 'invalid_comment_request' });
  }
  const page = (req.client as any)?.page;
  if (!page || page.isClosed()) {
    return res.status(503).json({ status: 'error', code: 'comments_unavailable' });
  }
  try {
    const result = await page.evaluate(communityComments, { operation, messageId, text });
    if (!result?.ok) {
      const code = result?.code;
      return res.status(code === 'not_community_announcement' ? 400 : operation === 'send' ? 502 : 503)
        .json({ status: 'error', code: code || 'comments_unavailable', retrySafe: false });
    }
    if (operation === 'read') {
      if (!Array.isArray(result.response)) throw new Error('comments_unavailable');
      return res.status(200).json({ status: 'success', response: result.response });
    }
    if (result.response?.messageSendResult === 'OK') {
      // Comment sends return a native verdict, not a normal-message id.
      return res.status(201).json({ status: 'success', response: result.response });
    }
  } catch {
    // Never expose a native error or turn an ambiguous send into a retry.
  }
  return res.status(operation === 'send' ? 502 : 503).json({
    status: 'error', code: operation === 'send' ? 'comment_unconfirmed' : 'comments_unavailable',
    retrySafe: false,
  });
}

export const getComments = (req: Request, res: Response) => run(req, res, 'read');
export const sendCommentMessage = (req: Request, res: Response) => run(req, res, 'send');
