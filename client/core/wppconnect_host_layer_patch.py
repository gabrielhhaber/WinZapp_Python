"""Shared source-text constants for patching @wppconnect-team/wppconnect's
compiled host.layer.js — the phone-number pairing-code rotation fix
(WinZapp issue #8).

Both setup_api.py (repo root, the developer/CI setup script) and
ApiSetupDialog (client/ui/dialogs/api_setup.py, the real end-user install
flow) need to apply the exact same patch to node_modules right after every
`npm install` — see either call site's own docstring for why this can't go
through the normal api_patches/ mechanism. They used to each carry their
own hand-duplicated copy of these strings; when the patch needed a
correction (v1 -> v2, see below) only one of the two copies actually got
fixed here, which is exactly the kind of drift this shared module exists to
rule out going forward. This module has zero dependencies beyond the
standard library so it's safe for setup_api.py to import via a sys.path
insert of client/ without pulling in wx or any other heavy client code.

History:

* v0 — the original upstream bug (wppconnect-team/wppconnect#2836):
  checkQrCode() is bound to WhatsApp's own QR rotation (`conn.auth_code_change`)
  and dedupes the QR-image branch against `this.urlCode` before re-emitting
  it, but the phoneNumber (pairing-code) branch returns straight into
  loginByCode() with no equivalent guard — so every ~20-60s QR rotation
  regenerates a BRAND NEW pairing code, faster than a screen-reader user can
  read an 8-character code.

* v1 — WinZapp's first fix attempt (shipped, then found unsafe): a
  `linkCodeGenerated` latch set to True BEFORE loginByCode() actually
  produced a code, cleared only on a successful login. If loginByCode()
  ever rejected, or if a legitimately-issued code needed a later refresh,
  the latch never got reset — the displayed code silently froze forever.
  Reported live: "esperei 10 minutos e o código não atualizou nenhuma vez."

* v6 — both pairing routes were dead at once, for two unrelated reasons that
  presented identically ("nothing ever appears on screen"), which is why this
  took a full instrumented bisect rather than a reading of the code.

  The pairing code hit `Invariant Violation: Minified invariant #56367`
  inside WhatsApp Web. Cause: v1..v5 hoisted the phoneNumber branch above
  `await this.getQrCode()` to fix the rotation problem in v0, and that line
  was load-bearing for a reason nobody had written down — reaching
  loginByCode() only after a urlCode existed also guaranteed WhatsApp Web's
  user-prefs storage was initialised. Without it, wa-js walks setADVSecretKey
  -> allUserPrefsIdb -> getUserPrefsTable -> getStorage into an uninitialised
  table. v6 restores the gate explicitly. See PATCHED_CHECK_QR_CODE for the
  three measured timings that isolate it.

  The QR emitted no event at all, because upstream's scrapeImg() reads the DOM
  and current WhatsApp Web no longer renders the QR into a <canvas> whose
  nearest data-ref ancestor is the code. v6 reads WPP.conn.getAuthCode()
  instead and renders the PNG itself. See PATCHED_GET_QR_CODE.

  Neither failure is a WinZapp regression in the ordinary sense — WhatsApp Web
  changed under a DOM scraper, and the storage gate was dropped five patches
  earlier without symptom until WhatsApp started asserting on it. Both are
  worth keeping in mind the next time a patch here "simplifies" an upstream
  ordering: the ordering may be the guard.

* v9 — wppconnect 2.3.2 restructured the pairing flow, so there are now TWO
  patch sets in this module and patch_host_layer_source() picks between them
  by reading the file (MANAGED_LINK_MARKER). Everything above stays exactly
  as it shipped, for the installs whose node_modules is still on 2.3.1:
  _apply_node_modules_patches() runs on every launch against whatever is on
  disk, so a WinZapp update alone never moves node_modules, and applying the
  2.3.2 text to a 2.3.1 file would strip the only call site that runtime has
  for loginByCode() — pairing by code, dead, silently. The two sets are told
  apart by matching, not by trusting a version string nothing here can see.

  What 2.3.2 changed, and what survives of v8 because of it:

  * checkQrCode() no longer forwards to loginByCode() at all, and is not even
    registered on `conn.auth_code_change` when a phoneNumber is set —
    afterPageScriptInjected() branches, subscribing the link-code events and
    calling loginByCode() exactly once. The mint loop v2..v8 paced with a
    reuse cooldown therefore does not exist any more, and neither does the
    unattended stream it produced: wa-js's own linkDeviceCodeLifecycle now
    reuses the active code for repeat calls and re-mints on a 195s timer,
    bounded at 5 refreshes. So the cooldown is REMOVED rather than ported —
    there is no longer anything for it to pace, and a cooldown wrapped around
    a call that no longer happens is just text that reads like a guard.
    v7's auth-probe fix is not about the mint loop and is ported unchanged:
    `!null` is still `true`, and checkQrCode() still runs on every auth-code
    rotation in QR mode.

  * loginByCode() lost the gate that made it safe. 2.3.1's upstream called it
    only after getQrCode() had returned a urlCode; 2.3.2 calls it straight
    after injection, which is the exact condition v6 measured as `Invariant
    Violation #56367` (see V6_CHECK_QR_CODE's comment — the auth state has to
    exist first). Worse, nothing re-invokes it: upstream's single call is
    `.catch(error => this.log('error', error))`, so one failure ends pairing
    for that session with a line in wppconnect.log and nothing on screen. The
    gate, the error extraction and the catchLinkCodeError reporting therefore
    all move INTO loginByCode(), which also grows the bounded retry v8 used
    to get for free from the auth-code rotation.

  * the code itself now arrives at onLinkCode(), fired from
    `conn.link_code_change`, and loginByCode()'s own resolved value is a
    second copy of the same code — hence the dedup in onLinkCode(), which is
    what lets both routes stay live without announcing the code twice.

  * every failure after the first mint arrives at onLinkCodeError(), and
    upstream hands that hook `error.message` and nothing else. wa-js emits
    `conn.link_code_error` through
    `e instanceof Error ? e : new Error(String(e))`, so WhatsApp's own answer
    — the `IQErrorRateOverlimit` / `rate-overlimit` that decides whether the
    user is told to retry or to wait — lives in the error's *own properties*,
    never in `.message`. The page-side listener therefore serialises the error
    the same way loginByCode()'s catch already does, and the hook classifies
    it into the same `rateLimited` + `details` envelope. See
    MANAGED_PATCHED_LINK_CODE_LISTENER.

"""

ORIGINAL_CHECK_QR_CODE = (
    "    async checkQrCode() {\n"
    "        const needScan = await (0, auth_1.needsToScan)(this.page).catch(() => null);\n"
    "        this.isLogged = !needScan;\n"
    "        if (!needScan) {\n"
    "            this.attempt = 0;\n"
    "            return;\n"
    "        }\n"
    "        const result = await this.getQrCode();\n"
    "        if (!result?.urlCode || this.urlCode === result.urlCode) {\n"
    "            return;\n"
    "        }\n"
    "        if (typeof this.options.phoneNumber === 'string') {\n"
    "            return this.loginByCode(this.options.phoneNumber);\n"
    "        }\n"
    "        this.urlCode = result.urlCode;\n"
    "        this.attempt++;\n"
    "        let qr = '';\n"
    "        if (this.options.logQR || this.catchQR) {\n"
    "            qr = await (0, auth_1.asciiQr)(this.urlCode);\n"
    "        }\n"
    "        if (this.options.logQR) {\n"
    "            this.log('info', `Waiting for QRCode Scan (Attempt ${this.attempt})...:\\n${qr}`, { code: this.urlCode });\n"
    "        }\n"
    "        else {\n"
    "            this.log('verbose', `Waiting for QRCode Scan: Attempt ${this.attempt}`);\n"
    "        }\n"
    "        this.catchQR?.(result.base64Image, qr, this.attempt, result.urlCode);\n"
    "    }\n"
)

V1_CHECK_QR_CODE = (
    "    async checkQrCode() {\n"
    "        const needScan = await (0, auth_1.needsToScan)(this.page).catch(() => null);\n"
    "        this.isLogged = !needScan;\n"
    "        if (!needScan) {\n"
    "            this.attempt = 0;\n"
    "            this.linkCodeGenerated = false;\n"
    "            return;\n"
    "        }\n"
    "        const result = await this.getQrCode();\n"
    "        if (!result?.urlCode || this.urlCode === result.urlCode) {\n"
    "            return;\n"
    "        }\n"
    "        if (typeof this.options.phoneNumber === 'string') {\n"
    "            if (this.linkCodeGenerated) {\n"
    "                return;\n"
    "            }\n"
    "            this.linkCodeGenerated = true;\n"
    "            return this.loginByCode(this.options.phoneNumber);\n"
    "        }\n"
    "        this.urlCode = result.urlCode;\n"
    "        this.attempt++;\n"
    "        let qr = '';\n"
    "        if (this.options.logQR || this.catchQR) {\n"
    "            qr = await (0, auth_1.asciiQr)(this.urlCode);\n"
    "        }\n"
    "        if (this.options.logQR) {\n"
    "            this.log('info', `Waiting for QRCode Scan (Attempt ${this.attempt})...:\\n${qr}`, { code: this.urlCode });\n"
    "        }\n"
    "        else {\n"
    "            this.log('verbose', `Waiting for QRCode Scan: Attempt ${this.attempt}`);\n"
    "        }\n"
    "        this.catchQR?.(result.base64Image, qr, this.attempt, result.urlCode);\n"
    "    }\n"
)

V2_CHECK_QR_CODE = (
    "    async checkQrCode() {\n"
    "        const needScan = await (0, auth_1.needsToScan)(this.page).catch(() => null);\n"
    "        this.isLogged = !needScan;\n"
    "        if (!needScan) {\n"
    "            this.attempt = 0;\n"
    "            this.linkCodeIssuedAt = 0;\n"
    "            return;\n"
    "        }\n"
    "        if (typeof this.options.phoneNumber === 'string') {\n"
    "            if (this.linkCodeInFlight) {\n"
    "                return;\n"
    "            }\n"
    "            const now = Date.now();\n"
    "            if (this.linkCodeIssuedAt && (now - this.linkCodeIssuedAt) < 60000) {\n"
    "                return;\n"
    "            }\n"
    "            this.linkCodeInFlight = true;\n"
    "            try {\n"
    "                await this.loginByCode(this.options.phoneNumber);\n"
    "                this.linkCodeIssuedAt = Date.now();\n"
    "            }\n"
    "            finally {\n"
    "                this.linkCodeInFlight = false;\n"
    "            }\n"
    "            return;\n"
    "        }\n"
    "        const result = await this.getQrCode();\n"
    "        if (!result?.urlCode || this.urlCode === result.urlCode) {\n"
    "            return;\n"
    "        }\n"
    "        this.urlCode = result.urlCode;\n"
    "        this.attempt++;\n"
    "        let qr = '';\n"
    "        if (this.options.logQR || this.catchQR) {\n"
    "            qr = await (0, auth_1.asciiQr)(this.urlCode);\n"
    "        }\n"
    "        if (this.options.logQR) {\n"
    "            this.log('info', `Waiting for QRCode Scan (Attempt ${this.attempt})...:\\n${qr}`, { code: this.urlCode });\n"
    "        }\n"
    "        else {\n"
    "            this.log('verbose', `Waiting for QRCode Scan: Attempt ${this.attempt}`);\n"
    "        }\n"
    "        this.catchQR?.(result.base64Image, qr, this.attempt, result.urlCode);\n"
    "    }\n"
)

