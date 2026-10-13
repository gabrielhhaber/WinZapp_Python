import type { Request, Response } from 'express';
import {
  readPrivacySettings,
  writePrivacySetting,
  writeStatusPrivacy,
} from '../util/privacyRuntime';

// The values WhatsApp accepts for each account setting (WPP.privacy.set*).
const ACCOUNT_SETTINGS: Record<string, string[]> = {
  lastSeen: ['all', 'contacts', 'none'],
  online: ['all', 'match_last_seen'],
  about: ['all', 'contacts', 'none'],
  profilePicture: ['all', 'contacts', 'none'],
  readReceipts: ['all', 'none'],
  groupAdd: ['all', 'contacts'],
};

const STATUS_SETTINGS = ['contact', 'deny-list', 'allow-list'];

// A status audience is a list of people, never a group or a broadcast.
const PERSON_JID = /^\d{5,20}@(c\.us|lid)$/;

// WhatsApp's own limit for a status audience is far below this; the cap only
// keeps a malformed request from reaching the page.
const MAX_STATUS_LIST = 2000;

function livePage(req: Request) {
  const page = (req.client as any)?.page;
  return page && !page.isClosed() ? page : null;
}

/**
 * POST /api/:session/privacy — every account privacy setting (lastSeen,
 * online, about, profilePicture, readReceipts, groupAdd) and, when it can be
 * read, who can see the account's Status: `{ settings, status }`, where
 * `status` is `{ setting, list }` or null. `list` is null when the setting was
 * read but its contact list was not.
 */
export async function getPrivacySettings(req: Request, res: Response) {
  const page = livePage(req);
  if (!page) {
    return res.status(503).json({ status: 'error', code: 'privacy_unavailable' });
  }
  try {
    const response = await page.evaluate(readPrivacySettings);
    return res.status(200).json({ status: 'success', response });
  } catch (_) {
    // Native errors can include phone numbers and contact names.
    return res.status(503).json({ status: 'error', code: 'privacy_unavailable' });
  }
}

/**
 * POST /api/:session/privacy/set — `{ setting, value }`, one account setting.
 */
export async function setPrivacySetting(req: Request, res: Response) {
  const { setting, value } = req.body || {};
  const allowed = typeof setting === 'string' ? ACCOUNT_SETTINGS[setting] : undefined;
  if (!allowed || typeof value !== 'string' || !allowed.includes(value)) {
    return res.status(400).json({ status: 'error', code: 'invalid_privacy_setting' });
  }
  const page = livePage(req);
  if (!page) {
    return res.status(503).json({ status: 'error', code: 'privacy_unavailable' });
  }
  try {
    const result = await page.evaluate(writePrivacySetting, { setting, value });
    if (result?.ok) return res.status(200).json({ status: 'success' });
    return res.status(502).json({
      status: 'error',
      code: result?.error || 'privacy_setting_failed',
      kind: result?.kind,
    });
  } catch (_) {
    return res.status(503).json({ status: 'error', code: 'privacy_unavailable' });
  }
}

/**
 * POST /api/:session/privacy/status — `{ setting, list }`: who can see the
 * account's Status. `setting` is contact | deny-list | allow-list; `list` the
 * people it names (ignored for contact). Answers with the setting WhatsApp
 * reports afterwards.
 */
export async function setStatusPrivacy(req: Request, res: Response) {
  const { setting } = req.body || {};
  const list = req.body?.list ?? [];
  if (typeof setting !== 'string' || !STATUS_SETTINGS.includes(setting)
      || !Array.isArray(list) || list.length > MAX_STATUS_LIST
      || !list.every((id) => typeof id === 'string' && PERSON_JID.test(id))) {
    return res.status(400).json({ status: 'error', code: 'invalid_status_privacy' });
  }
  const page = livePage(req);
  if (!page) {
    return res.status(503).json({ status: 'error', code: 'privacy_unavailable' });
  }
  try {
    const result = await page.evaluate(writeStatusPrivacy, { setting, list });
    if (result?.ok) {
      return res.status(200).json({ status: 'success', response: { setting: result.setting } });
    }
    return res.status(502).json({
      status: 'error',
      code: result?.error || 'status_privacy_failed',
      kind: result?.kind,
    });
  } catch (_) {
    return res.status(503).json({ status: 'error', code: 'privacy_unavailable' });
  }
}
