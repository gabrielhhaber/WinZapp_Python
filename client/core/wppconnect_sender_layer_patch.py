"""Shared source-text constant for patching @wppconnect-team/wppconnect's
compiled sender.layer.js — three independent fixes to sendFile(), applied
together since they touch the same method:

3. The MediaGatingUtils.getUploadLimit() override below used to run on
   EVERY document send, unconditionally re-wrapping whatever function was
   currently installed with a brand-new closure around it — with no check
   for "already wrapped". mediaGating is WPP.whatsapp.MediaGatingUtils, a
   singleton that lives for the whole WhatsApp Web page's lifetime (the
   entire session, since the page is never reloaded on its own), so every
   document a user sent added one more permanent link to a closure chain
   nothing ever released: send 500 documents in one session and
   getUploadLimit() is 500 closures deep, and none of it is reclaimed until
   the page itself reloads. This is a real, unbounded, one-way memory leak
   in the browser process for any WinZapp user who sends documents
   regularly. Fixed by setting a one-time flag
   (mediaGating.__winzappUploadLimitPatched) before wrapping, so the
   override installs exactly once per page load no matter how many
   attachments follow — see the guard added to PATCHED_SEND_FILE and
   _BROWSER_ATTACHMENT_LIMIT_PATCH below, and LEGACY_PATCHED_SEND_FILE_V2 /
   ALL_PATCHES for how an already-patched (leaking) install on an existing
   user's machine gets migrated to the guarded version on the next
   setup_api.py run / app update.

1. sendFile() losing the real error when a send fails inside the browser
   page. Every video sent from WinZapp (message attachment AND status)
   used to fail with an opaque HTTP 500 whose logged "error" was just
   {"name":"t","message":"t"} — useless, single-letter, minified junk. Root
   cause: `WPP.chat.sendFileMessage()` throws INSIDE the Puppeteer page
   context (browser side), and whatever it throws there is not a standard
   `Error` instance wa-js's own minified bundle constructs cleanly —
   Puppeteer serializes a thrown page-context exception across the CDP
   boundary via `Runtime.evaluate`'s `exceptionDetails`, which for a
   non-standard thrown value only reliably carries a `className`/
   `description`, not the real message/stack. That's what "t"/"t" actually
   is: the minified class name of whatever wa-js threw, with no usable
   text. Fixed by catching the exception INSIDE the page (where the real
   Error object with its real message/stack still exists) and RETURNing it
   as plain data instead of letting it cross the CDP exception boundary raw
   — `page.evaluate()`'s return value goes through ordinary JSON-safe
   structured cloning, which preserves whatever plain string properties are
   pulled off the error before returning, unlike its exception path. The
   Node side then reconstructs and throws a real `Error` from that data, so
   messageController.ts's returnError() (see
   client/api_patches/src/controller/messageController.ts) finally has real
   text to report instead of "t".

2. The bounded/chunked browser transfer (PATCHED_SEND_FILE below — streams
   the file into Chromium in 3MB pieces via window.__winzappFileTransfers
   instead of building one giant base64 string and passing it as a single
   CDP argument) was gated to `options.type === 'document'` only
   (PATCHED_FILE_LOADING_V1). Every other type (image/video/audio) always
   fell through to the old base64-in-memory path regardless of size —
   exactly the "one oversized CDP argument" problem the chunked path exists
   to avoid, just for every type BUT documents. Reported live as "erro 500
   ao enviar vídeos em diferentes formatos" for anything past WinZapp's own
   conservative client-side media cap (a cap that only existed because this
   path couldn't safely go any higher). WPP.chat.sendFileMessage() doesn't
   care whether the content it's classifying is a document or a video —
   options.type (always explicitly set by WinZapp, never left at
   'auto-detect') is what tells it that, identically on both paths — so
   PATCHED_FILE_LOADING widens the gate to image/video/audio too.

3. Raising WhatsApp Web's own client-side attachment ceiling — 2 GB for
   documents, 1 GB for photos/videos/audio, which is what WhatsApp itself
   allows — by
   wrapping MediaGatingUtils.getUploadLimit inside the page. This runs in a
   per-send callback, so it MUST install itself only once: the guard that
   decides whether to wrap cannot be "does getUploadLimit exist", because a
   wrapper is itself a getUploadLimit and the answer is yes forever after.
   The first version did exactly that and layered a new closure over the
   previous one on every document sent, each call then walking the whole
   chain, growing for as long as the page lived. `__winzappUploadLimitPatched`
   on the object is the marker that makes it once-per-page instead; the
   unmarked variant is migrated by patch_sender_layer_source() below.

Both setup_api.py and ApiSetupDialog (client/ui/dialogs/api_setup.py) apply
this patch to node_modules right after every `npm install` — see
client/core/wppconnect_host_layer_patch.py's module docstring for why that
sharing matters and why this can't go through the normal api_patches/
mechanism (sender.layer.js is compiled output of a THIRD-PARTY dependency,
not WPPConnect Server's own source).
"""

