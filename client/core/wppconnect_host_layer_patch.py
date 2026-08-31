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


def patch_host_layer_source(content: str):
    notes = []

    if PATCHED_CHECK_QR_CODE in content:
        notes.append("checkQrCode: already at v7.")
    elif V6_CHECK_QR_CODE in content:
        content = content.replace(V6_CHECK_QR_CODE, PATCHED_CHECK_QR_CODE, 1)
        notes.append(
            "checkQrCode: upgraded v6 -> v7 — a failed auth probe here no "
            "longer sets isLogged, which used to end the concurrent scan wait."
        )
    elif V5_CHECK_QR_CODE in content:
        content = content.replace(V5_CHECK_QR_CODE, PATCHED_CHECK_QR_CODE, 1)
        notes.append(
            "checkQrCode: upgraded v5 -> v7 — the pairing code now waits for "
            "WhatsApp Web's auth state instead of throwing Invariant #56367."
        )
    elif V4_CHECK_QR_CODE in content:
        content = content.replace(V4_CHECK_QR_CODE, PATCHED_CHECK_QR_CODE, 1)
        notes.append(
            "checkQrCode: upgraded v4 -> v7 — repeated pairing-code failures "
            "now back off, and the code waits for the auth state to exist."
        )
    elif V3_CHECK_QR_CODE in content:
        content = content.replace(V3_CHECK_QR_CODE, PATCHED_CHECK_QR_CODE, 1)
        notes.append(
            "checkQrCode: upgraded v3 -> v7 — a pairing-code failure is now "
            "reported to the client, not just written to wppconnect.log."
        )
    elif V2_CHECK_QR_CODE in content:
        content = content.replace(V2_CHECK_QR_CODE, PATCHED_CHECK_QR_CODE, 1)
        notes.append(
            "checkQrCode: upgraded v2 -> v7 — a failing loginByCode() is now "
            "caught, reported and logged instead of escaping as an unhandled "
            "rejection."
        )
    elif V1_CHECK_QR_CODE in content:
        content = content.replace(V1_CHECK_QR_CODE, PATCHED_CHECK_QR_CODE, 1)
        notes.append("checkQrCode: upgraded v1 (unsafe, could freeze forever) -> v7.")
    elif ORIGINAL_CHECK_QR_CODE in content:
        content = content.replace(ORIGINAL_CHECK_QR_CODE, PATCHED_CHECK_QR_CODE, 1)
        notes.append(
            "checkQrCode: patched (v7) — pairing code no longer regenerates on "
            "every QR rotation (60s reuse cooldown), waits for the auth state, "
            "failures are reported."
        )
    else:
        notes.append("checkQrCode: DID NOT MATCH any known source text — left untouched.")

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

    ok = not any("DID NOT MATCH" in note for note in notes)
    return content, notes, ok