V3_CHECK_QR_CODE = (
    "    async checkQrCode() {\n"
    "        const needScan = await (0, auth_1.needsToScan)(this.page).catch(() => null);\n"
    "        this.isLogged = !needScan;\n"
    "        if (!needScan) {\n"
    "            this.attempt = 0;\n"
    "            this.linkCodeIssuedAt = 0;\n"
    "            return;\n"
    "        }\n"
    "        if (typeof this.options.phoneNumber === 'string') {\n"
    "            if (this.linkCodeInFlight) {\n"
    "                return;\n"
    "            }\n"
    "            const now = Date.now();\n"
    "            if (this.linkCodeIssuedAt && (now - this.linkCodeIssuedAt) < 60000) {\n"
    "                return;\n"
    "            }\n"
    "            this.linkCodeInFlight = true;\n"
    "            try {\n"
    "                await this.loginByCode(this.options.phoneNumber);\n"
    "                this.linkCodeIssuedAt = Date.now();\n"
    "            }\n"
    "            catch (error) {\n"
    "                this.log('error', `Could not generate the pairing code: ${error?.name || 'Error'}: ${error?.message || error}`);\n"
    "            }\n"
    "            finally {\n"
    "                this.linkCodeInFlight = false;\n"
    "            }\n"
    "            return;\n"
    "        }\n"
    "        const result = await this.getQrCode();\n"
    "        if (!result?.urlCode || this.urlCode === result.urlCode) {\n"
    "            return;\n"
    "        }\n"
    "        this.urlCode = result.urlCode;\n"
    "        this.attempt++;\n"
    "        let qr = '';\n"
    "        if (this.options.logQR || this.catchQR) {\n"
    "            qr = await (0, auth_1.asciiQr)(this.urlCode);\n"
    "        }\n"
    "        if (this.options.logQR) {\n"
    "            this.log('info', `Waiting for QRCode Scan (Attempt ${this.attempt})...:\\n${qr}`, { code: this.urlCode });\n"
    "        }\n"
    "        else {\n"
    "            this.log('verbose', `Waiting for QRCode Scan: Attempt ${this.attempt}`);\n"
    "        }\n"
    "        this.catchQR?.(result.base64Image, qr, this.attempt, result.urlCode);\n"
    "    }\n"
)


V4_CHECK_QR_CODE = (
    "    async checkQrCode() {\n"
    "        const needScan = await (0, auth_1.needsToScan)(this.page).catch(() => null);\n"
    "        this.isLogged = !needScan;\n"
    "        if (!needScan) {\n"
    "            this.attempt = 0;\n"
    "            this.linkCodeIssuedAt = 0;\n"
    "            return;\n"
    "        }\n"
    "        if (typeof this.options.phoneNumber === 'string') {\n"
    "            if (this.linkCodeInFlight) {\n"
    "                return;\n"
    "            }\n"
    "            const now = Date.now();\n"
    "            if (this.linkCodeIssuedAt && (now - this.linkCodeIssuedAt) < 60000) {\n"
    "                return;\n"
    "            }\n"
    "            this.linkCodeInFlight = true;\n"
    "            try {\n"
    "                await this.loginByCode(this.options.phoneNumber);\n"
    "                this.linkCodeIssuedAt = Date.now();\n"
    "            }\n"
    "            catch (error) {\n"
    "                this.log('error', `Could not generate the pairing code: ${error?.name || 'Error'}: ${error?.message || error}`);\n"
    "                this.options.catchLinkCodeError?.({\n"
    "                    name: String(error?.name || 'Error'),\n"
    "                    message: String(error?.message || error),\n"
    "                    session: this.session,\n"
    "                });\n"
    "            }\n"
    "            finally {\n"
    "                this.linkCodeInFlight = false;\n"
    "            }\n"
    "            return;\n"
    "        }\n"
    "        const result = await this.getQrCode();\n"
    "        if (!result?.urlCode || this.urlCode === result.urlCode) {\n"
    "            return;\n"
    "        }\n"
    "        this.urlCode = result.urlCode;\n"
    "        this.attempt++;\n"
    "        let qr = '';\n"
    "        if (this.options.logQR || this.catchQR) {\n"
    "            qr = await (0, auth_1.asciiQr)(this.urlCode);\n"
    "        }\n"
    "        if (this.options.logQR) {\n"
    "            this.log('info', `Waiting for QRCode Scan (Attempt ${this.attempt})...:\\n${qr}`, { code: this.urlCode });\n"
    "        }\n"
    "        else {\n"
    "            this.log('verbose', `Waiting for QRCode Scan: Attempt ${this.attempt}`);\n"
    "        }\n"
    "        this.catchQR?.(result.base64Image, qr, this.attempt, result.urlCode);\n"
    "    }\n"
)


V5_CHECK_QR_CODE = (
    "    async checkQrCode() {\n"
    "        const needScan = await (0, auth_1.needsToScan)(this.page).catch(() => null);\n"
    "        this.isLogged = !needScan;\n"
    "        if (!needScan) {\n"
    "            this.attempt = 0;\n"
    "            this.linkCodeIssuedAt = 0;\n"
    "            this.linkCodeFailures = 0;\n"
    "            this.linkCodeRetryAfter = 0;\n"
    "            return;\n"
    "        }\n"
    "        if (typeof this.options.phoneNumber === 'string') {\n"
    "            if (this.linkCodeInFlight) {\n"
    "                return;\n"
    "            }\n"
    "            const now = Date.now();\n"
    "            if (this.linkCodeIssuedAt && (now - this.linkCodeIssuedAt) < 60000) {\n"
    "                return;\n"
    "            }\n"
    "            if (this.linkCodeRetryAfter && now < this.linkCodeRetryAfter) {\n"
    "                return;\n"
    "            }\n"
    "            this.linkCodeInFlight = true;\n"
    "            try {\n"
    "                await this.loginByCode(this.options.phoneNumber);\n"
    "                this.linkCodeIssuedAt = Date.now();\n"
    "                this.linkCodeFailures = 0;\n"
    "                this.linkCodeRetryAfter = 0;\n"
    "            }\n"
    "            catch (error) {\n"
    "                this.linkCodeFailures = (this.linkCodeFailures || 0) + 1;\n"
    "                const backoff = Math.min(20000 * Math.pow(2, this.linkCodeFailures - 1), 300000);\n"
    "                this.linkCodeRetryAfter = Date.now() + backoff;\n"
    "                const retryInSeconds = Math.round(backoff / 1000);\n"
    "                this.log('error', `Could not generate the pairing code (attempt ${this.linkCodeFailures}, next retry in ${retryInSeconds}s): ${error?.name || 'Error'}: ${error?.message || error}`);\n"
    "                this.options.catchLinkCodeError?.({\n"
    "                    name: String(error?.name || 'Error'),\n"
    "                    message: String(error?.message || error),\n"
    "                    session: this.session,\n"
    "                    attempt: this.linkCodeFailures,\n"
    "                    retryInSeconds: retryInSeconds,\n"
    "                    stack: String(error?.stack || ''),\n"
    "                    details: error?.winzappDetails || {},\n"
    "                });\n"
    "            }\n"
    "            finally {\n"
    "                this.linkCodeInFlight = false;\n"
    "            }\n"
    "            return;\n"
    "        }\n"
    "        const result = await this.getQrCode();\n"
    "        if (!result?.urlCode || this.urlCode === result.urlCode) {\n"
    "            return;\n"
    "        }\n"
    "        this.urlCode = result.urlCode;\n"
    "        this.attempt++;\n"
    "        let qr = '';\n"
    "        if (this.options.logQR || this.catchQR) {\n"
    "            qr = await (0, auth_1.asciiQr)(this.urlCode);\n"
    "        }\n"
    "        if (this.options.logQR) {\n"
    "            this.log('info', `Waiting for QRCode Scan (Attempt ${this.attempt})...:\\n${qr}`, { code: this.urlCode });\n"
    "        }\n"
    "        else {\n"
    "            this.log('verbose', `Waiting for QRCode Scan: Attempt ${this.attempt}`);\n"
    "        }\n"
    "        this.catchQR?.(result.base64Image, qr, this.attempt, result.urlCode);\n"
    "    }\n"
)