ORIGINAL_SEND_FILE = (
    "        const sendResult = await (0, helpers_1.evaluateAndReturn)(this.page, async ({ to, base64, options }) => {\n"
    "            const result = await WPP.chat.sendFileMessage(to, base64, {\n"
    "                waitForAck: true,\n"
    "                ...options,\n"
    "            });\n"
    "            return { ack: result.ack, id: result.id };\n"
    "        }, { to, base64, options: options });\n"
    "        return sendResult;\n"
)

ORIGINAL_FILE_LOADING = (
    "        let base64 = '';\n"
    "        if (pathOrBase64.startsWith('data:')) {\n"
    "            base64 = pathOrBase64;\n"
    "        }\n"
    "        else {\n"
    "            let fileContent = await (0, helpers_1.downloadFileToBase64)(pathOrBase64);\n"
    "            if (!fileContent) {\n"
    "                fileContent = await (0, helpers_1.fileToBase64)(pathOrBase64);\n"
    "            }\n"
    "            if (fileContent) {\n"
    "                base64 = fileContent;\n"
    "            }\n"
    "            if (!options.filename) {\n"
    "                options.filename = path.basename(pathOrBase64);\n"
    "            }\n"
    "        }\n"
    "        if (!base64) {\n"
    "            const error = new Error('Empty or invalid file or base64');\n"
    "            Object.assign(error, {\n"
    "                code: 'empty_file',\n"
    "            });\n"
    "            throw error;\n"
    "        }\n"
)

PATCHED_FILE_LOADING_V1 = (
    "        let base64 = '';\n"
    "        let largeFilePath = '';\n"
    "        if (!pathOrBase64.startsWith('data:') && options.type === 'document') {\n"
    "            try {\n"
    "                if (require('fs').statSync(pathOrBase64).size > 8 * 1024 * 1024)\n"
    "                    largeFilePath = pathOrBase64;\n"
    "            }\n"
    "            catch (_) { }\n"
    "        }\n"
    "        if (pathOrBase64.startsWith('data:')) {\n"
    "            base64 = pathOrBase64;\n"
    "        }\n"
    "        else if (!largeFilePath) {\n"
    "            let fileContent = await (0, helpers_1.downloadFileToBase64)(pathOrBase64);\n"
    "            if (!fileContent) {\n"
    "                fileContent = await (0, helpers_1.fileToBase64)(pathOrBase64);\n"
    "            }\n"
    "            if (fileContent) {\n"
    "                base64 = fileContent;\n"
    "            }\n"
    "        }\n"
    "        if (!options.filename && !pathOrBase64.startsWith('data:')) {\n"
    "            options.filename = path.basename(pathOrBase64);\n"
    "        }\n"
    "        if (!base64 && !largeFilePath) {\n"
    "            const error = new Error('Empty or invalid file or base64');\n"
    "            Object.assign(error, {\n"
    "                code: 'empty_file',\n"
    "            });\n"
    "            throw error;\n"
    "        }\n"
)

