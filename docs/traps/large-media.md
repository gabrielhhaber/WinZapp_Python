# Large media: download streaming and the four attachment size gates

> Base64-in-JSON blew up RAM on a 200 MB file; four gates must agree on 2 GB documents / 1 GB media.
>
> Moved verbatim out of `CLAUDE.md` so it is read when the area is touched, not on every session. Keep it here: this is measured history, not a summary.

## Downloading large media — the mirror of the send-side chunking

### The message event must not download a file for an absent webhook (2026-10-16)

The raw HTTP download path is not the only way a file can reach Python.
`listenMessages()` used the webhook's `autoDownload` flag for its Socket.IO
`received-message` event too. The default is true even when the webhook URL
is null, while `websocket.autoDownload` is false. Consequently a received
document was fully downloaded/decrypted and converted to base64 before its
metadata reached WinZapp, bypassing the client's download settings and limits.
With an actual webhook, its own `callWebHook()` path downloaded again.

Only the websocket's explicit download setting controls that event now;
`callWebHook()` still owns downloads for configured webhook deliveries.
`test_media_event_payload.py` runs the actual callbacks/utilities with
synthetic storage and networking: no-destination reception used to download
once, and a configured webhook twice. Both regressions are covered, alongside
outgoing events and explicitly requested websocket downloads.

HTTP progress also needs backpressure before `wx.CallAfter`, not just a
monotonic value after it. A 24 MiB upload at 256 KiB per chunk reports 98
values, each previously queuing a list-row repaint. Upload/download progress
callbacks now retain only their latest pending value per transfer. This does
not throttle the transport's cancellation checks or ordered stage events.

A bounded diagnostic encrypting 20 MiB with Fernet measured 133.3 MiB of
additional traced allocations, a 26.7 MiB token, 0.157 s elapsed and a 0.060 s
maximum synthetic heartbeat gap on this machine. Whole-file caching remains
a memory multiplier; that measurement alone does not establish the cause of
a long freeze. No actual large-file send or recipient interaction was used
to validate these changes. A fresh UI-watchdog stack and connection logs are
still needed to attribute a particular freeze/reconnect episode.

Sending files above ~100 MB started working once `wppconnect_sender_layer_patch.py` stopped handing one enormous argument across a boundary that could not take it. **Downloading never got the same treatment**, and a 200 MB document could not be fetched at all: it announced "baixando", produced nothing, filled RAM, and returned the button to Download.

Two independent faults, either one fatal on its own:

- **The response was base64 inside JSON.** `res.json({ base64: buffer.toString('base64') })` holds, for one 200 MB file, the Buffer plus a 267 MB base64 string plus the ~267 MB string `JSON.stringify` builds — and emits nothing until all three exist. Python then held the chunk list, the joined body, its decoded str, the str `json.loads` built, the decoded bytes and finally `encrypt()`'s Fernet token: **~1.3 GB in-process for one 200 MB file**. Past ~400 MB the server cannot answer at all, because `toString('base64')` exceeds V8's maximum string length. `sendMediaBuffer()` now sends raw bytes when the client sends `Accept: application/octet-stream`, and `fetch_media_bytes()` asks for them. It is **negotiated, not switched**: `client/api/` is reinstalled independently of the Python app, so a newer server must keep answering an older client its `{base64, mimetype}`, and the reader decides by Content-Type without ever touching the body.
- **The read timeout was a flat 60 s.** `requests` measures a read timeout *between bytes*, which is normally forgiving — except the server sends nothing at all while it fetches from WhatsApp's CDN and decrypts. That silence *is* the download, so the client abandoned a request the server was still working on, and the server kept going while holding everything above. `media_fetch_timeout()` scales it from the message's declared `fileLength` (pessimistic bytes/second, capped at 30 min), and falls back to the flat value whenever the size is missing or unparseable.

Note what is still whole-file and why: `encrypt()` is Fernet, which cannot stream, so Python holds the plaintext plus its ~1.37× token — ~470 MB for a 200 MB file. That is the floor until media-at-rest encryption changes, and it is far below the ~1.3 GB it replaced.

## Attachment size ceilings — 2 GB for documents, 1 GB for everything else

WhatsApp's own limit is **2 GB for documents** and **1 GB for photos, videos and audio**. WinZapp capped documents at 1 GB too until it was noticed, for no reason other than one shared constant. A large document therefore crosses **four independent gates, and all four have to agree** — miss one and the send fails somewhere the user cannot see:

1. `ConversationsPanel._on_send_attachment()` (`_MAX_DOCUMENT_BYTES` / `_MAX_MEDIA_BYTES`) — the pre-send dialog, the only one the user is told about.
2. `MainWindow.send_media_attachment()` — what the message queue actually calls.
3. `WebSocketClient._set_wpp_limits()`'s `maxFileSize` — one flat cap for every type, so it must be set to the **largest** of them (2 GB), not per type.
4. WhatsApp Web's own `MediaGatingUtils.getUploadLimit`, wrapped inside the page by `wppconnect_sender_layer_patch.py`.

`tests/test_large_file_patch.py::test_every_gate_a_large_document_passes_agrees_on_2gb` pins all four together rather than one by one. Size alone stopped being the constraint once the sender-layer patch started streaming files into Chromium in bounded chunks (the old single oversized CDP argument is what used to kill the session), so these ceilings are WhatsApp's, not WinZapp's.

Raising the page-side ceiling is the delicate half, because that patch is applied by idempotent search-and-replace to `node_modules`: an installation already patched with the previous text will not match the pristine source any more, so a **new block version plus a migration from the old one** is required, exactly as `_BROWSER_ATTACHMENT_LIMIT_PATCH_V3` derives from `_BROWSER_ATTACHMENT_LIMIT_PATCH_V2` and `patch_sender_layer_source()` rewrites V2 into V3 at both nesting depths. Never edit a shipped `_V<n>` constant in place — it is the left-hand side of a migration, and changing it strands every install that carries it. Order matters too: the V2→V3 ceiling rewrite must run *after* the WAV-bypass removal, whose match has V2 as its prefix.
