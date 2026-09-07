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
 * WinZapp patch: failures named by what went wrong, not by an HTTP status.
 *
 * The controllers here each catch their own errors and pick a status inline,
 * which is how getMessages ended up answering 401 "Error on open list" for a
 * missing chat, a page that had not finished loading, and a genuine crash
 * alike — three different situations, one answer, and the one word it did say
 * ("unauthenticated") was true of none of them.
 *
 * A domain error carries the two things the caller actually needs: a stable
 * `reason` code it can branch on, and the HTTP status that best describes it.
 * Business logic throws these; the error middleware turns them into responses.
 * Nothing below imports express, on purpose — the moment a service has to
 * import HttpException-shaped types to explain itself, the protocol has leaked
 * into the logic, and testing that logic starts requiring a web framework.
 */

export class DomainError extends Error {
  /** Stable, machine-readable code. Clients branch on this, not on the text. */
  public readonly reason: string;
  /** What this means over HTTP, applied by the error middleware. */
  public readonly httpStatus: number;
  /** Anything worth logging with the failure. Never a credential. */
  public readonly details: Record<string, unknown>;

  constructor(
    reason: string,
    message: string,
    httpStatus: number,
    details: Record<string, unknown> = {}
  ) {
    super(message);
    this.name = new.target.name;
    this.reason = reason;
    this.httpStatus = httpStatus;
    this.details = details;
    // Without this, `instanceof` fails for subclasses of built-ins once the
    // code is transpiled down — the middleware would then treat every domain
    // error as an unknown crash and answer 500.
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/** The thing asked for does not exist (chat, message, contact). */
export class NotFoundError extends DomainError {
  constructor(message: string, details: Record<string, unknown> = {}) {
    super('not_found', message, 404, details);
  }
}

/**
 * The page is not ready to answer yet: wa-js not injected, session closed,
 * Puppeteer context destroyed by a navigation. Distinct from NotFound because
 * it is temporary — the caller should retry rather than conclude anything.
 */
export class SessionNotReadyError extends DomainError {
  constructor(message: string, details: Record<string, unknown> = {}) {
    super('session_not_ready', message, 503, details);
  }
}

/** The request itself is wrong; retrying it unchanged cannot help. */
export class InvalidRequestError extends DomainError {
  constructor(message: string, details: Record<string, unknown> = {}) {
    super('invalid_request', message, 400, details);
  }
}

/**
 * WhatsApp Web answered, and the answer was "no" — a status that cannot be
 * reacted to, a message that cannot be revoked. Not our bug and not a retry.
 */
export class UnsupportedOperationError extends DomainError {
  constructor(message: string, details: Record<string, unknown> = {}) {
    super('unsupported_operation', message, 422, details);
  }
}

/**
 * Classify an error thrown by WhatsApp Web / wa-js, whose failures arrive as
 * plain Errors with only a message to go on.
 *
 * Kept in one place because these strings are the only signal available and
 * they are matched from more than one call site; spreading the regexes around
 * is how two of them end up disagreeing about what "not found" means.
 */
export function classifyPageError(error: any): DomainError {
  const detail = String(error?.message || error || '');
  if (/not found|no such chat|chat not exist/i.test(detail)) {
    return new NotFoundError(detail);
  }
  // `Page closed` and `Waiting failed: <n>ms exceeded` both come from
  // host.layer.js's waitForPageLoad(), which wppconnect 2.3.2 made
  // isConnected() await: the first when the document died mid-injection, the
  // second when it loaded but never reached WPP.isReady within puppeteer's
  // default timeout. statusConnection.ts already names both, but it only runs
  // ahead of the routes that declare it — everything else reaches this
  // function through errorHandler, where the same two strings used to come
  // back as internal_error/500. They belong beside `Target closed`, which is
  // the same situation under puppeteer's older wording and already sits here.
  // This is a consistency fix, not a behaviour change for the sender: 500 and
  // 503 land in the same retryable bucket on the Python side. What it buys is
  // that one string stops meaning two different things in two files.
  // Widening is safe in a way `/not found/i` is not: no puppeteer or
  // wppconnect message carries "Page closed" for anything other than a dead
  // page, whereas "not found" also matches "Session not found".
  if (
    /WAPI is not defined|not connected|Session (closed|not active)|Execution context|Target closed|Page closed|Waiting failed: \d+ms exceeded/i.test(
      detail
    )
  ) {
    return new SessionNotReadyError(detail);
  }
  return new DomainError('internal_error', detail || 'Unknown error', 500);
}