# v2: the chunked/bounded-transfer path (see PATCHED_SEND_FILE below) was
# document-only — anything else (image/video/audio) always fell through to
# the base64-in-memory branch just below, however large. That's the old
# "single oversized CDP argument" problem PATCHED_SEND_FILE exists to avoid
# in the first place, just for every OTHER media type instead of documents:
# reported live as "erro 500 ao enviar vídeos em diferentes formatos" for
# anything beyond WinZapp's own conservative 70MB client-side media cap
# (ui/conversations.py's since-removed _MAX_MEDIA_BYTES / websocket_client.py's
# maxMediaSize) — a cap that only existed because this path couldn't safely
# go any higher, and that widening this gate is what allowed image/video/audio
# to be folded into the single _MAX_ATTACHMENT_BYTES ceiling documents already
# used. WPP.chat.sendFileMessage() itself doesn't care whether the
# content it's classifying is a document or a video — options.type (always
# explicitly set by WinZapp, never left at 'auto-detect') is what tells it
# that, identically on both paths — so there is no reason large image/
# video/audio sends can't reuse the exact same bounded transfer.
PATCHED_FILE_LOADING = (
    "        let base64 = '';\n"
    "        let largeFilePath = '';\n"
    "        if (\n"
    "            !pathOrBase64.startsWith('data:') &&\n"
    "            ['document', 'image', 'video', 'audio'].includes(options.type)\n"
    "        ) {\n"
    "            try {\n"
    "                if (require('fs').statSync(pathOrBase64).size > 8 * 1024 * 1024)\n"
    "                    largeFilePath = pathOrBase64;\n"
    "            }\n"
    "            catch (_) { }\n"
    "        }\n"
    "        if (pathOrBase64.startsWith('data:')) {\n"
    "            base64 = pathOrBase64;\n"
    "        }\n"
    "        else if (!largeFilePath) {\n"
    "            let fileContent = await (0, helpers_1.downloadFileToBase64)(pathOrBase64);\n"
    "            if (!fileContent) {\n"
    "                fileContent = await (0, helpers_1.fileToBase64)(pathOrBase64);\n"
    "            }\n"
    "            if (fileContent) {\n"
    "                base64 = fileContent;\n"
    "            }\n"
    "        }\n"
    "        if (!options.filename && !pathOrBase64.startsWith('data:')) {\n"
    "            options.filename = path.basename(pathOrBase64);\n"
    "        }\n"
    "        if (!base64 && !largeFilePath) {\n"
    "            const error = new Error('Empty or invalid file or base64');\n"
    "            Object.assign(error, {\n"
    "                code: 'empty_file',\n"
    "            });\n"
    "            throw error;\n"
    "        }\n"
)

LEGACY_PATCHED_SEND_FILE = (
    "        const sendResult = await (0, helpers_1.evaluateAndReturn)(this.page, async ({ to, base64, options }) => {\n"
    "            try {\n"
    "                const result = await WPP.chat.sendFileMessage(to, base64, {\n"
    "                    waitForAck: true,\n"
    "                    ...options,\n"
    "                });\n"
    "                return { ack: result.ack, id: result.id };\n"
    "            }\n"
    "            catch (e) {\n"
    "                return {\n"
    "                    __winzappSendFileError: true,\n"
    "                    message: (e && e.message) || String(e),\n"
    "                    name: (e && e.name) || 'Error',\n"
    "                    stack: e && e.stack,\n"
    "                };\n"
    "            }\n"
    "        }, { to, base64, options: options });\n"
    "        if (sendResult && sendResult.__winzappSendFileError) {\n"
    "            const err = new Error(sendResult.message);\n"
    "            err.name = sendResult.name;\n"
    "            if (sendResult.stack)\n"
    "                err.stack = sendResult.stack;\n"
    "            throw err;\n"
    "        }\n"
    "        return sendResult;\n"
)