# v6 — the phoneNumber branch waits for the auth state to exist before it
# calls into the link-device API.
#
# v1..v5 all hoisted this branch ABOVE the `await this.getQrCode()` line to fix
# the rotation problem, and in doing so silently dropped the only thing that
# made the call safe. Upstream reaches loginByCode() only after getQrCode()
# has returned a urlCode — i.e. only once WhatsApp Web has an auth code, which
# means its user-prefs storage is initialised. Calling the link-device API
# before that point makes wa-js walk setADVSecretKey -> allUserPrefsIdb ->
# getUserPrefsTable -> getStorage into an uninitialised table, and WhatsApp
# Web throws `Invariant Violation: Minified invariant #56367`. Measured
# directly against the pinned build, one variable at a time:
#
#   WPP present, not isReady yet   -> TypeError: Cannot read properties of
#                                     undefined (reading 'm')
#   isReady, auth code not yet up  -> Invariant Violation #56367   <-- shipped
#   auth code available            -> no invariant
#
# So the gate is restored, but expressed against the rewritten getQrCode()
# below, which reads WPP.conn.getAuthCode() instead of scraping the DOM. That
# makes it a cheap, side-effect-free readiness probe (verified: gating on it
# and merely sleeping the same duration produce the identical outcome, so
# reading the auth code does not itself disturb the link-device flow), and it
# keeps the v5 cooldown/backoff bookkeeping untouched.
V6_CHECK_QR_CODE = (
    "    async checkQrCode() {\n"
    "        const needScan = await (0, auth_1.needsToScan)(this.page).catch(() => null);\n"
    "        this.isLogged = !needScan;\n"
    "        if (!needScan) {\n"
    "            this.attempt = 0;\n"
    "            this.linkCodeIssuedAt = 0;\n"
    "            this.linkCodeFailures = 0;\n"
    "            this.linkCodeRetryAfter = 0;\n"
    "            return;\n"
    "        }\n"
    "        if (typeof this.options.phoneNumber === 'string') {\n"
    "            if (this.linkCodeInFlight) {\n"
    "                return;\n"
    "            }\n"
    "            const now = Date.now();\n"
    "            if (this.linkCodeIssuedAt && (now - this.linkCodeIssuedAt) < 60000) {\n"
    "                return;\n"
    "            }\n"
    "            if (this.linkCodeRetryAfter && now < this.linkCodeRetryAfter) {\n"
    "                return;\n"
    "            }\n"
    "            const ready = await this.getQrCode();\n"
    "            if (!ready?.urlCode) {\n"
    "                this.log('verbose', 'Auth state not ready yet — deferring the pairing code.');\n"
    "                return;\n"
    "            }\n"
    "            this.linkCodeInFlight = true;\n"
    "            try {\n"
    "                await this.loginByCode(this.options.phoneNumber);\n"
    "                this.linkCodeIssuedAt = Date.now();\n"
    "                this.linkCodeFailures = 0;\n"
    "                this.linkCodeRetryAfter = 0;\n"
    "            }\n"
    "            catch (error) {\n"
    "                this.linkCodeFailures = (this.linkCodeFailures || 0) + 1;\n"
    "                const backoff = Math.min(20000 * Math.pow(2, this.linkCodeFailures - 1), 300000);\n"
    "                this.linkCodeRetryAfter = Date.now() + backoff;\n"
    "                const retryInSeconds = Math.round(backoff / 1000);\n"
    "                this.log('error', `Could not generate the pairing code (attempt ${this.linkCodeFailures}, next retry in ${retryInSeconds}s): ${error?.name || 'Error'}: ${error?.message || error}`);\n"
    "                this.options.catchLinkCodeError?.({\n"
    "                    name: String(error?.name || 'Error'),\n"
    "                    message: String(error?.message || error),\n"
    "                    session: this.session,\n"
    "                    attempt: this.linkCodeFailures,\n"
    "                    retryInSeconds: retryInSeconds,\n"
    "                    stack: String(error?.stack || ''),\n"
    "                    details: error?.winzappDetails || {},\n"
    "                });\n"
    "            }\n"
    "            finally {\n"
    "                this.linkCodeInFlight = false;\n"
    "            }\n"
    "            return;\n"
    "        }\n"
    "        const result = await this.getQrCode();\n"
    "        if (!result?.urlCode || this.urlCode === result.urlCode) {\n"
    "            return;\n"
    "        }\n"
    "        this.urlCode = result.urlCode;\n"
    "        this.attempt++;\n"
    "        let qr = '';\n"
    "        if (this.options.logQR || this.catchQR) {\n"
    "            qr = await (0, auth_1.asciiQr)(this.urlCode);\n"
    "        }\n"
    "        if (this.options.logQR) {\n"
    "            this.log('info', `Waiting for QRCode Scan (Attempt ${this.attempt})...:\\n${qr}`, { code: this.urlCode });\n"
    "        }\n"
    "        else {\n"
    "            this.log('verbose', `Waiting for QRCode Scan: Attempt ${this.attempt}`);\n"
    "        }\n"
    "        this.catchQR?.(result.base64Image, qr, this.attempt, result.urlCode);\n"
    "    }\n"
)


# v7 - checkQrCode must not tell the same lie waitForQrCodeScan just stopped
# telling. Its first two lines were still
#
#     const needScan = await needsToScan(this.page).catch(() => null);
#     this.isLogged = !needScan;
#
# and `!null` is `true`. checkQrCode is invoked from the page on every
# `conn.auth_code_change`, so it runs CONCURRENTLY with waitForQrCodeScan: one
# failed probe here sets isLogged, that loop's `while (!this.isLogged)` exits on
# its next check, waitForLogin re-probes, gets null, and reports
# `Failed to authenticate` - the same symptom, through the door left open. Not
# theoretical: the navigation loop this patch series was written against throws
# "Execution context was destroyed" through this exact call every few seconds.
#
# A probe that could not answer leaves isLogged alone and returns; the next
# auth-code rotation re-enters for free.
V7_CHECK_QR_CODE = (
    "    async checkQrCode() {\n"
    "        let needScan;\n"
    "        try {\n"
    "            needScan = await (0, auth_1.needsToScan)(this.page);\n"
    "        }\n"
    "        catch (error) {\n"
    "            this.log('verbose', `Auth probe failed inside checkQrCode - leaving isLogged untouched: ${error?.name || 'Error'}: ${error?.message || error}`);\n"
    "            return;\n"
    "        }\n"
    "        this.isLogged = !needScan;\n"
    "        if (!needScan) {\n"
    "            this.attempt = 0;\n"
    "            this.linkCodeIssuedAt = 0;\n"
    "            this.linkCodeFailures = 0;\n"
    "            this.linkCodeRetryAfter = 0;\n"
    "            return;\n"
    "        }\n"
    "        if (typeof this.options.phoneNumber === 'string') {\n"
    "            if (this.linkCodeInFlight) {\n"
    "                return;\n"
    "            }\n"
    "            const now = Date.now();\n"
    "            if (this.linkCodeIssuedAt && (now - this.linkCodeIssuedAt) < 60000) {\n"
    "                return;\n"
    "            }\n"
    "            if (this.linkCodeRetryAfter && now < this.linkCodeRetryAfter) {\n"
    "                return;\n"
    "            }\n"
    "            const ready = await this.getQrCode();\n"
    "            if (!ready?.urlCode) {\n"
    "                this.log('verbose', 'Auth state not ready yet — deferring the pairing code.');\n"
    "                return;\n"
    "            }\n"
    "            this.linkCodeInFlight = true;\n"
    "            try {\n"
    "                await this.loginByCode(this.options.phoneNumber);\n"
    "                this.linkCodeIssuedAt = Date.now();\n"
    "                this.linkCodeFailures = 0;\n"
    "                this.linkCodeRetryAfter = 0;\n"
    "            }\n"
    "            catch (error) {\n"
    "                this.linkCodeFailures = (this.linkCodeFailures || 0) + 1;\n"
    "                const backoff = Math.min(20000 * Math.pow(2, this.linkCodeFailures - 1), 300000);\n"
    "                this.linkCodeRetryAfter = Date.now() + backoff;\n"
    "                const retryInSeconds = Math.round(backoff / 1000);\n"
    "                this.log('error', `Could not generate the pairing code (attempt ${this.linkCodeFailures}, next retry in ${retryInSeconds}s): ${error?.name || 'Error'}: ${error?.message || error}`);\n"
    "                this.options.catchLinkCodeError?.({\n"
    "                    name: String(error?.name || 'Error'),\n"
    "                    message: String(error?.message || error),\n"
    "                    session: this.session,\n"
    "                    attempt: this.linkCodeFailures,\n"
    "                    retryInSeconds: retryInSeconds,\n"
    "                    stack: String(error?.stack || ''),\n"
    "                    details: error?.winzappDetails || {},\n"
    "                });\n"
    "            }\n"
    "            finally {\n"
    "                this.linkCodeInFlight = false;\n"
    "            }\n"
    "            return;\n"
    "        }\n"
    "        const result = await this.getQrCode();\n"
    "        if (!result?.urlCode || this.urlCode === result.urlCode) {\n"
    "            return;\n"
    "        }\n"
    "        this.urlCode = result.urlCode;\n"
    "        this.attempt++;\n"
    "        let qr = '';\n"
    "        if (this.options.logQR || this.catchQR) {\n"
    "            qr = await (0, auth_1.asciiQr)(this.urlCode);\n"
    "        }\n"
    "        if (this.options.logQR) {\n"
    "            this.log('info', `Waiting for QRCode Scan (Attempt ${this.attempt})...:\\n${qr}`, { code: this.urlCode });\n"
    "        }\n"
    "        else {\n"
    "            this.log('verbose', `Waiting for QRCode Scan: Attempt ${this.attempt}`);\n"
    "        }\n"
    "        this.catchQR?.(result.base64Image, qr, this.attempt, result.urlCode);\n"
    "    }\n"
)


