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

/**
 * WinZapp patch: response contracts for the endpoints the Python client syncs
 * through — list-chats, get-messages, all-contacts and status-session.
 *
 * WHY THESE EXIST AT ALL
 *
 * Everything WhatsApp Web hands back is produced by minified, unversioned
 * third-party code that WinZapp does not control and that changes without
 * notice. When a field it used to return quietly changes shape, nothing here
 * notices: the payload still serialises, still returns 200, and the damage
 * only surfaces far downstream in Python, disguised as something else.
 *
 * That is not hypothetical. WhatsApp Web renamed MsgKey's `_serialized` getter
 * to `$1`; WPPConnect's serializer still read `_serialized`, so every message
 * came back with `id: undefined`, JSON.stringify dropped the key entirely,
 * WinZapp normalised the missing id to "" and DatabaseManager discarded 100%
 * of them as id-less. The visible symptom was a chat list with unread counts
 * over a database with zero messages — several steps and one process away from
 * the field that actually broke. See restoreMsgKeySerialized() in
 * util/createSessionUtil.ts, which is the shim that repairs it.
 *
 * WHAT THEY DO, AND DELIBERATELY DO NOT, DO
 *
 * Validation runs in "observe" mode: a payload that does not match is logged
 * and then returned COMPLETELY UNCHANGED. Nothing is rejected, nothing is
 * stripped, no request fails. A WhatsApp Web that starts sending a new field,
 * or an optional one WinZapp has never seen, must never be able to break a
 * running user's sync — this is an accessibility app people depend on daily,
 * and a hard failure here costs far more than a stale field.
 *
 * The value is the log line: it converts "unread counts are wrong and the
 * database is empty" into "list-chats item 0: id — expected string, got
 * undefined", at the exact boundary where the payload entered.
 *
 * Every object schema is `.passthrough()` for the same reason: these describe
 * the minimum WinZapp reads, never the full shape WhatsApp Web sends. Unknown
 * keys are normal and are not a finding.
 */

import { Logger } from 'winston';
import { z } from 'zod';

/** How many mismatches one log line carries before it is truncated. */
const MAX_LOGGED_ISSUES = 5;

/**
 * A message id, in either form WPPConnect serialises it as.
 *
 * The object form is the MsgKey described in this module's header: the reason
 * `_serialized` is optional here rather than required is that its absence is
 * precisely the historical bug, and an optional field still reports as missing
 * through `SyncMessageSchema` below without failing the whole message.
 */
export const MsgKeySchema = z.union([
  z.string(),
  z
    .object({
      _serialized: z.string().optional(),
      remote: z.union([z.string(), z.record(z.any())]).optional(),
      id: z.string().optional(),
      fromMe: z.boolean().optional(),
      participant: z.union([z.string(), z.record(z.any())]).optional(),
    })
    .passthrough(),
]);

/**
 * One item of get-messages.
 *
 * The required/optional split is not cosmetic: it mirrors exactly what
 * websocket_client._normalize_wpp_message() reads to build WinZapp's canonical
 * message dict. `id` is the only strictly required field, because it is the
 * one whose absence silently destroys the message downstream — everything else
 * degrades into a less useful message rather than a discarded one.
 */
export const SyncMessageSchema = z
  .object({
    id: MsgKeySchema,
    from: z.string().optional(),
    to: z.string().optional(),
    fromMe: z.union([z.boolean(), z.string()]).optional(),
    isStatus: z.boolean().optional(),
    type: z.string().optional(),
    t: z.number().optional(),
    timestamp: z.number().optional(),
    body: z.string().nullable().optional(),
    caption: z.string().nullable().optional(),
    mimetype: z.string().nullable().optional(),
    mediaKey: z.string().nullable().optional(),
    clientUrl: z.string().nullable().optional(),
    isPtt: z.boolean().optional(),
    isGif: z.boolean().optional(),
  })
  .passthrough();

/**
 * One item of list-chats.
 *
 * `msgs` is legitimately null on this endpoint — list-chats serialises every
 * chat without its last message — so it is typed as such rather than merely
 * omitted, to keep that from ever reading as a defect.
 */
export const SyncChatSchema = z
  .object({
    id: z.union([z.string(), z.record(z.any())]),
    name: z.string().nullable().optional(),
    isGroup: z.boolean().optional(),
    unreadCount: z.number().optional(),
    t: z.number().nullable().optional(),
    archive: z.boolean().nullable().optional(),
    // The pin TIMESTAMP in ms on a live list-chats (1783718891426), a bool on
    // other paths, and occasionally the string "true"/"false" — which is why
    // both Python consumers parse it three ways. It was modelled as a boolean
    // `pinned` here, a field that does not exist in the payload at all.
    pin: z.union([z.boolean(), z.number(), z.string()]).nullable().optional(),
    msgs: z.null().or(z.array(z.any())).optional(),
  })
  .passthrough();

/** One item of all-contacts. */
export const SyncContactSchema = z
  .object({
    id: z.union([z.string(), z.record(z.any())]),
    name: z.string().nullable().optional(),
    pushname: z.string().nullable().optional(),
    shortName: z.string().nullable().optional(),
    isMyContact: z.boolean().optional(),
  })
  .passthrough();

/**
 * status-session.
 *
 * `status` is the field the whole connection state machine turns on
 * (CONNECTED / INITIALIZING / CLOSED / QRCODE ...), so it is required; the
 * Python side treats an unknown string as "not connected yet" rather than an
 * error, which is why it is not an enum.
 */
export const StatusSessionSchema = z
  .object({
    status: z.string(),
    syncing: z.boolean().optional(),
    qrcode: z.string().nullable().optional(),
    urlcode: z.string().nullable().optional(),
    session: z.string().optional(),
  })
  .passthrough();

export const SyncMessageListSchema = z.array(SyncMessageSchema);
export const SyncChatListSchema = z.array(SyncChatSchema);
export const SyncContactListSchema = z.array(SyncContactSchema);

export type MsgKey = z.infer<typeof MsgKeySchema>;
export type SyncMessage = z.infer<typeof SyncMessageSchema>;
export type SyncChat = z.infer<typeof SyncChatSchema>;
export type SyncContact = z.infer<typeof SyncContactSchema>;
export type StatusSession = z.infer<typeof StatusSessionSchema>;

/**
 * Check `payload` against `schema` and return it EXACTLY as received.
 *
 * The return value is the untouched input — never zod's parsed output — on
 * purpose. `.passthrough()` preserves unknown keys, but zod still rebuilds the
 * object, and handing the rebuilt copy back would make this middleware able to
 * change what users receive. It must only ever be able to change what the log
 * says.
 *
 * Never throws: a schema bug, or a payload shaped so unusually that zod itself
 * fails, must not be able to take down an endpoint that was working.
 */
export function observePayload<T>(
  schema: z.ZodType<T>,
  payload: unknown,
  context: { logger?: Logger; endpoint: string }
): unknown {
  try {
    const result = schema.safeParse(payload);
    if (!result.success) {
      const issues = result.error.issues
        .slice(0, MAX_LOGGED_ISSUES)
        .map(
          (issue) => `${issue.path.join('.') || '<root>'}: ${issue.message}`
        );
      const omitted = result.error.issues.length - issues.length;
      context.logger?.warn?.(
        `[contract] ${context.endpoint} does not match its schema — ` +
          `${issues.join('; ')}${omitted > 0 ? ` (+${omitted} more)` : ''}`
      );
    }
  } catch (error: any) {
    context.logger?.warn?.(
      `[contract] ${context.endpoint} check failed to run: ${
        error?.message || error
      }`
    );
  }
  return payload;
}
