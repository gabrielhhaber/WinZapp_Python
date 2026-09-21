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
import { createHash } from 'crypto';
import winston from 'winston';

// WinZapp addition: mask WhatsApp phone numbers, LIDs and group ids in every
// log line, at the one place ALL of this process's log output (the console
// transport, which is what main.py pipes into wppconnect.log, and the file
// transport) passes through. Dozens of controller call sites log a JID or a
// status message id directly (e.g. deviceController.ts's
// `[status-reaction] begin msgId=${msgId}` — a status message id always ends
// in the poster's own JID, see _serialize_msg_id() in main.py) — editing
// every one of them is both impossible to keep complete and silently reopens
// on the next new call site. Masking the rendered message once here covers
// all of them, past and future, by construction. Mirrors
// client/core/pii_redaction.py's approach on the Python side so
// wppconnect.log and log.log redact the exact same way.
//
// Upper bound is 20, not 15: a phone/LID digit run is at most ~15, but a
// group JID's own id (never a participant's digits — see main.py's JID
// handling notes) runs longer, e.g. "120363067944453158@g.us" (18 digits).
const PHONE_OR_JID =
  /(?<!\d)(\d{8,20})(@(?:s\.whatsapp\.net|c\.us|lid|g\.us|broadcast|newsletter))?(?!\d)/g;

/**
 * Mask a WhatsApp phone number, LID or group id wherever it appears in
 * `text`, replacing the digits with a short, deterministic tag (an md5
 * prefix of the digits, not the number itself) so a log can still show
 * "this is the same id three times" without ever spelling out which one.
 * Idempotent: the tag never matches the digit-run pattern again.
 */
export function redactPhone(text: string): string {
  return text.replace(
    PHONE_OR_JID,
    (_match, digits: string, suffix?: string) => {
      const tag = createHash('md5').update(digits).digest('hex').slice(0, 8);
      return `~${tag}${suffix || ''}`;
    }
  );
}

const redactPii = winston.format((info) => {
  if (typeof info.message === 'string') {
    info.message = redactPhone(info.message);
  }
  if (typeof info.stack === 'string') {
    info.stack = redactPhone(info.stack);
  }
  return info;
})();

// Use JSON logging for log files
// Here winston.format.errors() just seem to work
// because there is no winston.format.simple()
const jsonLogFileFormat = winston.format.combine(
  winston.format.errors({ stack: true }),
  redactPii,
  winston.format.timestamp(),
  winston.format.prettyPrint()
);

export function createLogger(options: any) {
  const log_level = options.level;
  // Create file loggers
  const logger = winston.createLogger({
    level: 'debug',
    format: jsonLogFileFormat,
  });

  // When running locally, write everything to the console
  // with proper stacktraces enabled
  if (options.logger.indexOf('console') > -1) {
    logger.add(
      new winston.transports.Console({
        format: winston.format.combine(
          winston.format.errors({ stack: true }),
          redactPii,
          winston.format.colorize(),
          winston.format.printf(({ level, message, timestamp, stack }) => {
            if (stack) {
              // print log trace
              return `${level}: ${timestamp} ${message} - ${stack}`;
            }
            return `${level}: ${timestamp} ${message}`;
          })
        ),
      })
    );
  }
  if (options.logger.indexOf('file') > -1) {
    logger.add(
      new winston.transports.File({
        filename: './log/app.logg',
        level: log_level,
        maxsize: 10485760,
        maxFiles: 3,
      })
    );
  }

  return logger;
}