# v8 — stop burning WhatsApp's per-number pairing-code quota while nobody is
# looking, and treat a rate-limit answer as a rate limit.
#
# checkQrCode() runs on every `conn.auth_code_change`, which WhatsApp Web fires
# roughly once a minute for as long as the page is unpaired. v2..v7 paced that
# with a flat 60s reuse cooldown, which is right while somebody is actually
# reading the code off the screen and wrong the moment nobody is: a session
# that WhatsApp logged out of, and that WinZapp leaves running, keeps asking
# for a fresh pairing code once a minute, forever, unattended.
#
# Straight out of a real wppconnect.log, one stranded session over 18 minutes:
#
#   20:50:44 phoneCode TT8C58TB     20:55:34 phoneCode V2CAPF4B (re-emit)
#   20:54:14 phoneCode V2CAPF4B     20:57:45 phoneCode YV74XX5Z
#   20:59:05 / 21:00:05 re-emits    21:01:16 phoneCode ZHL8WQ9H
#   21:02:36 re-emit                21:04:47 CompanionHelloError
#     details: {"name":"IQErrorRateOverlimit","value":{"text":"rate-overlimit","code":429}}
#
# The quota is per phone number and lives on WhatsApp's side, so it outlives
# both the session and the process. That is the whole of the "the FIRST pairing
# code after a dropped session always fails, the second one works" report: by
# the time the user asks for a code, the background loop has already spent the
# allowance; the failure dialog appears; the user tries again a minute later,
# by which point the window has moved on.
#
# Two changes, both local to this method:
#
#   * the reuse cooldown grows with the number of codes issued that nobody
#     paired with — 60s, 60s, 2min, then a 4min ceiling. The first two
#     reissues stay fast, because those are the attended ones (a mistyped or
#     expired code while the dialog is open); after that the loop goes quiet
#     without ever stopping outright, so a code always eventually refreshes for
#     a user who is still waiting. Reset by a successful pairing.
#
#     The ceiling is 4 minutes and not longer ON PURPOSE, and it is the one
#     number here that must not be raised casually. checkQrCode() is also the
#     only thing that ever refreshes the code shown in the pairing dialog
#     (on_wpp_phone_code -> Connect.update_pairing_code), and in the captured
#     log WhatsApp handed out a genuinely new code roughly every 3.5 minutes,
#     re-emitting the same one in between. A ceiling above that means a user
#     sitting in front of the dialog is shown a code WhatsApp has already
#     rotated past, with nothing on screen saying so — a 15min ceiling would
#     leave a blind user typing a dead code for up to twelve minutes, in the
#     exact flow that is their only way back into the app. 4min keeps the
#     displayed code at most one WhatsApp rotation stale while still cutting
#     the unattended request rate from one a minute to one every four.
#   * a rate-limited failure backs off for 15 minutes instead of the generic
#     20s/40s/80s ladder. Retrying a 429 sooner is what keeps it alive, and the
#     ladder's 20s first step is far shorter than the window WhatsApp is
#     enforcing.
#
# This reduces the burn rate; it does not remove the cause. The session only
# keeps asking because WinZapp leaves a logged-out session running with
# `phoneNumber` still set — closing it when nobody is pairing is the complete
# fix and is deliberately not attempted here.
PATCHED_CHECK_QR_CODE = (
    "    async checkQrCode() {\n"
    "        let needScan;\n"
    "        try {\n"
    "            needScan = await (0, auth_1.needsToScan)(this.page);\n"
    "        }\n"
    "        catch (error) {\n"
    "            this.log('verbose', `Auth probe failed inside checkQrCode - leaving isLogged untouched: ${error?.name || 'Error'}: ${error?.message || error}`);\n"
    "            return;\n"
    "        }\n"
    "        this.isLogged = !needScan;\n"
    "        if (!needScan) {\n"
    "            this.attempt = 0;\n"
    "            this.linkCodeIssuedAt = 0;\n"
    "            this.linkCodeIssues = 0;\n"
    "            this.linkCodeFailures = 0;\n"
    "            this.linkCodeRetryAfter = 0;\n"
    "            return;\n"
    "        }\n"
    "        if (typeof this.options.phoneNumber === 'string') {\n"
    "            if (this.linkCodeInFlight) {\n"
    "                return;\n"
    "            }\n"
    "            const now = Date.now();\n"
    "            const issued = this.linkCodeIssues || 0;\n"
    "            const reuseWindow = Math.min(60000 * Math.pow(2, Math.max(0, issued - 1)), 240000);\n"
    "            if (this.linkCodeIssuedAt && (now - this.linkCodeIssuedAt) < reuseWindow) {\n"
    "                return;\n"
    "            }\n"
    "            if (this.linkCodeRetryAfter && now < this.linkCodeRetryAfter) {\n"
    "                return;\n"
    "            }\n"
    "            const ready = await this.getQrCode();\n"
    "            if (!ready?.urlCode) {\n"
    "                this.log('verbose', 'Auth state not ready yet — deferring the pairing code.');\n"
    "                return;\n"
    "            }\n"
    "            this.linkCodeInFlight = true;\n"
    "            try {\n"
    "                await this.loginByCode(this.options.phoneNumber);\n"
    "                this.linkCodeIssuedAt = Date.now();\n"
    "                this.linkCodeIssues = issued + 1;\n"
    "                this.linkCodeFailures = 0;\n"
    "                this.linkCodeRetryAfter = 0;\n"
    "            }\n"
    "            catch (error) {\n"
    "                this.linkCodeFailures = (this.linkCodeFailures || 0) + 1;\n"
    "                let detail = '';\n"
    "                try {\n"
    "                    detail = JSON.stringify(error?.winzappDetails || {});\n"
    "                }\n"
    "                catch (e) {\n"
    "                    detail = '';\n"
    "                }\n"
    "                const rateLimited = /rate-overlimit|RateOverlimit/i.test(`${detail} ${error?.name || ''} ${error?.message || ''}`);\n"
    "                const backoff = rateLimited\n"
    "                    ? 900000\n"
    "                    : Math.min(20000 * Math.pow(2, this.linkCodeFailures - 1), 300000);\n"
    "                this.linkCodeRetryAfter = Date.now() + backoff;\n"
    "                const retryInSeconds = Math.round(backoff / 1000);\n"
    "                this.log('error', `Could not generate the pairing code (attempt ${this.linkCodeFailures}${rateLimited ? ', rate-limited by WhatsApp' : ''}, next retry in ${retryInSeconds}s): ${error?.name || 'Error'}: ${error?.message || error}`);\n"
    "                this.options.catchLinkCodeError?.({\n"
    "                    name: String(error?.name || 'Error'),\n"
    "                    message: String(error?.message || error),\n"
    "                    session: this.session,\n"
    "                    attempt: this.linkCodeFailures,\n"
    "                    retryInSeconds: retryInSeconds,\n"
    "                    rateLimited: rateLimited,\n"
    "                    stack: String(error?.stack || ''),\n"
    "                    details: error?.winzappDetails || {},\n"
    "                });\n"
    "            }\n"
    "            finally {\n"
    "                this.linkCodeInFlight = false;\n"
    "            }\n"
    "            return;\n"
    "        }\n"
    "        const result = await this.getQrCode();\n"
    "        if (!result?.urlCode || this.urlCode === result.urlCode) {\n"
    "            return;\n"
    "        }\n"
    "        this.urlCode = result.urlCode;\n"
    "        this.attempt++;\n"
    "        let qr = '';\n"
    "        if (this.options.logQR || this.catchQR) {\n"
    "            qr = await (0, auth_1.asciiQr)(this.urlCode);\n"
    "        }\n"
    "        if (this.options.logQR) {\n"
    "            this.log('info', `Waiting for QRCode Scan (Attempt ${this.attempt})...:\\n${qr}`, { code: this.urlCode });\n"
    "        }\n"
    "        else {\n"
    "            this.log('verbose', `Waiting for QRCode Scan: Attempt ${this.attempt}`);\n"
    "        }\n"
    "        this.catchQR?.(result.base64Image, qr, this.attempt, result.urlCode);\n"
    "    }\n"
)


# getQrCode() — read the auth code from wa-js instead of scraping the DOM.
#
# Upstream's helper walks `document.querySelector('canvas').closest('[data-ref]')`
# and reads that element's data-ref as the QR payload. Current WhatsApp Web
# breaks both halves of that: at the moment the helper runs there is often no
# <canvas> at all, and once one exists the nearest ancestor carrying a data-ref
# is the "Link with phone number instead" / download banner, whose data-ref is
# a `https://wa.me/settings/...` URL. Measured against the pinned build:
#
#   scrapeImg()             -> urlCode "https://wa.me/settings/l..."
#   WPP.conn.getAuthCode()  -> fullCode 237 chars, starts with "2@",
#                              type "multidevice"
#
# A real WhatsApp login payload starts with `2@`, so what upstream emits is not
# a login QR at all — a phone pointed at it can never pair. Every failure is
# swallowed by scrapeImg's own `.catch(() => undefined)`, so this surfaced only
# as a QR that never appeared, or one that appeared and was silently refused.
#
# wa-js exposes the payload directly, so the DOM is out of the loop entirely.
# The PNG is rendered here with `qrcode` (already present in node_modules,
# resolvable from this file) at margin 0, deliberately: connect.py's
# display_qrcode_image() adds its own quiet zone and then magnifies by a whole
# integer factor with nearest-neighbour, and it documents that it is fed a
# borderless image. Emitting one with a margin would double the quiet zone and
# shrink the modules.
ORIGINAL_GET_QR_CODE = (
    "    async getQrCode() {\n"
    "        let qrResult;\n"
    "        qrResult = await (0, helpers_1.scrapeImg)(this.page).catch(() => undefined);\n"
    "        return qrResult;\n"
    "    }\n"
)


# The first cut of the wa-js rewrite, before it logged the "no auth code yet"
# case. Kept only so a machine patched from this branch mid-investigation is
# upgraded rather than reported as DID NOT MATCH.
V1_GET_QR_CODE = (
    "    async getQrCode() {\n"
    "        const auth = await (0, helpers_1.evaluateAndReturn)(this.page, async () => {\n"
    "            try {\n"
    "                const code = await WPP.conn.getAuthCode();\n"
    "                if (!code || !code.fullCode) {\n"
    "                    return null;\n"
    "                }\n"
    "                return { fullCode: String(code.fullCode), type: String(code.type || '') };\n"
    "            }\n"
    "            catch (error) {\n"
    "                return null;\n"
    "            }\n"
    "        }).catch(() => null);\n"
    "        if (!auth?.fullCode) {\n"
    "            return undefined;\n"
    "        }\n"
    "        let base64Image = '';\n"
    "        try {\n"
    "            base64Image = await require('qrcode').toDataURL(auth.fullCode, { margin: 0, scale: 4 });\n"
    "        }\n"
    "        catch (error) {\n"
    "            this.log('warn', `Could not render the QR image: ${error?.message || error}`);\n"
    "        }\n"
    "        return { base64Image, urlCode: auth.fullCode };\n"
    "    }\n"
)


PATCHED_GET_QR_CODE = (
    "    async getQrCode() {\n"
    "        const auth = await (0, helpers_1.evaluateAndReturn)(this.page, async () => {\n"
    "            try {\n"
    "                const code = await WPP.conn.getAuthCode();\n"
    "                if (!code || !code.fullCode) {\n"
    "                    return null;\n"
    "                }\n"
    "                return { fullCode: String(code.fullCode), type: String(code.type || '') };\n"
    "            }\n"
    "            catch (error) {\n"
    "                return null;\n"
    "            }\n"
    "        }).catch(() => null);\n"
    "        if (!auth?.fullCode) {\n"
    "            this.qrProbeMisses = (this.qrProbeMisses || 0) + 1;\n"
    "            if (this.qrProbeMisses <= 3 || this.qrProbeMisses % 20 === 0) {\n"
    "                this.log('verbose', `No auth code available yet (probe ${this.qrProbeMisses}).`);\n"
    "            }\n"
    "            return undefined;\n"
    "        }\n"
    "        this.qrProbeMisses = 0;\n"
    "        let base64Image = '';\n"
    "        try {\n"
    "            base64Image = await require('qrcode').toDataURL(auth.fullCode, { margin: 0, scale: 4 });\n"
    "        }\n"
    "        catch (error) {\n"
    "            this.log('warn', `Could not render the QR image: ${error?.message || error}`);\n"
    "        }\n"
    "        return { base64Image, urlCode: auth.fullCode };\n"
    "    }\n"
)


# waitForQrCodeScan() — a failed auth probe must not count as "logged in".
#
# Upstream:
#
#     const needScan = await needsToScan(this.page).catch(() => null);
#     this.isLogged = !needScan;
#
# needsToScan() is `page.evaluate(() => WPP.conn.isRegistered())`. When that
# throws — a detached frame, a navigation, a page that is briefly not there —
# the catch turns it into `null`, and `!null` is `true`. So the one thing that
# means "we could not find out" is recorded as the strongest possible claim:
# the user has logged in. The loop exits, waitForLogin() then calls
# isAuthenticated() itself, gets null again, and reports `Failed to
# authenticate` / `qrReadError` — with the actual browser-side error never
# written down anywhere, which is why this cost a full instrumented bisect to
# find rather than being readable from wppconnect.log.
#
# A probe failure is now retried (it is usually transient) and logged. Only a
# long run of consecutive failures gives up, and it says so, leaving isLogged
# false so the caller's own reporting stays honest.
ORIGINAL_WAIT_FOR_QR_CODE_SCAN = (
    "    async waitForQrCodeScan() {\n"
    "        if (!this.isStarted) {\n"
    "            throw new Error('waitForQrCodeScan error: Session not started');\n"
    "        }\n"
    "        while (!this.page.isClosed() && !this.isLogged) {\n"
    "            await (0, sleep_1.sleep)(200);\n"
    "            const needScan = await (0, auth_1.needsToScan)(this.page).catch(() => null);\n"
    "            this.isLogged = !needScan;\n"
    "        }\n"
    "    }\n"
)


