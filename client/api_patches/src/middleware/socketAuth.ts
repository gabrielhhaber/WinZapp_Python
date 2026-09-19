import bcrypt from 'bcrypt';
import crypto from 'crypto';
import { Socket } from 'socket.io';

const SOCKET_SESSION_DATA_KEY = 'winzappSession';

function timingSafeEqualStr(a: unknown, b: unknown): boolean {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  const bufA = Buffer.from(a, 'utf8');
  const bufB = Buffer.from(b, 'utf8');
  if (bufA.length !== bufB.length) return false;
  return crypto.timingSafeEqual(bufA, bufB);
}

function headerString(value: unknown): string {
  if (typeof value === 'string') return value.trim();
  if (Array.isArray(value) && typeof value[0] === 'string') return value[0].trim();
  return '';
}

function bearerValue(value: unknown): string {
  const raw = headerString(value);
  if (!raw) return '';
  const match = raw.match(/^Bearer\s+(.+)$/i);
  return (match?.[1] || raw).trim();
}

function requestedSession(socket: Socket): string {
  const authSession = socket.handshake.auth?.session;
  return typeof authSession === 'string' ? authSession.split(':')[0].trim() : '';
}

function socketCredential(socket: Socket): string {
  const auth = socket.handshake.auth || {};
  const authToken =
    typeof auth.token === 'string'
      ? auth.token
      : typeof auth.apikey === 'string'
        ? auth.apikey
        : '';
  if (authToken.trim()) return authToken.trim();

  const apiKey = headerString(socket.handshake.headers?.apikey);
  if (apiKey) return apiKey;
  return bearerValue(socket.handshake.headers?.authorization);
}

/**
 * Authenticate one Socket.IO connection with the same `<session>:<bcrypt>`
 * credential used by WPPConnect's REST API.
 *
 * The global secret key is also accepted for administrative clients, but only
 * when they explicitly provide the session in the Socket.IO auth payload. A
 * secret by itself is not enough because every authenticated socket must be
 * bound to exactly one WhatsApp session before it can exchange media/events.
 */
export async function authenticateSocketSession(
  socket: Socket,
  secureToken: string
): Promise<string | null> {
  const credential = socketCredential(socket);
  const explicitSession = requestedSession(socket);
  if (!credential || !secureToken) return null;

  if (timingSafeEqualStr(credential, secureToken)) {
    return explicitSession || null;
  }

  const separator = credential.indexOf(':');
  if (separator <= 0 || separator >= credential.length - 1) return null;

  const session = credential.slice(0, separator).trim();
  const token = credential
    .slice(separator + 1)
    .replace(/_/g, '/')
    .replace(/-/g, '+');
  if (!session || !token) return null;
  if (explicitSession && explicitSession !== session) return null;

  const valid = await bcrypt.compare(session + secureToken, token);
  return valid ? session : null;
}

export function socketSession(socket: Socket): string {
  const session = socket.data?.[SOCKET_SESSION_DATA_KEY];
  return typeof session === 'string' ? session : '';
}

export function socketAuthMiddleware(secureToken: string, logger: any) {
  return async (socket: Socket, next: (err?: Error) => void): Promise<void> => {
    try {
      const session = await authenticateSocketSession(socket, secureToken);
      if (!session) {
        logger?.warn?.(`Rejected unauthenticated Socket.IO connection ${socket.id}`);
        next(new Error('unauthorized'));
        return;
      }
      socket.data[SOCKET_SESSION_DATA_KEY] = session;
      next();
    } catch (error: any) {
      logger?.warn?.(
        `Socket.IO authentication failed for ${socket.id}: ${error?.message || error}`
      );
      next(new Error('unauthorized'));
    }
  };
}
