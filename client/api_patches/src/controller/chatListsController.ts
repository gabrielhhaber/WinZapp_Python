/** WinZapp adapter for WA-JS 4.6.1 personal CUSTOM lists (not favorites). */
import type { Request, Response } from 'express';
import { executeListCommand } from '../util/chatListsRuntime';

type ListCommand = {
  action: string;
  id?: string;
  name?: string;
  chatIds?: string[];
};

async function handle(req: Request, res: Response, command: ListCommand) {
  const page = (req.client as any)?.page;
  if (!page || page.isClosed()) return res.status(503).json({ status: 'error', code: 'lists_not_available' });
  try {
    const response = await page.evaluate(executeListCommand, command);
    return res.status(200).json({ status: 'success', response });
  } catch (error: any) {
    // No arbitrary exception text: native errors can contain list/contact names.
    const code = String(error?.code || error?.message || '');
    const known = ['lists_not_available', 'list_editing_not_available', 'list_not_found',
      'list_name_required', 'list_command_invalid', 'list_chat_not_found', 'list_response_invalid'];
    const safeCode = known.includes(code) ? code : 'list_operation_unconfirmed';
    const status = safeCode === 'lists_not_available' ? 501
      : safeCode === 'list_editing_not_available' ? 403
        : safeCode === 'list_operation_unconfirmed' ? 503 : 400;
    return res.status(status).json({ status: 'error', code: safeCode });
  }
}

export async function readLists(req: Request, res: Response) {
  return handle(req, res, { action: 'read' });
}

export async function changeList(req: Request, res: Response) {
  // Only our fields enter the page. No function name or arbitrary JS payload.
  const { action, id, name, chatIds } = req.body || {};
  return handle(req, res, { action, id, name, chatIds });
}