V1_WAIT_FOR_QR_CODE_SCAN = (
    "    async waitForQrCodeScan() {\n"
    "        if (!this.isStarted) {\n"
    "            throw new Error('waitForQrCodeScan error: Session not started');\n"
    "        }\n"
    "        let probeFailures = 0;\n"
    "        while (!this.page.isClosed() && !this.isLogged) {\n"
    "            await (0, sleep_1.sleep)(200);\n"
    "            let needScan;\n"
    "            try {\n"
    "                needScan = await (0, auth_1.needsToScan)(this.page);\n"
    "            }\n"
    "            catch (error) {\n"
    "                probeFailures++;\n"
    "                if (probeFailures <= 3 || probeFailures % 25 === 0) {\n"
    "                    this.log('warn', `Auth probe failed (${probeFailures} in a row, still waiting): ${error?.name || 'Error'}: ${error?.message || error}`);\n"
    "                }\n"
    "                if (probeFailures >= 150) {\n"
    "                    this.log('error', 'Auth probe has failed for 30s straight — giving up on the scan wait.');\n"
    "                    return;\n"
    "                }\n"
    "                continue;\n"
    "            }\n"
    "            probeFailures = 0;\n"
    "            this.isLogged = !needScan;\n"
    "        }\n"
    "    }\n"
)


# The first cut, bounded by an iteration count instead of the wall clock.
# Kept only so a machine patched from this branch mid-investigation is
# upgraded rather than reported as DID NOT MATCH.
PATCHED_WAIT_FOR_QR_CODE_SCAN = (
    "    async waitForQrCodeScan() {\n"
    "        if (!this.isStarted) {\n"
    "            throw new Error('waitForQrCodeScan error: Session not started');\n"
    "        }\n"
    "        let probeFailures = 0;\n"
    "        let probeDeadline = 0;\n"
    "        while (!this.page.isClosed() && !this.isLogged) {\n"
    "            await (0, sleep_1.sleep)(200);\n"
    "            let needScan;\n"
    "            try {\n"
    "                needScan = await (0, auth_1.needsToScan)(this.page);\n"
    "            }\n"
    "            catch (error) {\n"
    "                probeFailures++;\n"
    "                if (!probeDeadline) {\n"
    "                    probeDeadline = Date.now() + 30000;\n"
    "                }\n"
    "                if (probeFailures <= 3 || probeFailures % 20 === 0) {\n"
    "                    this.log('warn', `Auth probe failed (${probeFailures} in a row, still waiting): ${error?.name || 'Error'}: ${error?.message || error}`);\n"
    "                }\n"
    "                if (Date.now() >= probeDeadline) {\n"
    "                    this.log('error', 'Auth probe has failed for 30s straight — giving up on the scan wait.');\n"
    "                    return;\n"
    "                }\n"
    "                continue;\n"
    "            }\n"
    "            probeFailures = 0;\n"
    "            probeDeadline = 0;\n"
    "            this.isLogged = !needScan;\n"
    "        }\n"
    "    }\n"
)


ORIGINAL_LOGIN_BY_CODE = (
    "    async loginByCode(phone) {\n"
    "        const code = await (0, helpers_1.evaluateAndReturn)(this.page, async ({ phone }) => {\n"
    "            return JSON.parse(JSON.stringify(await WPP.conn.genLinkDeviceCodeForPhoneNumber(phone)));\n"
    "        }, { phone });\n"
    "        if (this.options.logQR) {\n"
    "            this.log('info', `Waiting for Login By Code (Code: ${code})\\n`);\n"
    "        }\n"
    "        else {\n"
    "            this.log('verbose', `Waiting for Login By Code`);\n"
    "        }\n"
    "        this.catchLinkCode?.(code);\n"
    "    }\n"
)

LEGACY_LOGIN_BY_CODE_RAW = (
    "    async loginByCode(phone) {\n"
    "        const outcome = await (0, helpers_1.evaluateAndReturn)(this.page, async ({ phone }) => {\n"
    "            try {\n"
    "                return { code: JSON.parse(JSON.stringify(await WPP.conn.genLinkDeviceCodeForPhoneNumber(phone))) };\n"
    "            }\n"
    "            catch (error) {\n"
    "                const details = {};\n"
    "                try {\n"
    "                    for (const key of Object.getOwnPropertyNames(Object(error))) {\n"
    "                        if (key === 'stack') { continue; }\n"
    "                        const value = error[key];\n"
    "                        const kind = typeof value;\n"
    "                        if (value === null || kind === 'string' || kind === 'number' || kind === 'boolean') {\n"
    "                            details[key] = String(value);\n"
    "                        }\n"
    "                        else if (kind !== 'function') {\n"
    "                            try { details[key] = JSON.stringify(value); } catch (e) { details[key] = '[unserializable]'; }\n"
    "                        }\n"
    "                    }\n"
    "                }\n"
    "                catch (e) { }\n"
    "                return {\n"
    "                    __winzappError: {\n"
    "                        name: String(error?.name || 'Error'),\n"
    "                        message: String(error?.message || error?.reason || error?.text || error),\n"
    "                        stack: String(error?.stack || ''),\n"
    "                        details: details,\n"
    "                    },\n"
    "                };\n"
    "            }\n"
    "        }, { phone });\n"
    "        if (outcome?.__winzappError) {\n"
    "            const failure = new Error(outcome.__winzappError.message);\n"
    "            failure.name = outcome.__winzappError.name;\n"
    "            if (outcome.__winzappError.stack) {\n"
    "                failure.stack = outcome.__winzappError.stack;\n"
    "            }\n"
    "            failure.winzappDetails = outcome.__winzappError.details || {};\n"
    "            throw failure;\n"
    "        }\n"
    "        const code = outcome?.code;\n"
    "        if (this.options.logQR) {\n"
    "            this.log('info', `Waiting for Login By Code (Code: ${code})\\n`);\n"
    "        }\n"
    "        else {\n"
    "            this.log('verbose', `Waiting for Login By Code`);\n"
    "        }\n"
    "        this.catchLinkCode?.(code);\n"
    "    }\n"
)


PATCHED_LOGIN_BY_CODE = (
    "    async loginByCode(phone) {\n"
    "        const outcome = await (0, helpers_1.evaluateAndReturn)(this.page, async ({ phone }) => {\n"
    "            try {\n"
    "                const managed = typeof WPP.conn.startLinkDeviceCodeForPhoneNumber === 'function';\n"
    "                const value = managed\n"
    "                    ? await WPP.conn.startLinkDeviceCodeForPhoneNumber(phone)\n"
    "                    : JSON.parse(JSON.stringify(await WPP.conn.genLinkDeviceCodeForPhoneNumber(phone)));\n"
    "                return { code: String(value), managed: managed };\n"
    "            }\n"
    "            catch (error) {\n"
    "                const details = {};\n"
    "                try {\n"
    "                    for (const key of Object.getOwnPropertyNames(Object(error))) {\n"
    "                        if (key === 'stack') { continue; }\n"
    "                        const value = error[key];\n"
    "                        const kind = typeof value;\n"
    "                        if (value === null || kind === 'string' || kind === 'number' || kind === 'boolean') {\n"
    "                            details[key] = String(value);\n"
    "                        }\n"
    "                        else if (kind !== 'function') {\n"
    "                            try { details[key] = JSON.stringify(value); } catch (e) { details[key] = '[unserializable]'; }\n"
    "                        }\n"
    "                    }\n"
    "                    details.__winzappManagedApi = String(typeof WPP.conn.startLinkDeviceCodeForPhoneNumber === 'function');\n"
    "                }\n"
    "                catch (e) { }\n"
    "                return {\n"
    "                    __winzappError: {\n"
    "                        name: String(error?.name || 'Error'),\n"
    "                        message: String(error?.message || error?.reason || error?.text || error),\n"
    "                        stack: String(error?.stack || ''),\n"
    "                        details: details,\n"
    "                    },\n"
    "                };\n"
    "            }\n"
    "        }, { phone });\n"
    "        if (outcome?.__winzappError) {\n"
    "            const failure = new Error(outcome.__winzappError.message);\n"
    "            failure.name = outcome.__winzappError.name;\n"
    "            if (outcome.__winzappError.stack) {\n"
    "                failure.stack = outcome.__winzappError.stack;\n"
    "            }\n"
    "            failure.winzappDetails = outcome.__winzappError.details || {};\n"
    "            throw failure;\n"
    "        }\n"
    "        const code = outcome?.code;\n"
    "        this.log('info', `Link code obtained via the ${outcome?.managed ? 'managed' : 'legacy raw'} wa-js API.`);\n"
    "        if (this.options.logQR) {\n"
    "            this.log('info', `Waiting for Login By Code (Code: ${code})\\n`);\n"
    "        }\n"
    "        else {\n"
    "            this.log('verbose', `Waiting for Login By Code`);\n"
    "        }\n"
    "        this.catchLinkCode?.(code);\n"
    "    }\n"
)


# ---------------------------------------------------------------------------
# wppconnect >= 2.3.2 — the managed link-device flow.
#
# Everything above this line targets the 2.3.1-and-older file, where
# checkQrCode() owned the whole pairing-code lifecycle. Everything below
# targets the file 2.3.2 ships, where wa-js owns it. See the v9 entry in the
# module docstring for what that moved and why the cooldown did not come with
# it.
# ---------------------------------------------------------------------------


#: How patch_host_layer_source() tells the two runtimes apart. refreshLinkCode()
#: is new in 2.3.2, and it is deliberately a method NO patch here rewrites — a
#: marker that one of the patches also edits would stop identifying the file the
#: moment that patch applied. A future release that removes it again falls back
#: to the legacy set, where nothing matches, and says so loudly: the failure
#: mode is a warning nobody can miss, never a 2.3.2 patch written into a 2.3.1
#: file.
MANAGED_LINK_MARKER = "    async refreshLinkCode() {\n"


MANAGED_ORIGINAL_CHECK_QR_CODE = (
    "    async checkQrCode() {\n"
    "        const needScan = await (0, auth_1.needsToScan)(this.page).catch(() => null);\n"
    "        this.isLogged = !needScan;\n"
    "        if (!needScan) {\n"
    "            this.attempt = 0;\n"
    "            return;\n"
    "        }\n"
    "        const result = await this.getQrCode();\n"
    "        if (!result?.urlCode || this.urlCode === result.urlCode) {\n"
    "            return;\n"
    "        }\n"
    "        this.urlCode = result.urlCode;\n"
    "        this.attempt++;\n"
    "        let qr = '';\n"
    "        if (this.options.logQR || this.catchQR) {\n"
    "            qr = await (0, auth_1.asciiQr)(this.urlCode);\n"
    "        }\n"
    "        if (this.options.logQR) {\n"
    "            this.log('info', `Waiting for QRCode Scan (Attempt ${this.attempt})...:\\n${qr}`, { code: this.urlCode });\n"
    "        }\n"
    "        else {\n"
    "            this.log('verbose', `Waiting for QRCode Scan: Attempt ${this.attempt}`);\n"
    "        }\n"
    "        this.catchQR?.(result.base64Image, qr, this.attempt, result.urlCode);\n"
    "    }\n"
)


