"""Shared source-text constants for patching @wppconnect-team/wppconnect's
compiled status.layer.js — status (stories) posting always reporting
success regardless of whether it actually worked.

Bug: sendTextStatus()/sendImageStatus()/sendVideoStatus() each call
`evaluateAndReturn(this.page, (args) => { WPP.status.sendXStatus(...); },
args)` — the arrow function passed to Puppeteer's `page.evaluate()` is
neither `async` nor does it `return`/`await` the call to
`WPP.status.sendXStatus(...)`. Puppeteer's `evaluate()` therefore resolves
as soon as that synchronous arrow function returns (`undefined`,
immediately) instead of waiting for the actual post to succeed or fail
inside the page — so the Node-side promise this method returns always
resolves "successfully" (with `undefined`) regardless of whether WhatsApp
actually accepted the status update, and any real failure inside the page
becomes an unhandled promise rejection there that never propagates back.
statusController.ts (WPPConnect Server) then always reports HTTP 200/201 to
WinZapp no matter what actually happened — this is why the "text status
posted" notification never correctly reflected success vs. failure
(WinZapp issue: "a notificação não informa corretamente se foi enviado com
sucesso ou se houve erro").

Every other `evaluateAndReturn()` call in this same vendored package (e.g.
host.layer.js's `loginByCode()`) correctly awaits/returns the underlying
`WPP.*` call — this file's three methods are the exception, not the rule.

Fix: make the evaluate callback `async` and `return await` the call, so
Puppeteer actually waits for and propagates the real result/error.

Both setup_api.py and ApiSetupDialog (client/ui/dialogs/api_setup.py) apply
this patch to node_modules right after every `npm install`, importing the
shared constants from this module instead of hand-duplicating them — see
client/core/wppconnect_host_layer_patch.py's module docstring for why that
sharing matters (a v1->v2 correction there once landed in only one of the
two apply-sites because the text was duplicated).
"""

ORIGINAL_SEND_TEXT_STATUS = (
    "    async sendTextStatus(text, options) {\n"
    "        return await (0, helpers_1.evaluateAndReturn)(this.page, ({ text, options }) => {\n"
    "            WPP.status.sendTextStatus(text, options);\n"
    "        }, { text, options });\n"
    "    }\n"
)
PATCHED_SEND_TEXT_STATUS = (
    "    async sendTextStatus(text, options) {\n"
    "        return await (0, helpers_1.evaluateAndReturn)(this.page, async ({ text, options }) => {\n"
    "            return await WPP.status.sendTextStatus(text, options);\n"
    "        }, { text, options });\n"
    "    }\n"
)

ORIGINAL_SEND_IMAGE_STATUS = (
    "        return await (0, helpers_1.evaluateAndReturn)(this.page, ({ base64, options }) => {\n"
    "            WPP.status.sendImageStatus(base64, options);\n"
    "        }, { base64, options });\n"
)
PATCHED_SEND_IMAGE_STATUS = (
    "        return await (0, helpers_1.evaluateAndReturn)(this.page, async ({ base64, options }) => {\n"
    "            return await WPP.status.sendImageStatus(base64, options);\n"
    "        }, { base64, options });\n"
)

ORIGINAL_SEND_VIDEO_STATUS = (
    "        return await (0, helpers_1.evaluateAndReturn)(this.page, ({ base64, options }) => {\n"
    "            WPP.status.sendVideoStatus(base64, options);\n"
    "        }, { base64, options });\n"
)
PATCHED_SEND_VIDEO_STATUS = (
    "        return await (0, helpers_1.evaluateAndReturn)(this.page, async ({ base64, options }) => {\n"
    "            return await WPP.status.sendVideoStatus(base64, options);\n"
    "        }, { base64, options });\n"
)

# (original_text, patched_text) pairs applied independently — sendImageStatus
# and sendVideoStatus have differently-shaped surrounding code (image
# mimetype validation vs. none) so only their evaluateAndReturn call itself,
# not the whole method, is matched/replaced.
ALL_PATCHES = (
    (ORIGINAL_SEND_TEXT_STATUS, PATCHED_SEND_TEXT_STATUS),
    (ORIGINAL_SEND_IMAGE_STATUS, PATCHED_SEND_IMAGE_STATUS),
    (ORIGINAL_SEND_VIDEO_STATUS, PATCHED_SEND_VIDEO_STATUS),
)


# wppconnect 2.4.0 fixed the missing await itself, but each method now returns
# only {ack, id}. That is not enough: statusController.ts (the WPPConnect
# Server side of WinZapp) reads `sendMsgResult.messageSendResult` from the
# result to tell a status WhatsApp rejected (e.g. "ERROR_UNKNOWN") from one it
# accepted, and with that field gone every rejected status would be answered
# 201 again. So 2.4.0's text is one more thing to rewrite into the same
# patched forms above, which return WPP's whole result, not a shape the patch
# leaves alone. One tuple of alternative originals per ALL_PATCHES entry, in
# the same order.
V240_SEND_TEXT_STATUS = (
    "    async sendTextStatus(text, options) {\n"
    "        return await (0, helpers_1.evaluateAndReturn)(this.page, async ({ text, options }) => {\n"
    "            const result = await WPP.status.sendTextStatus(text, options);\n"
    "            return { ack: result?.ack, id: result?.id };\n"
    "        }, { text, options });\n"
    "    }\n"
)
V240_SEND_IMAGE_STATUS = (
    "        return await (0, helpers_1.evaluateAndReturn)(this.page, async ({ base64, options }) => {\n"
    "            const result = await WPP.status.sendImageStatus(base64, options);\n"
    "            return { ack: result?.ack, id: result?.id };\n"
    "        }, { base64, options });\n"
)
V240_SEND_VIDEO_STATUS = (
    "        return await (0, helpers_1.evaluateAndReturn)(this.page, async ({ base64, options }) => {\n"
    "            const result = await WPP.status.sendVideoStatus(base64, options);\n"
    "            return { ack: result?.ack, id: result?.id };\n"
    "        }, { base64, options });\n"
)
ALTERNATIVE_ORIGINALS = (
    (V240_SEND_TEXT_STATUS,),
    (V240_SEND_IMAGE_STATUS,),
    (V240_SEND_VIDEO_STATUS,),
)