PATCHED_SEND_FILE = (
    "        let sendResult;\n"
    "        if (largeFilePath) {\n"
    "            const transferId = `winzapp-${Date.now()}-${Math.random()}`;\n"
    "            const mime = options.mimetype || 'application/octet-stream';\n"
    "            await this.page.evaluate((id) => {\n"
    "                window.__winzappFileTransfers = window.__winzappFileTransfers || new Map();\n"
    "                window.__winzappFileTransfers.set(id, []);\n"
    "            }, transferId);\n"
    "            try {\n"
    "                const stream = require('fs').createReadStream(largeFilePath, { highWaterMark: 3 * 1024 * 1024 });\n"
    "                for await (const data of stream) {\n"
    "                    const chunk = data.toString('base64');\n"
    "                    await this.page.evaluate(({ id, chunk }) => {\n"
    "                        const binary = atob(chunk);\n"
    "                        const bytes = new Uint8Array(binary.length);\n"
    "                        for (let i = 0; i < binary.length; i++)\n"
    "                            bytes[i] = binary.charCodeAt(i);\n"
    "                        window.__winzappFileTransfers.get(id).push(bytes);\n"
    "                    }, { id: transferId, chunk });\n"
    "                }\n"
    "                sendResult = await (0, helpers_1.evaluateAndReturn)(this.page, async ({ id, to, mime, options }) => {\n"
    "                    try {\n"
    "                        const chunks = window.__winzappFileTransfers.get(id);\n"
    "                        const file = new File(chunks, options.filename || 'file', { type: mime });\n"
    "                        const mediaGating = WPP.whatsapp?.MediaGatingUtils;\n"
    "                        if (options.type === 'document' && mediaGating?.getUploadLimit && !mediaGating.__winzappUploadLimitPatched) {\n"
    "                            const getUploadLimit = mediaGating.getUploadLimit.bind(mediaGating);\n"
    "                            mediaGating.getUploadLimit = (type, origin, isVcard) => type === 'document'\n"
    "                                ? Math.max(getUploadLimit(type, origin, isVcard), 1 * 1024 * 1024 * 1024)\n"
    "                                : getUploadLimit(type, origin, isVcard);\n"
    "                            mediaGating.__winzappUploadLimitPatched = true;\n"
    "                        }\n"
    "                        const result = await WPP.chat.sendFileMessage(to, file, {\n"
    "                            waitForAck: true,\n"
    "                            ...options,\n"
    "                        });\n"
    "                        return { ack: result.ack, id: result.id };\n"
    "                    }\n"
    "                    catch (e) {\n"
    "                        return {\n"
    "                            __winzappSendFileError: true,\n"
    "                            message: (e && e.message) || String(e),\n"
    "                            name: (e && e.name) || 'Error',\n"
    "                            stack: e && e.stack,\n"
    "                        };\n"
    "                    }\n"
    "                }, { id: transferId, to, mime, options: options });\n"
    "            }\n"
    "            finally {\n"
    "                await this.page.evaluate((id) => {\n"
    "                    if (window.__winzappFileTransfers)\n"
    "                        window.__winzappFileTransfers.delete(id);\n"
    "                }, transferId).catch(() => undefined);\n"
    "            }\n"
    "        }\n"
    "        else {\n"
    "            sendResult = await (0, helpers_1.evaluateAndReturn)(this.page, async ({ to, base64, options }) => {\n"
    "                try {\n"
    "                    const mediaGating = WPP.whatsapp?.MediaGatingUtils;\n"
    "                    if (options.type === 'document' && mediaGating?.getUploadLimit && !mediaGating.__winzappUploadLimitPatched) {\n"
    "                        const getUploadLimit = mediaGating.getUploadLimit.bind(mediaGating);\n"
    "                        mediaGating.getUploadLimit = (type, origin, isVcard) => type === 'document'\n"
    "                            ? Math.max(getUploadLimit(type, origin, isVcard), 1 * 1024 * 1024 * 1024)\n"
    "                            : getUploadLimit(type, origin, isVcard);\n"
    "                        mediaGating.__winzappUploadLimitPatched = true;\n"
    "                    }\n"
    "                    const result = await WPP.chat.sendFileMessage(to, base64, {\n"
    "                        waitForAck: true,\n"
    "                        ...options,\n"
    "                    });\n"
    "                    return { ack: result.ack, id: result.id };\n"
    "                }\n"
    "                catch (e) {\n"
    "                    return {\n"
    "                        __winzappSendFileError: true,\n"
    "                        message: (e && e.message) || String(e),\n"
    "                        name: (e && e.name) || 'Error',\n"
    "                        stack: e && e.stack,\n"
    "                    };\n"
    "                }\n"
    "            }, { to, base64, options: options });\n"
    "        }\n"
    "        if (sendResult && sendResult.__winzappSendFileError) {\n"
    "            const err = new Error(sendResult.message);\n"
    "            err.name = sendResult.name;\n"
    "            if (sendResult.stack)\n"
    "                err.stack = sendResult.stack;\n"
    "            throw err;\n"
    "        }\n"
    "        return sendResult;\n"
)

# The exact document-only upload-limit patch shipped before the browser-side
# limit override caught up with PATCHED_FILE_LOADING's image/video/audio
# support.  Keep the whole previous method so patch_sender_layer_source() can
# migrate an already-patched node_modules on the next app start/update.
LEGACY_PATCHED_SEND_FILE_V3 = PATCHED_SEND_FILE