# The v7 head, and nothing else. On this runtime checkQrCode() is the QR-mode
# path only — it is registered on `conn.auth_code_change` just as before, so it
# still runs concurrently with waitForQrCodeScan() and can still hand that loop
# a `!null` "the user is logged in" out of a probe that could not answer.
MANAGED_PATCHED_CHECK_QR_CODE = (
    "    async checkQrCode() {\n"
    "        let needScan;\n"
    "        try {\n"
    "            needScan = await (0, auth_1.needsToScan)(this.page);\n"
    "        }\n"
    "        catch (error) {\n"
    "            this.log('verbose', `Auth probe failed inside checkQrCode - leaving isLogged untouched: ${error?.name || 'Error'}: ${error?.message || error}`);\n"
    "            return;\n"
    "        }\n"
    "        this.isLogged = !needScan;\n"
    "        if (!needScan) {\n"
    "            this.attempt = 0;\n"
    "            return;\n"
    "        }\n"
    "        const result = await this.getQrCode();\n"
    "        if (!result?.urlCode || this.urlCode === result.urlCode) {\n"
    "            return;\n"
    "        }\n"
    "        this.urlCode = result.urlCode;\n"
    "        this.attempt++;\n"
    "        let qr = '';\n"
    "        if (this.options.logQR || this.catchQR) {\n"
    "            qr = await (0, auth_1.asciiQr)(this.urlCode);\n"
    "        }\n"
    "        if (this.options.logQR) {\n"
    "            this.log('info', `Waiting for QRCode Scan (Attempt ${this.attempt})...:\\n${qr}`, { code: this.urlCode });\n"
    "        }\n"
    "        else {\n"
    "            this.log('verbose', `Waiting for QRCode Scan: Attempt ${this.attempt}`);\n"
    "        }\n"
    "        this.catchQR?.(result.base64Image, qr, this.attempt, result.urlCode);\n"
    "    }\n"
)


MANAGED_ORIGINAL_LOGIN_BY_CODE = (
    "    async loginByCode(phone) {\n"
    "        await (0, helpers_1.evaluateAndReturn)(this.page, async ({ phone }) => {\n"
    "            await WPP.conn.startLinkDeviceCodeForPhoneNumber(phone);\n"
    "        }, { phone });\n"
    "    }\n"
)


# loginByCode() is now the whole pairing attempt, because upstream calls it once
# and never again. Three things ride on that, each of which was a shipped bug on
# the previous runtime:
#
#   * the auth-state gate v6 restored. 2.3.1's upstream reached this call only
#     after getQrCode() produced a urlCode; 2.3.2 calls it as soon as wapi.js is
#     injected, which is precisely the window where wa-js walks setADVSecretKey
#     -> allUserPrefsIdb -> getUserPrefsTable into an uninitialised table and
#     WhatsApp Web throws `Invariant Violation #56367`. The probe is the same
#     one v6 used and measured as side-effect-free.
#
#     `needScan === false` short-circuits it: WinZapp only sends `phone` on a
#     real pairing attempt, but a page reload inside one re-enters here, and
#     wa-js refuses a code for an already-registered session
#     ("cannot_get_code_for_already_authenticated"). Polling for an auth code
#     that by definition will never come would burn the whole gate window and
#     then report a failure for a session that is fine.
#
#     60 probes is a ceiling for a second reason, and it is the easy one to
#     miss: afterPageScriptInjected() *awaits* this method, and
#     ListenerLayer.afterPageScriptInjected() registers
#     `WPP.on('chat.new_message', ...)` / waitNewAcknowledgements only after
#     `await super.afterPageScriptInjected()` returns. Every second spent here
#     is a second the page-side message listeners do not exist. The happy path
#     never pays it — a registered session short-circuits on the first probe
#     just above, and a session actually pairing has no messages to miss — but
#     a probe that kept failing on a live session reloading with
#     options.phoneNumber still set would hold those listeners off for the
#     whole window. 60s is the largest slice of connect.py's 90s wait that
#     still leaves room for the second mint attempt below; it is a ceiling to
#     stay under, not a budget to spend. Same reasoning createSessionUtil.ts's
#     own bounded isConnected() loop spells out for what is registered behind
#     it.
#
#   * the error detail. Upstream awaits bare, so a refusal crosses the CDP
#     boundary as the minified "t: t" and lands in wppconnect.log — the exact
#     state v3/v4 were written to end. The __winzappError envelope carries
#     WhatsApp's own error properties back out of the page as plain data, and
#     catchLinkCodeError puts them in front of the person pairing.
#
#   * a bounded retry. v2..v8 got one for free: `conn.auth_code_change` re-entered
#     checkQrCode() every ~minute, so a transient failure fixed itself. Nothing
#     re-enters this method, so a single "Execution context was destroyed" would
#     otherwise end pairing for the session with nothing on screen. One retry,
#     20s later — and a rate-limited answer gives up immediately instead of
#     backing off, because the quota is per phone number and lives on
#     WhatsApp's side: retrying a 429 is what keeps it alive, and here there is
#     no loop to slow down, only one to not start.
#
#     Two attempts, not four, and the ceiling is the *client's* patience rather
#     than anything on this side: connect.py waits 90s for a phoneCode and then
#     abandons the session. The gate above can spend 60s of that on its own, so
#     20s/40s/80s put attempts 3 and 4 at ~120s and ~200s — after the dialog is
#     gone, the token cleared and `no_pairing_code_received` already shown.
#     _belongs_to_this_session() drops whatever code they produce, so nothing
#     breaks visibly; they just spend real quota on the user's number for a
#     dialog nobody is looking at, which is a smaller version of the thing the
#     v8 cooldown existed to prevent. 60s + 20s still lands attempt 2 inside
#     the window, so a single hiccup still does not end pairing.
#
#   * `this.lastLinkCode = null` on entry. onLinkCode's dedup below is meant to
#     cover the event and the return value of *this* call — per invocation, not
#     per session — and clearing it here is what makes that true. `page.on
#     ('load')` -> afterPageLoad() -> afterPageScriptInjected() re-enters this
#     method on every WhatsApp Web reload, on the same HostLayer, while wa-js's
#     own state inside the page is reset. If the post-reload mint answered with
#     the code still on screen, a surviving value would drop both deliveries of
#     it: catchLinkCode never fires, no phoneCode reaches Python, and
#     connect.py's 90s wait ends in "no pairing code received" for a session
#     that had a perfectly good code. Defensive — WhatsApp re-issuing an
#     identical code was not reproduced.
MANAGED_PATCHED_LOGIN_BY_CODE = (
    "    async loginByCode(phone) {\n"
    "        this.lastLinkCode = null;\n"
    "        let ready = null;\n"
    "        for (let probe = 1; probe <= 60 && !ready; probe++) {\n"
    "            if (this.page.isClosed()) {\n"
    "                return;\n"
    "            }\n"
    "            const needScan = await (0, auth_1.needsToScan)(this.page).catch(() => null);\n"
    "            if (needScan === false) {\n"
    "                this.log('verbose', 'Already registered — no pairing code needed.');\n"
    "                return;\n"
    "            }\n"
    "            ready = await this.getQrCode();\n"
    "            if (!ready?.urlCode) {\n"
    "                ready = null;\n"
    "                await (0, sleep_1.sleep)(1000);\n"
    "            }\n"
    "        }\n"
    "        if (!ready) {\n"
    "            const timeout = new Error('WhatsApp Web never produced an auth state to link against.');\n"
    "            timeout.name = 'LinkCodeAuthStateTimeout';\n"
    "            this.log('error', `Could not generate the pairing code: ${timeout.name}: ${timeout.message}`);\n"
    "            this.options.catchLinkCodeError?.({\n"
    "                name: timeout.name,\n"
    "                message: timeout.message,\n"
    "                session: this.session,\n"
    "            });\n"
    "            throw timeout;\n"
    "        }\n"
    "        for (let attempt = 1; attempt <= 2; attempt++) {\n"
    "            let outcome;\n"
    "            try {\n"
    "                outcome = await (0, helpers_1.evaluateAndReturn)(this.page, async ({ phone }) => {\n"
    "                    try {\n"
    "                        const value = await WPP.conn.startLinkDeviceCodeForPhoneNumber(phone);\n"
    "                        return { code: value ? String(value) : '' };\n"
    "                    }\n"
    "                    catch (error) {\n"
    "                        const details = {};\n"
    "                        try {\n"
    "                            for (const key of Object.getOwnPropertyNames(Object(error))) {\n"
    "                                if (key === 'stack') { continue; }\n"
    "                                const value = error[key];\n"
    "                                const kind = typeof value;\n"
    "                                if (value === null || kind === 'string' || kind === 'number' || kind === 'boolean') {\n"
    "                                    details[key] = String(value);\n"
    "                                }\n"
    "                                else if (kind !== 'function') {\n"
    "                                    try { details[key] = JSON.stringify(value); } catch (e) { details[key] = '[unserializable]'; }\n"
    "                                }\n"
    "                            }\n"
    "                            details.__winzappManagedApi = String(typeof WPP.conn.startLinkDeviceCodeForPhoneNumber === 'function');\n"
    "                        }\n"
    "                        catch (e) { }\n"
    "                        return {\n"
    "                            __winzappError: {\n"
    "                                name: String(error?.name || 'Error'),\n"
    "                                message: String(error?.message || error?.reason || error?.text || error),\n"
    "                                stack: String(error?.stack || ''),\n"
    "                                details: details,\n"
    "                            },\n"
    "                        };\n"
    "                    }\n"
    "                }, { phone });\n"
    "            }\n"
    "            catch (error) {\n"
    "                outcome = {\n"
    "                    __winzappError: {\n"
    "                        name: String(error?.name || 'Error'),\n"
    "                        message: String(error?.message || error),\n"
    "                        stack: String(error?.stack || ''),\n"
    "                        details: {},\n"
    "                    },\n"
    "                };\n"
    "            }\n"
    "            if (!outcome?.__winzappError) {\n"
    "                this.onLinkCode(outcome?.code);\n"
    "                return;\n"
    "            }\n"
    "            const failed = outcome.__winzappError;\n"
    "            let detail = '';\n"
    "            try {\n"
    "                detail = JSON.stringify(failed.details || {});\n"
    "            }\n"
    "            catch (e) {\n"
    "                detail = '';\n"
    "            }\n"
    "            const rateLimited = /rate-overlimit|RateOverlimit/i.test(`${detail} ${failed.name} ${failed.message}`);\n"
    "            const giveUp = rateLimited || attempt >= 2 || this.page.isClosed();\n"
    "            const backoff = giveUp ? 0 : 20000 * Math.pow(2, attempt - 1);\n"
    "            const retryInSeconds = Math.round(backoff / 1000);\n"
    "            this.log('error', `Could not generate the pairing code (attempt ${attempt}${rateLimited ? ', rate-limited by WhatsApp' : ''}${giveUp ? ', giving up' : `, next retry in ${retryInSeconds}s`}): ${failed.name}: ${failed.message}`);\n"
    "            this.options.catchLinkCodeError?.({\n"
    "                name: failed.name,\n"
    "                message: failed.message,\n"
    "                session: this.session,\n"
    "                attempt: attempt,\n"
    "                retryInSeconds: retryInSeconds,\n"
    "                rateLimited: rateLimited,\n"
    "                stack: failed.stack,\n"
    "                details: failed.details || {},\n"
    "            });\n"
    "            if (giveUp) {\n"
    "                const failure = new Error(failed.message);\n"
    "                failure.name = failed.name;\n"
    "                if (failed.stack) {\n"
    "                    failure.stack = failed.stack;\n"
    "                }\n"
    "                failure.winzappDetails = failed.details || {};\n"
    "                throw failure;\n"
    "            }\n"
    "            await (0, sleep_1.sleep)(backoff);\n"
    "        }\n"
    "    }\n"
)


