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

import { NextFunction, Request, Response } from 'express';

import { SessionNotReadyError } from '../errors/domain';
import { contactToArray, probeIsConnected } from '../util/functions';

// How long this middleware waits for isConnected() before answering without
// it. Well under main.py's smallest send timeout (text posts with timeout=25,
// a voice message with timeout=30) and deliberately so — see the call site.
const CONNECTION_PROBE_BUDGET_MS = 8000;

function disconnected(res: Response, reason?: string) {
  return res.status(404).json({
    response: null,
    status: 'Disconnected',
    // Only the probe-timeout branch below sets this, and it is the sole thing
    // telling the two kinds of 404 apart on the Python side. Left undefined
    // everywhere else, which JSON.stringify() drops from the body entirely.
    reason,
    message: 'A sessão do WhatsApp não está ativa.',
  });
}

export default async function statusConnection(
  req: Request,
  res: Response,
  next: NextFunction
) {
  try {
    const numbers: any = [];
    // `typeof` rather than the bare reference: with the probe itself moved to
    // util/functions.ts nothing in this file calls isConnected() any more, and
    // tsc reads a lone function reference in a condition as TS2774.
    if (req.client && typeof req.client.isConnected === 'function') {
      const skipsConnectionProbe =
        req.path.endsWith('/typing') ||
        req.path.endsWith('/recording') ||
        req.path.endsWith('/send-seen');
      if (!skipsConnectionProbe) {
        // Bounded, because on wppconnect 2.3.2 isConnected() awaits
        // waitForPageLoad(), which sits on puppeteer's default 30s waiting for
        // WPP.isReady. This middleware fronts every send route, so an ordinary
        // WhatsApp Web reload made the client give up first: main.py's
        // ReadTimeout is classified *ambiguous* (WhatsApp Web may already have
        // accepted the message into its own outbox), so MessageQueue drops the
        // message without retrying and the user is told it is "unconfirmed" —
        // about a message that never reached a controller at all, because this
        // middleware had not let it through yet.
        //
        // An unanswered probe is reported as Disconnected rather than as
        // SessionNotReadyError: 404 leaves the message queued for the
        // reconnection, while the 503 is retryable and would spend
        // MessageQueue's four attempts against a page that is still reloading.
        // A probe that *answers* keeps its own meaning either way — a thrown
        // "WAPI is not defined" still reaches the catch below as a 503.
        //
        // It carries `reason: 'probe_timeout'` because that 404 is a weaker
        // claim than the one below it, and this middleware does not front only
        // sends: list-chats declares it too, and its callers (get_remote_chats
        // from start_sync, the post-sync settling pass and
        // _probe_chats_and_start_sync) are all background work. An unanswered
        // probe proves the request never reached a controller and nothing at
        // all about WhatsApp, while `connected === false` is the page itself
        // answering that the session is down. Undifferentiated, an ordinary
        // WhatsApp Web reload overlapping a sync round had Python announce
        // "modo offline" with sound and speech and then "conexão restaurada"
        // seconds later, over whatever the user was reading — on 2.3.1 that
        // same reload threw and left here as a 503, so it passed in silence.
        const connected = await probeIsConnected(
          req.client,
          CONNECTION_PROBE_BUDGET_MS
        );
        if (connected === undefined) return disconnected(res, 'probe_timeout');
        if (connected !== true) return disconnected(res);
      }

      const localArr = contactToArray(
        req.body.phone || [],
        req.body.isGroup,
        req.body.isNewsletter,
        req.body.isLid
      );
      let index = 0;
      // Same multipart-string-truthiness pitfall as contactToArray()
      // (util/functions.ts) — req.body.isGroup arrives as the literal
      // string "false" for any multipart/form-data call, which is truthy
      // in a bare `||` check.
      const wantsGroup =
        req.body.isGroup === true || req.body.isGroup === 'true';
      const wantsNewsletter =
        req.body.isNewsletter === true || req.body.isNewsletter === 'true';
      const wantsLid = req.body.isLid === true || req.body.isLid === 'true';
      for (const contact of localArr) {
        if (
          wantsGroup ||
          wantsNewsletter ||
          wantsLid ||
          (typeof contact === 'string' && contact.endsWith('@lid')) ||
          req.path.endsWith('/typing') ||
          req.path.endsWith('/recording') ||
          req.path.endsWith('/send-seen')
        ) {
          // checkNumberStatus() below expects a phone-number JID it can look
          // up in WhatsApp's contact directory — it doesn't understand @lid
          // identifiers. When the caller explicitly says this is a @lid
          // contact (already resolved via our own lid<->phone cache), skip
          // the existence check instead of letting it wrongly report the
          // contact as nonexistent.
          localArr[index] = contact;
        } else if (numbers.indexOf(contact) < 0) {
          console.log(contact);
          const profile: any = await req.client
            .checkNumberStatus(contact)
            .catch((error) => console.log(error));
          if (!profile?.numberExists) {
            const num = (contact as any).split('@')[0];
            return res.status(400).json({
              response: null,
              status: 'Connected',
              message: `O número ${num} não existe.`,
            });
          } else {
            if ((numbers as any).indexOf(profile.id._serialized) < 0) {
              (numbers as any).push(profile.id._serialized);
            }
            (localArr as any)[index] = profile.id._serialized;
          }
        }
        index++;
      }
      req.body.phone = localArr;
    } else {
      return disconnected(res);
    }
    next();
  } catch (error) {
    const detail = String((error as any)?.message || error || '');
    // "Waiting failed: 30000ms exceeded" is the other half of the same 2.3.2
    // change: waitForPageLoad() also awaits page.waitForFunction(() =>
    // WPP.isReady) on puppeteer's default 30s timeout, so a page that loaded
    // but never finished injecting wa-js throws this instead of hanging. It
    // says exactly what "WAPI is not defined" says — the document is alive,
    // wa-js is not ready — so it gets the same 503, not disconnected()'s 404,
    // which would claim the browser is gone. The digits are matched loosely
    // because the timeout is puppeteer's default, not a number we set.
    if (
      /WAPI is not defined|Execution context was destroyed|Waiting failed: \d+ms exceeded/i.test(
        detail
      )
    ) {
      next(new SessionNotReadyError(detail));
      return;
    }
    // "Page closed before WAPI injection completed" is thrown by
    // host.layer.js's waitForPageLoad(), which wppconnect 2.3.2 made
    // isConnected() await before it probes. On 2.3.1 the same situation
    // hung that loop forever; now it is an exception, and one that lands
    // here on every request. Without naming it, it falls through to
    // next(error) -> HTTP 500, which main.py's send paths class as retryable
    // (`is_retryable = response.status_code in (408, 429, 500, ...)`), so
    // MessageQueue would keep re-sending into a browser that is gone. It
    // means exactly what disconnected() means.
    if (
      /Target closed|not connected|Session (closed|not active)|Page closed/i.test(
        detail
      )
    ) {
      disconnected(res);
      return;
    }
    next(error);
  }
}