# The exact PATCHED_SEND_FILE text shipped before the
# __winzappUploadLimitPatched guard existed — every install that already
# ran setup_api.py/ApiSetupDialog against an earlier WinZapp build has
# this verbatim text sitting in its node_modules right now, silently
# leaking one closure per document sent. Kept only so ALL_PATCHES below
# can find and migrate it forward; never touched otherwise.
LEGACY_PATCHED_SEND_FILE_V2 = (
    "        let sendResult;\n"
    "        if (largeFilePath) {\n"
    "            const transferId = `winzapp-${Date.now()}-${Math.random()}`;\n"
    "            const mime = options.mimetype || 'application/octet-stream';\n"
    "            await this.page.evaluate((id) => {\n"
    "                window.__winzappFileTransfers = window.__winzappFileTransfers || new Map();\n"
    "                window.__winzappFileTransfers.set(id, []);\n"
    "            }, transferId);\n"
    "            try {\n"
    "                const stream = require('fs').createReadStream(largeFilePath, { highWaterMark: 3 * 1024 * 1024 });\n"
    "                for await (const data of stream) {\n"
    "                    const chunk = data.toString('base64');\n"
    "                    await this.page.evaluate(({ id, chunk }) => {\n"
    "                        const binary = atob(chunk);\n"
    "                        const bytes = new Uint8Array(binary.length);\n"
    "                        for (let i = 0; i < binary.length; i++)\n"
    "                            bytes[i] = binary.charCodeAt(i);\n"
    "                        window.__winzappFileTransfers.get(id).push(bytes);\n"
    "                    }, { id: transferId, chunk });\n"
    "                }\n"
    "                sendResult = await (0, helpers_1.evaluateAndReturn)(this.page, async ({ id, to, mime, options }) => {\n"
    "                    try {\n"
    "                        const chunks = window.__winzappFileTransfers.get(id);\n"
    "                        const file = new File(chunks, options.filename || 'file', { type: mime });\n"
    "                        const mediaGating = WPP.whatsapp?.MediaGatingUtils;\n"
    "                        if (options.type === 'document' && mediaGating?.getUploadLimit) {\n"
    "                            const getUploadLimit = mediaGating.getUploadLimit.bind(mediaGating);\n"
    "                            mediaGating.getUploadLimit = (type, origin, isVcard) => type === 'document'\n"
    "                                ? Math.max(getUploadLimit(type, origin, isVcard), 1 * 1024 * 1024 * 1024)\n"
    "                                : getUploadLimit(type, origin, isVcard);\n"
    "                        }\n"
    "                        const result = await WPP.chat.sendFileMessage(to, file, {\n"
    "                            waitForAck: true,\n"
    "                            ...options,\n"
    "                        });\n"
    "                        return { ack: result.ack, id: result.id };\n"
    "                    }\n"
    "                    catch (e) {\n"
    "                        return {\n"
    "                            __winzappSendFileError: true,\n"
    "                            message: (e && e.message) || String(e),\n"
    "                            name: (e && e.name) || 'Error',\n"
    "                            stack: e && e.stack,\n"
    "                        };\n"
    "                    }\n"
    "                }, { id: transferId, to, mime, options: options });\n"
    "            }\n"
    "            finally {\n"
    "                await this.page.evaluate((id) => {\n"
    "                    if (window.__winzappFileTransfers)\n"
    "                        window.__winzappFileTransfers.delete(id);\n"
    "                }, transferId).catch(() => undefined);\n"
    "            }\n"
    "        }\n"
    "        else {\n"
    "            sendResult = await (0, helpers_1.evaluateAndReturn)(this.page, async ({ to, base64, options }) => {\n"
    "                try {\n"
    "                    const mediaGating = WPP.whatsapp?.MediaGatingUtils;\n"
    "                    if (options.type === 'document' && mediaGating?.getUploadLimit) {\n"
    "                        const getUploadLimit = mediaGating.getUploadLimit.bind(mediaGating);\n"
    "                        mediaGating.getUploadLimit = (type, origin, isVcard) => type === 'document'\n"
    "                            ? Math.max(getUploadLimit(type, origin, isVcard), 1 * 1024 * 1024 * 1024)\n"
    "                            : getUploadLimit(type, origin, isVcard);\n"
    "                    }\n"
    "                    const result = await WPP.chat.sendFileMessage(to, base64, {\n"
    "                        waitForAck: true,\n"
    "                        ...options,\n"
    "                    });\n"
    "                    return { ack: result.ack, id: result.id };\n"
    "                }\n"
    "                catch (e) {\n"
    "                    return {\n"
    "                        __winzappSendFileError: true,\n"
    "                        message: (e && e.message) || String(e),\n"
    "                        name: (e && e.name) || 'Error',\n"
    "                        stack: e && e.stack,\n"
    "                    };\n"
    "                }\n"
    "            }, { to, base64, options: options });\n"
    "        }\n"
    "        if (sendResult && sendResult.__winzappSendFileError) {\n"
    "            const err = new Error(sendResult.message);\n"
    "            err.name = sendResult.name;\n"
    "            if (sendResult.stack)\n"
    "                err.stack = sendResult.stack;\n"
    "            throw err;\n"
    "        }\n"
    "        return sendResult;\n"
)