# The retry bound used to live only in `giveUp` (`attempt >= 2`), with the loop
# header written `for (let attempt = 1;; attempt++)`. Same two attempts either
# way — the header now states the limit where a reader looks for it. It is a
# rewrite rather than an edit because a tree already patched with the previous
# text matches neither the pristine source nor the current patched block, so
# without this the whole loginByCode block would report DID NOT MATCH and stay
# on the old text forever — the same reason the sender layer migrates its own
# shipped blocks. Only the header moved, so one line covers it.
MANAGED_UNBOUNDED_LOGIN_RETRY_HEADER = "        for (let attempt = 1;; attempt++) {\n"
MANAGED_BOUNDED_LOGIN_RETRY_HEADER = (
    "        for (let attempt = 1; attempt <= 2; attempt++) {\n"
)


MANAGED_ORIGINAL_ON_LINK_CODE = (
    "    onLinkCode(code) {\n"
    "        if (this.options.logQR) {\n"
    "            this.log('info', `Waiting for Login By Code (Code: ${code})\\n`);\n"
    "        }\n"
    "        else {\n"
    "            this.log('verbose', `Waiting for Login By Code`);\n"
    "        }\n"
    "        this.catchLinkCode?.(code);\n"
    "    }\n"
)


# The same code arrives twice, on purpose. wa-js emits `conn.link_code_change`
# (which upstream routes here) AND resolves startLinkDeviceCodeForPhoneNumber
# with the code, and loginByCode() above hands its own copy to this method
# rather than to catchLinkCode directly. Neither route is redundant: the event
# is the only one that carries a later re-mint, and the returned value is the
# only one that survives `WPP.on('conn.link_code_change', window.onLinkCode)`
# being registered before page.exposeFunction('onLinkCode', ...) has resolved —
# a race upstream loses by never producing a code at all. Whichever lands first
# wins; dedup by value keeps the pairing dialog from being rewritten, and the
# code re-read aloud, for a code the user is already looking at.
MANAGED_PATCHED_ON_LINK_CODE = (
    "    onLinkCode(code) {\n"
    "        if (!code || this.lastLinkCode === code) {\n"
    "            return;\n"
    "        }\n"
    "        this.lastLinkCode = code;\n"
    "        if (this.options.logQR) {\n"
    "            this.log('info', `Waiting for Login By Code (Code: ${code})\\n`);\n"
    "        }\n"
    "        else {\n"
    "            this.log('verbose', `Waiting for Login By Code`);\n"
    "        }\n"
    "        this.catchLinkCode?.(code);\n"
    "    }\n"
)


MANAGED_ORIGINAL_LINK_CODE_HOOKS = (
    "        await this.page.exposeFunction('onLinkCodeExpired', () => this.log('warn', 'Login by code expired; call refreshLinkCode() to retry'));\n"
    "        await this.page.exposeFunction('onLinkCodeError', (message) => this.log('error', `Login by code failed: ${message}`));\n"
)


# wa-js re-mints the code on its own 195s timer and gives up after five
# refreshes, so from the first code on, every further failure and the end of the
# stream are reported through these two events and nowhere else — loginByCode()
# has long since returned. Upstream writes both to wppconnect.log, which is the
# one place a blind user pairing cannot look. Routed into catchLinkCodeError so
# they reach the same phoneCodeError channel as a first-mint failure.
#
# Reaching that channel is not by itself reaching the user: `_phone_code_error`
# is read only by connect.py's 90s wait, which has already returned by the time
# either event can fire. So both are announced over the pairing dialog by
# WebSocketClient._announce_pairing_code_expired(), and the name they are
# announced under is decided here, because this is the only side that knows
# whether a code was ever on screen.
#
# A refresh failure is the end of the stream, not a hiccup — read off wa-js's
# own lifecycle rather than assumed. refreshLinkDeviceCode() clears the 195s
# timer *before* minting, and the only thing that re-arms it is the success
# continuation of the mint; so when the mint fails, wa-js is left with
# `code = null`, no timer, and a rejection its own caller swallows with
# `.catch(() => {})`. Nothing on wa-js's own side reschedules, and
# `conn.link_code_expired` can no longer fire either — both of its emitters
# need the timer still armed or a `force_manual_refresh` from WhatsApp Web.
# The alternative-linking error has the same shape. Only WhatsApp Web can
# restart it, by pushing `refresh_alt_linking_code`, which is not something to
# leave a user waiting on. So the user is left holding a code wa-js has already
# discarded, at t+195s instead of the ~19.5 minutes the expiry path takes: they
# type it, WhatsApp refuses, they cancel and retry, and the retry mints a fresh
# session and another six codes. It is the anti-abuse loop LinkCodeExpired was
# surfaced to break, entered through a quieter door and sooner.
#
# `hadCode` is what keeps the *first* mint out of that. Its failure emits
# `conn.link_code_error` too (startLinkDeviceCodeForPhoneNumber's own catch),
# and that case is already covered — loginByCode()'s ladder retries it and
# connect.py's 90s wait reports it — so announcing "your code expired" during
# the initial wait, over a dialog with nothing on it to expire, would be pure
# noise. `lastLinkCode` is the only signal that distinguishes the two, and
# clearing it here is needed on its own account anyway, symmetrically with the
# expiry hook: if WhatsApp Web does push `refresh_alt_linking_code` and the
# mint that follows answers with the same code, a surviving value would drop
# it at onLinkCode's dedup — the one delivery that would have recovered the
# attempt.
#
# The report carries the quota verdict too. `name`/`message` alone cannot carry
# it, and this hook is what decides between "cancel and try again" and "wait a
# few minutes" on the Python side — opposite instructions, the first of which is precisely
# what spends the quota that produced the refusal. wa-js hands this event an
# error whose OWN properties hold WhatsApp's answer
# ({"name":"IQErrorRateOverlimit","value":{"text":"rate-overlimit","code":429}})
# while `.message` is whatever the minified class left there, so the page-side
# listener serialises those properties (MANAGED_PATCHED_LINK_CODE_LISTENER) and
# this hook classifies them exactly as loginByCode()'s own catch does — same
# regex, same `rateLimited`/`details` fields, so phone_code_error_is_rate_limit()
# reads one shape from both mint paths.
#
# The plain-string argument is still accepted, deliberately: the two blocks are
# matched and replaced independently, so an install where only the hook took
# must degrade to a message-only report rather than to no report at all.
MANAGED_PATCHED_LINK_CODE_HOOKS = (
    "        await this.page.exposeFunction('onLinkCodeExpired', () => {\n"
    "            this.log('warn', 'Login by code expired; call refreshLinkCode() to retry');\n"
    "            this.lastLinkCode = null;\n"
    "            this.options.catchLinkCodeError?.({\n"
    "                name: 'LinkCodeExpired',\n"
    "                message: 'WhatsApp stopped refreshing the pairing code for this attempt.',\n"
    "                session: this.session,\n"
    "            });\n"
    "        });\n"
    "        await this.page.exposeFunction('onLinkCodeError', (report) => {\n"
    "            const failed = (report && typeof report === 'object') ? report : { message: String(report || '') };\n"
    "            const message = String(failed.message || '');\n"
    "            this.log('error', `Login by code failed: ${failed.name || 'Error'}: ${message}`);\n"
    "            const hadCode = this.lastLinkCode != null;\n"
    "            this.lastLinkCode = null;\n"
    "            let detail = '';\n"
    "            try {\n"
    "                detail = JSON.stringify(failed.details || {});\n"
    "            }\n"
    "            catch (e) {\n"
    "                detail = '';\n"
    "            }\n"
    "            const rateLimited = /rate-overlimit|RateOverlimit/i.test(`${detail} ${failed.name || ''} ${message}`);\n"
    "            this.options.catchLinkCodeError?.({\n"
    "                name: hadCode ? 'LinkCodeRefreshFailed' : 'LinkCodeError',\n"
    "                message: message,\n"
    "                session: this.session,\n"
    "                rateLimited: rateLimited,\n"
    "                details: failed.details || {},\n"
    "            });\n"
    "        });\n"
)


MANAGED_ORIGINAL_LINK_CODE_LISTENER = (
    "                WPP.on('conn.link_code_error', (error) => window.onLinkCodeError(error.message));\n"
)


# The other half of the envelope, and the half that has to run inside the page:
# page.exposeFunction() serialises its arguments and an Error serialises to
# `{}`, which is why upstream reads `.message` off it here instead of passing
# the error across. Reading only `.message` throws away the one field that
# identifies a quota refusal, so this walks the error's own properties the same
# way MANAGED_PATCHED_LOGIN_BY_CODE's catch does — same skip of `stack`, same
# '[unserializable]' guard for a getter that throws or a circular value — and
# hands the hook a plain object it can classify.
MANAGED_PATCHED_LINK_CODE_LISTENER = (
    "                WPP.on('conn.link_code_error', (error) => {\n"
    "                    const details = {};\n"
    "                    try {\n"
    "                        for (const key of Object.getOwnPropertyNames(Object(error))) {\n"
    "                            if (key === 'stack') { continue; }\n"
    "                            const value = error[key];\n"
    "                            const kind = typeof value;\n"
    "                            if (value === null || kind === 'string' || kind === 'number' || kind === 'boolean') {\n"
    "                                details[key] = String(value);\n"
    "                            }\n"
    "                            else if (kind !== 'function') {\n"
    "                                try { details[key] = JSON.stringify(value); } catch (e) { details[key] = '[unserializable]'; }\n"
    "                            }\n"
    "                        }\n"
    "                    }\n"
    "                    catch (e) { }\n"
    "                    window.onLinkCodeError({\n"
    "                        name: String(error?.name || 'Error'),\n"
    "                        message: String(error?.message || error?.reason || error?.text || error),\n"
    "                        details: details,\n"
    "                    });\n"
    "                });\n"
)


