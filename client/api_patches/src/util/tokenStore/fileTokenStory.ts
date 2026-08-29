import * as path from 'path';

import { FileTokenStore as fsTokenStore } from './FileTokenStore/FileTokenStore';

// Upstream never passes a `path` option here, so FileTokenStore falls back
// to its own hardcoded default ('./tokens', resolved against the Node
// process's own cwd). That is only a stable, persistent location in a
// --onedir build or dev mode; in a --onefile build the cwd is PyInstaller's
// per-launch extraction temp dir (a fresh, differently-named folder every
// single launch), so the saved auth token is silently orphaned the instant
// the app closes — the very next launch finds an empty tokens/ folder,
// wppconnect.log logs "Token store returned no data", and WhatsApp
// correctly (from its own side) decides the session needs a fresh QR scan.
// Reported live as "closed WinZapp, relaunched, told the device was
// disconnected" — with nothing wrong in any of WinZapp's own
// logout-detection logic, which had already been hardened against exactly
// that report. main.py sets WINZAPP_TOKEN_STORE_DIR to an absolute,
// install-writable path before spawning Node; the undefined fallback here
// (which makes FileTokenStore use its own built-in default) only applies
// when running this server outside WinZapp entirely.
// Logged once at module load — Node's module cache means this runs once per
// server process (i.e. once per WinZapp launch, since each account gets its
// own Node process), not once per session — so wppconnect.log always states
// in plain terms exactly where the token file for this run will be read
// from / written to, no more inferring it from "Token store returned no
// data" after the fact, or assuming it matches the previous run at all.
const _resolvedTokenDir = path.resolve(
  process.cwd(),
  process.env.WINZAPP_TOKEN_STORE_DIR || './tokens'
);
console.log(
  `[WinZapp] Token store directory: ${_resolvedTokenDir} ` +
  `(from ${process.env.WINZAPP_TOKEN_STORE_DIR ? 'WINZAPP_TOKEN_STORE_DIR env var' : 'built-in default relative to cwd — NOT persistent in a --onefile build'})`
);

class FileTokenStore {
  declare client: any;
  constructor(client: any) {
    this.client = client;
  }
  tokenStore = new fsTokenStore({
    ...(process.env.WINZAPP_TOKEN_STORE_DIR
      ? { path: process.env.WINZAPP_TOKEN_STORE_DIR }
      : {}),
    encodeFunction: (data) => {
      return this.encodeFunction(data, this.client.config);
    },
    decodeFunction: (text) => {
      return this.decodeFunction(text, this.client);
    },
  });

  public encodeFunction(data: any, config: any) {
    data.config = config;
    return JSON.stringify(data);
  }

  public async decodeFunction(text: string, client: any): Promise<string[]> {
    const object = JSON.parse(text);
    if (object.config && Object.keys(client.config).length === 0)
      client.config = object.config;
    if (object.webhook && Object.keys(client.config).length === 0)
      client.config.webhook = object.webhook;
    return object;
  }
}

export default FileTokenStore;