#: The same override as _BROWSER_DOCUMENT_LIMIT_PATCH below, but WITHOUT the
#: one-time `__winzappUploadLimitPatched` guard — i.e. the leaking version that
#: re-wrapped getUploadLimit() on every single document send. Kept only as the
#: left-hand side of the migration in patch_sender_layer_source(), so a
#: node_modules already patched with it is rewritten to the guarded form on the
#: next setup_api.py run / app update instead of being left leaking forever.
#:
#: This constant went missing in a merge: the same leak was fixed twice in
#: parallel (once on this branch, once on main), and the resolution kept the
#: other side's definitions together with this side's patch_sender_layer_source()
#: body — which references this name. The result was a NameError that both
#: installers caught and logged as a warning, silently skipping the whole
#: sender.layer.js patch (1 GB chunked upload and real send-error detail
#: included). tests/test_large_file_patch.py covers the migration itself.
_LEGACY_DOCUMENT_LIMIT_PATCH = (
    "                    const mediaGating = WPP.whatsapp?.MediaGatingUtils;\n"
    "                    if (options.type === 'document' && mediaGating?.getUploadLimit) {\n"
    "                        const getUploadLimit = mediaGating.getUploadLimit.bind(mediaGating);\n"
    "                        mediaGating.getUploadLimit = (type, origin, isVcard) => type === 'document'\n"
    "                            ? Math.max(getUploadLimit(type, origin, isVcard), 1 * 1024 * 1024 * 1024)\n"
    "                            : getUploadLimit(type, origin, isVcard);\n"
    "                    }\n"
)


_BROWSER_DOCUMENT_LIMIT_PATCH = (
    "                    const mediaGating = WPP.whatsapp?.MediaGatingUtils;\n"
    "                    if (options.type === 'document' && mediaGating?.getUploadLimit && !mediaGating.__winzappUploadLimitPatched) {\n"
    "                        const getUploadLimit = mediaGating.getUploadLimit.bind(mediaGating);\n"
    "                        mediaGating.getUploadLimit = (type, origin, isVcard) => type === 'document'\n"
    "                            ? Math.max(getUploadLimit(type, origin, isVcard), 1 * 1024 * 1024 * 1024)\n"
    "                            : getUploadLimit(type, origin, isVcard);\n"
    "                        mediaGating.__winzappUploadLimitPatched = true;\n"
    "                    }\n"
)


_BROWSER_ATTACHMENT_LIMIT_PATCH_V1 = (
    "                    const mediaGating = WPP.whatsapp?.MediaGatingUtils;\n"
    "                    if (['document', 'image', 'video', 'audio'].includes(options.type) && mediaGating?.getUploadLimit && !mediaGating.__winzappUploadLimitPatched) {\n"
    "                        const getUploadLimit = mediaGating.getUploadLimit.bind(mediaGating);\n"
    "                        mediaGating.getUploadLimit = (type, origin, isVcard) => ['document', 'image', 'video', 'audio'].includes(type)\n"
    "                            ? Math.max(getUploadLimit(type, origin, isVcard), 1 * 1024 * 1024 * 1024)\n"
    "                            : getUploadLimit(type, origin, isVcard);\n"
    "                        mediaGating.__winzappUploadLimitPatched = true;\n"
    "                    }\n"
)


_BROWSER_ATTACHMENT_LIMIT_PATCH_V2 = (
    _BROWSER_ATTACHMENT_LIMIT_PATCH_V1
    + "                    const mediaPrep = WPP.whatsapp?.MediaPrep;\n"
    + "                    if (mediaPrep?.prepRawMedia && !mediaPrep.__winzappAudioTypePatched) {\n"
    + "                        const prepRawMedia = mediaPrep.prepRawMedia.bind(mediaPrep);\n"
    + "                        mediaPrep.prepRawMedia = (opaqueData, prepOptions = {}) => {\n"
    + "                            const opaqueType = typeof opaqueData?.type === 'function'\n"
    + "                                ? opaqueData.type()\n"
    + "                                : opaqueData?.type;\n"
    + "                            return prepRawMedia(opaqueData, String(opaqueType || '').startsWith('audio/')\n"
    + "                                ? { ...prepOptions, isAudio: true }\n"
    + "                                : prepOptions);\n"
    + "                        };\n"
    + "                        mediaPrep.__winzappAudioTypePatched = true;\n"
    + "                    }\n"
)