def patch_host_layer_source(content: str):
    """Apply every host.layer.js patch, choosing the set that matches the
    runtime actually on disk.

    The choice is not cosmetic. Both call sites re-run this on every launch
    against whatever node_modules holds, and a WinZapp update on its own never
    reinstalls node_modules — so a 2.3.1 tree stays a 2.3.1 tree until the user
    reinstalls the API. Writing the 2.3.2 text into it would remove the only
    place that runtime calls loginByCode() from.
    """
    notes = []

    if MANAGED_LINK_MARKER in content:
        content = _patch_managed_link_flow(content, notes)
    else:
        content = _patch_legacy_link_flow(content, notes)

    # Neither method changed in 2.3.2, so both runtimes take the same text.
    content = _patch_qr_reads(content, notes)

    ok = not any("DID NOT MATCH" in note for note in notes)
    return content, notes, ok


def _patch_managed_link_flow(content: str, notes: list) -> str:
    """wppconnect >= 2.3.2: wa-js owns the code's lifecycle, host.layer.js
    starts it once. See the v9 entry in the module docstring."""
    if MANAGED_PATCHED_CHECK_QR_CODE in content:
        notes.append("checkQrCode: already carries the auth-probe fix.")
    elif MANAGED_ORIGINAL_CHECK_QR_CODE in content:
        content = content.replace(
            MANAGED_ORIGINAL_CHECK_QR_CODE, MANAGED_PATCHED_CHECK_QR_CODE, 1
        )
        notes.append(
            "checkQrCode: patched — a failed auth probe no longer sets "
            "isLogged. The pairing-code cooldown is gone with the loop it "
            "paced: this runtime never calls loginByCode() from here."
        )
    else:
        notes.append("checkQrCode: DID NOT MATCH any known source text — left untouched.")

    if MANAGED_UNBOUNDED_LOGIN_RETRY_HEADER in content:
        content = content.replace(
            MANAGED_UNBOUNDED_LOGIN_RETRY_HEADER,
            MANAGED_BOUNDED_LOGIN_RETRY_HEADER,
            1,
        )
        notes.append(
            "loginByCode: retry bound moved into the loop header — no "
            "behaviour change, still two attempts."
        )

    if MANAGED_PATCHED_LOGIN_BY_CODE in content:
        notes.append("loginByCode: already gated, reporting and retrying.")
    elif MANAGED_ORIGINAL_LOGIN_BY_CODE in content:
        content = content.replace(
            MANAGED_ORIGINAL_LOGIN_BY_CODE, MANAGED_PATCHED_LOGIN_BY_CODE, 1
        )
        notes.append(
            "loginByCode: patched — waits for WhatsApp Web's auth state "
            "before the one call this runtime makes, reports the real "
            "browser-side error, and retries a transient failure instead of "
            "ending pairing on it."
        )
    else:
        notes.append("loginByCode: DID NOT MATCH the known source text — left untouched.")

    if MANAGED_PATCHED_ON_LINK_CODE in content:
        notes.append("onLinkCode: already deduping the code.")
    elif MANAGED_ORIGINAL_ON_LINK_CODE in content:
        content = content.replace(
            MANAGED_ORIGINAL_ON_LINK_CODE, MANAGED_PATCHED_ON_LINK_CODE, 1
        )
        notes.append(
            "onLinkCode: patched — the event and loginByCode()'s own return "
            "value both deliver the code, and only the first of them is "
            "announced."
        )
    else:
        notes.append("onLinkCode: DID NOT MATCH the known source text — left untouched.")

    if MANAGED_PATCHED_LINK_CODE_HOOKS in content:
        notes.append("link-code hooks: already reported to the client.")
    elif MANAGED_ORIGINAL_LINK_CODE_HOOKS in content:
        content = content.replace(
            MANAGED_ORIGINAL_LINK_CODE_HOOKS, MANAGED_PATCHED_LINK_CODE_HOOKS, 1
        )
        notes.append(
            "link-code hooks: patched — an expired code and a refresh failure "
            "now reach the pairing dialog instead of only wppconnect.log."
        )
    else:
        notes.append(
            "link-code hooks: DID NOT MATCH the known source text — left untouched."
        )

    if MANAGED_PATCHED_LINK_CODE_LISTENER in content:
        notes.append("link-code error listener: already forwarding the error itself.")
    elif MANAGED_ORIGINAL_LINK_CODE_LISTENER in content:
        content = content.replace(
            MANAGED_ORIGINAL_LINK_CODE_LISTENER,
            MANAGED_PATCHED_LINK_CODE_LISTENER,
            1,
        )
        notes.append(
            "link-code error listener: patched — the error's own properties "
            "now cross into Node, so a quota refusal can be told apart from a "
            "plain failure."
        )
    else:
        notes.append(
            "link-code error listener: DID NOT MATCH the known source text — "
            "left untouched."
        )

    return content


def _patch_legacy_link_flow(content: str, notes: list) -> str:
    """wppconnect <= 2.3.1: checkQrCode() owns the pairing-code lifecycle."""
    if PATCHED_CHECK_QR_CODE in content:
        notes.append("checkQrCode: already at v8.")
    elif V7_CHECK_QR_CODE in content:
        content = content.replace(V7_CHECK_QR_CODE, PATCHED_CHECK_QR_CODE, 1)
        notes.append(
            "checkQrCode: upgraded v7 -> v8 — an unattended session no longer "
            "asks for a fresh pairing code every minute until WhatsApp answers "
            "rate-overlimit, and a rate-limited failure backs off for 15min."
        )
    elif V6_CHECK_QR_CODE in content:
        content = content.replace(V6_CHECK_QR_CODE, PATCHED_CHECK_QR_CODE, 1)
        notes.append(
            "checkQrCode: upgraded v6 -> v8 — a failed auth probe here no "
            "longer sets isLogged, and the reissue loop stops burning the "
            "pairing-code quota."
        )
    elif V5_CHECK_QR_CODE in content:
        content = content.replace(V5_CHECK_QR_CODE, PATCHED_CHECK_QR_CODE, 1)
        notes.append(
            "checkQrCode: upgraded v5 -> v8 — the pairing code now waits for "
            "WhatsApp Web's auth state instead of throwing Invariant #56367."
        )
    elif V4_CHECK_QR_CODE in content:
        content = content.replace(V4_CHECK_QR_CODE, PATCHED_CHECK_QR_CODE, 1)
        notes.append(
            "checkQrCode: upgraded v4 -> v8 — repeated pairing-code failures "
            "now back off, and the code waits for the auth state to exist."
        )
    elif V3_CHECK_QR_CODE in content:
        content = content.replace(V3_CHECK_QR_CODE, PATCHED_CHECK_QR_CODE, 1)
        notes.append(
            "checkQrCode: upgraded v3 -> v8 — a pairing-code failure is now "
            "reported to the client, not just written to wppconnect.log."
        )
    elif V2_CHECK_QR_CODE in content:
        content = content.replace(V2_CHECK_QR_CODE, PATCHED_CHECK_QR_CODE, 1)
        notes.append(
            "checkQrCode: upgraded v2 -> v8 — a failing loginByCode() is now "
            "caught, reported and logged instead of escaping as an unhandled "
            "rejection."
        )
    elif V1_CHECK_QR_CODE in content:
        content = content.replace(V1_CHECK_QR_CODE, PATCHED_CHECK_QR_CODE, 1)
        notes.append("checkQrCode: upgraded v1 (unsafe, could freeze forever) -> v8.")
    elif ORIGINAL_CHECK_QR_CODE in content:
        content = content.replace(ORIGINAL_CHECK_QR_CODE, PATCHED_CHECK_QR_CODE, 1)
        notes.append(
            "checkQrCode: patched (v8) — pairing code no longer regenerates on "
            "every QR rotation (the reuse cooldown widens while nobody pairs), "
            "waits for the auth state, failures are reported and a "
            "rate-overlimit answer backs off for 15min."
        )
    else:
        notes.append("checkQrCode: DID NOT MATCH any known source text — left untouched.")

    if PATCHED_LOGIN_BY_CODE in content:
        notes.append("loginByCode: already on the managed wa-js linking API.")
    elif LEGACY_LOGIN_BY_CODE_RAW in content:
        content = content.replace(LEGACY_LOGIN_BY_CODE_RAW, PATCHED_LOGIN_BY_CODE, 1)
        notes.append(
            "loginByCode: switched from the raw genLinkDeviceCodeForPhoneNumber "
            "call to wa-js's managed linking lifecycle."
        )
    elif ORIGINAL_LOGIN_BY_CODE in content:
        content = content.replace(ORIGINAL_LOGIN_BY_CODE, PATCHED_LOGIN_BY_CODE, 1)
        notes.append(
            "loginByCode: patched — uses wa-js's managed linking lifecycle and "
            "reports the real browser-side error instead of the minified 't: t'."
        )
    else:
        notes.append("loginByCode: DID NOT MATCH the known source text — left untouched.")

    return content


def _patch_qr_reads(content: str, notes: list) -> str:
    """getQrCode() and waitForQrCodeScan(), which 2.3.2 left untouched — so
    the same text applies to both runtimes and neither branch above owns it."""
    if PATCHED_WAIT_FOR_QR_CODE_SCAN in content:
        notes.append("waitForQrCodeScan: already retries a failed auth probe.")
    elif V1_WAIT_FOR_QR_CODE_SCAN in content:
        content = content.replace(
            V1_WAIT_FOR_QR_CODE_SCAN, PATCHED_WAIT_FOR_QR_CODE_SCAN, 1
        )
        notes.append(
            "waitForQrCodeScan: upgraded — the give-up bound is the wall "
            "clock now, not an iteration count that a wedged renderer "
            "stretched from 30s to hours."
        )
    elif ORIGINAL_WAIT_FOR_QR_CODE_SCAN in content:
        content = content.replace(
            ORIGINAL_WAIT_FOR_QR_CODE_SCAN, PATCHED_WAIT_FOR_QR_CODE_SCAN, 1
        )
        notes.append(
            "waitForQrCodeScan: patched — a failed auth probe is retried and "
            "logged instead of being read as 'the user is logged in'."
        )
    else:
        notes.append(
            "waitForQrCodeScan: DID NOT MATCH the known source text — left untouched."
        )

    if PATCHED_GET_QR_CODE in content:
        notes.append("getQrCode: already reading the QR from wa-js.")
    elif V1_GET_QR_CODE in content:
        content = content.replace(V1_GET_QR_CODE, PATCHED_GET_QR_CODE, 1)
        notes.append(
            "getQrCode: upgraded — a missing auth code is now logged instead "
            "of returning silently."
        )
    elif ORIGINAL_GET_QR_CODE in content:
        content = content.replace(ORIGINAL_GET_QR_CODE, PATCHED_GET_QR_CODE, 1)
        notes.append(
            "getQrCode: patched — reads WPP.conn.getAuthCode() instead of "
            "scraping a <canvas> that no longer exists, so the emitted payload "
            "is a real 2@... login code rather than the download banner's "
            "wa.me data-ref."
        )
    else:
        notes.append("getQrCode: DID NOT MATCH the known source text — left untouched.")

    return content