_BROWSER_ATTACHMENT_LIMIT_PATCH_WITH_WAV_BYPASS = (
    _BROWSER_ATTACHMENT_LIMIT_PATCH_V2
    + "                    const mediaWorker = WPP.loader?.loadModule?.('WAWebSendMessageToMediaWorker');\n"
    + "                    if (mediaWorker?.sendMessageToMediaWorker && !mediaWorker.__winzappWavPassthroughPatched) {\n"
    + "                        const sendMessageToMediaWorker = mediaWorker.sendMessageToMediaWorker.bind(mediaWorker);\n"
    + "                        mediaWorker.sendMessageToMediaWorker = async (message) => {\n"
    + "                            const response = await sendMessageToMediaWorker(message);\n"
    + "                            const file = message?.file;\n"
    + "                            const wavMimes = ['audio/wav', 'audio/x-wav', 'audio/wave', 'audio/vnd.wave'];\n"
    + "                            if (message?.type !== 'prep' || !wavMimes.includes(file?.type)\n"
    + "                                || response?.type !== 'parsingError'\n"
    + "                                || String(response?.error) !== 'File format unsupported') {\n"
    + "                                return response;\n"
    + "                            }\n"
    + "                            const header = new Uint8Array(await file.slice(0, 12).arrayBuffer());\n"
    + "                            const ascii = (offset) => String.fromCharCode(...header.subarray(offset, offset + 4));\n"
    + "                            const riff = ascii(0);\n"
    + "                            if (!['RIFF', 'RIFX', 'RF64'].includes(riff) || ascii(8) !== 'WAVE') {\n"
    + "                                return response;\n"
    + "                            }\n"
    + "                            return {\n"
    + "                                type: 'result',\n"
    + "                                result: { type: 'audio/wav', file, isGif: false },\n"
    + "                            };\n"
    + "                        };\n"
    + "                        mediaWorker.__winzappWavPassthroughPatched = true;\n"
    + "                    }\n"
)

# WAV cannot be made into a valid WhatsApp audio message by bypassing the
# browser's media worker: the upload completes, but WhatsApp rejects the
# resulting message with ACK -1. Keep the old block above only to migrate
# already-patched installations back to the supported MediaPrep path. WAV is
# now converted to OGG/Opus before sendFile() is called.


#: V2 with WhatsApp's real per-type ceilings instead of one flat 1 GB for
#: everything: documents go to 2 GB, photos/videos/audio stay at 1 GB. Every
#: version up to V2 capped documents at 1 GB too, which was never WhatsApp's
#: limit — just this block sharing a single constant across all four types.
#:
#: Derived from V2 by replacing the ceiling rather than written out again, so
#: the two cannot drift: V2 must stay byte-for-byte what shipped, because it is
#: the left-hand side of the migration in patch_sender_layer_source() that
#: raises an already-patched node_modules to 2 GB. (npm install refetches
#: pristine sources, so a reinstall takes the fresh path — but an install
#: patched in place would otherwise sit at 1 GB forever.)
_UPLOAD_LIMIT_CEILING_V2 = (
    "Math.max(getUploadLimit(type, origin, isVcard), 1 * 1024 * 1024 * 1024)"
)
_UPLOAD_LIMIT_CEILING_V3 = (
    "Math.max(getUploadLimit(type, origin, isVcard), "
    "type === 'document' ? 2 * 1024 * 1024 * 1024 : 1 * 1024 * 1024 * 1024)"
)

assert _BROWSER_ATTACHMENT_LIMIT_PATCH_V2.count(_UPLOAD_LIMIT_CEILING_V2) == 1

_BROWSER_ATTACHMENT_LIMIT_PATCH_V3 = _BROWSER_ATTACHMENT_LIMIT_PATCH_V2.replace(
    _UPLOAD_LIMIT_CEILING_V2, _UPLOAD_LIMIT_CEILING_V3
)


_BROWSER_ATTACHMENT_LIMIT_PATCH = _BROWSER_ATTACHMENT_LIMIT_PATCH_V3


def _deepen(block: str) -> str:
    """The same block one nesting level further in (the chunked branch)."""
    return block.replace("                    ", "                        ")


# Exact method briefly shipped with the native-WAV media-worker bypass. It is
# a migration input only; patch_sender_layer_source() removes that bypass.
LEGACY_PATCHED_SEND_FILE_V4 = LEGACY_PATCHED_SEND_FILE_V3.replace(
    _deepen(_BROWSER_DOCUMENT_LIMIT_PATCH),
    _deepen(_BROWSER_ATTACHMENT_LIMIT_PATCH_WITH_WAV_BYPASS),
).replace(
    _BROWSER_DOCUMENT_LIMIT_PATCH,
    _BROWSER_ATTACHMENT_LIMIT_PATCH_WITH_WAV_BYPASS,
)


# Build the current method from the last shipped version so the two copies of
# the page-side override cannot drift apart.  The legacy snapshot above stays
# byte-for-byte exact for migration of existing installations.
PATCHED_SEND_FILE = LEGACY_PATCHED_SEND_FILE_V3.replace(
    _deepen(_BROWSER_DOCUMENT_LIMIT_PATCH),
    _deepen(_BROWSER_ATTACHMENT_LIMIT_PATCH),
).replace(
    _BROWSER_DOCUMENT_LIMIT_PATCH,
    _BROWSER_ATTACHMENT_LIMIT_PATCH,
)


ALL_PATCHES = (
    (ORIGINAL_FILE_LOADING, PATCHED_FILE_LOADING),
    (PATCHED_FILE_LOADING_V1, PATCHED_FILE_LOADING),
    (ORIGINAL_SEND_FILE, PATCHED_SEND_FILE),
    (LEGACY_PATCHED_SEND_FILE, PATCHED_SEND_FILE),
    (LEGACY_PATCHED_SEND_FILE_V2, PATCHED_SEND_FILE),
    (LEGACY_PATCHED_SEND_FILE_V3, PATCHED_SEND_FILE),
    (LEGACY_PATCHED_SEND_FILE_V4, PATCHED_SEND_FILE),
)


def patch_sender_layer_source(source: str) -> str:
    """Apply the current patch and migrate every earlier transitional state."""
    for original, patched in ALL_PATCHES:
        source = source.replace(original, patched)

    # Remove the failed native-WAV experiment even when it appears inside a
    # source version that does not match the whole-method legacy snapshot.
    source = source.replace(
        _deepen(_BROWSER_ATTACHMENT_LIMIT_PATCH_WITH_WAV_BYPASS),
        _deepen(_BROWSER_ATTACHMENT_LIMIT_PATCH),
    )
    source = source.replace(
        _BROWSER_ATTACHMENT_LIMIT_PATCH_WITH_WAV_BYPASS,
        _BROWSER_ATTACHMENT_LIMIT_PATCH,
    )

    # Document-only -> every attachment type, including already-guarded
    # installations, at both nesting depths the block appears at.
    source = source.replace(
        _deepen(_BROWSER_DOCUMENT_LIMIT_PATCH),
        _deepen(_BROWSER_ATTACHMENT_LIMIT_PATCH),
    )
    source = source.replace(
        _BROWSER_DOCUMENT_LIMIT_PATCH,
        _BROWSER_ATTACHMENT_LIMIT_PATCH,
    )
    # Generic 1 GB override shipped before WAV was explicitly marked as audio.
    # The v2 block deliberately contains v1 as its prefix, so only run this
    # migration when its own marker is absent; otherwise replacing that prefix
    # would append the MediaPrep wrapper a second time on every startup.
    if "__winzappAudioTypePatched" not in source:
        source = source.replace(
            _deepen(_BROWSER_ATTACHMENT_LIMIT_PATCH_V1),
            _deepen(_BROWSER_ATTACHMENT_LIMIT_PATCH),
        )
        source = source.replace(
            _BROWSER_ATTACHMENT_LIMIT_PATCH_V1,
            _BROWSER_ATTACHMENT_LIMIT_PATCH,
        )

    # Flat 1 GB for every type -> WhatsApp's real per-type ceilings (2 GB for
    # documents). Runs AFTER the WAV-bypass removal above, which matches on a
    # block that has V2 as its prefix: rewriting the ceiling first would stop
    # that match and strand the bypass in place.
    source = source.replace(
        _deepen(_BROWSER_ATTACHMENT_LIMIT_PATCH_V2),
        _deepen(_BROWSER_ATTACHMENT_LIMIT_PATCH_V3),
    )
    source = source.replace(
        _BROWSER_ATTACHMENT_LIMIT_PATCH_V2, _BROWSER_ATTACHMENT_LIMIT_PATCH_V3
    )

    # Unmarked document-only -> current guarded attachment override.
    source = source.replace(
        _deepen(_LEGACY_DOCUMENT_LIMIT_PATCH), _deepen(_BROWSER_ATTACHMENT_LIMIT_PATCH)
    )
    source = source.replace(
        _LEGACY_DOCUMENT_LIMIT_PATCH, _BROWSER_ATTACHMENT_LIMIT_PATCH
    )

    if "WPP.whatsapp?.MediaGatingUtils" not in source:
        source = source.replace(
            "                    const result = await WPP.chat.sendFileMessage(to, file, {\n",
            _deepen(_BROWSER_ATTACHMENT_LIMIT_PATCH)
            + "                        const result = await WPP.chat.sendFileMessage(to, file, {\n",
        )
        source = source.replace(
            "                    const result = await WPP.chat.sendFileMessage(to, base64, {\n",
            _BROWSER_ATTACHMENT_LIMIT_PATCH
            + "                    const result = await WPP.chat.sendFileMessage(to, base64, {\n",
        )

    intermediate_marker = "        if (base64.length > 8 * 1024 * 1024) {\n"
    if intermediate_marker not in source:
        return source

    marker_index = source.index(intermediate_marker)
    block_start = source.rfind("        let sendResult;\n", 0, marker_index)
    block_end = source.find("\n    }\n    /**", marker_index)
    if block_start < 0 or block_end < 0:
        return source
    return source[:block_start] + PATCHED_SEND_FILE + source[block_end:]
