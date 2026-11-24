"""host.layer.js: once the phone has accepted a pairing, the pairing ends —
connected, or a clear error on the pairing dialog. Never a silent wait.

macOS report, 2026-11-24 (WinZapp 3.0.0.5928alpha, wppconnect 2.4.6, link by
phone code). wppconnect.log, in order:

    Checking Login by Code status...
    Login with success                 <- statusFind qrReadSuccess
    Checking phone is connected...     <- waitForInChat() starts
    pinned document served (#2)        <- WhatsApp Web's post-link reload
    wapi.js injected
    Pairing code already accepted; not requesting another on reload.
    WhatsApp WEB version: ...

and then nothing for twenty minutes, while the phone listed the new linked
device and the pairing dialog went on saying "Enter the pairing code".

Why nothing could end it, read from the code:

* waitForInChat() polls checkInChat() once a second until isInsideChat()
  (`WPP.conn.isRegistered() && WPP.conn.isMainReady()`) answers true. Its only
  bound is `deviceSyncTimeout`, which WinZapp pins to 0 (config.ts) so that
  WPPConnect never closes a session behind the user's back. With 0 the loop has
  no end at all.
* create() only returns after waitForLogin(), and the server sets CONNECTED
  and emits `session-logged` — the one event that closes the pairing dialog —
  only after create() has returned (CreateSessionUtil.start()). statusFind is
  deliberately not forwarded to the client. So a page that never reaches
  main_ready holds the whole pairing open, with nothing on the wire.
* wa-js latches isMainReady() from its own `conn.main_ready` event, which it
  emits when it sees the stream mode change to MAIN (or QR) after injection.
  Nothing re-reads the current mode, so the latch depends on the event being
  seen on that page. Stream mode MAIN is exactly what the latch stands for.

What this patch does, each part on its own anchor:

1. checkInChat() also accepts `isRegistered() && Stream.mode === 'MAIN'` —
   main_ready's own definition, read directly instead of through the latch.
2. waitForInChat() is bounded after a link made in this run (and only then,
   and only while deviceSyncTimeout is 0): at 90 s it reloads the page once,
   which is what restarting WinZapp did; at 240 s it either carries on
   (WhatsApp's socket is CONNECTED: the server's own start() then decides
   CONNECTED from the state event, and a large account's history sync is not
   cut short) or reports `PairingNotCompleted` to the pairing dialog and lets
   waitForLogin() fail as upstream does for "Phone not connected".
3. waitForLogin()'s post-scan check (v2 of the 2026-11-23 patch) records the
   link for part 2, stops waiting at once when the browser is gone, and reports
   `PairingNotCompleted` when the login is never confirmed or is refused —
   both used to throw inside create() with nothing reaching the dialog.
4. The page-load guard (v2) also records the login it sees, so a scan wait
   still running resumes on it. It still never asks for a new code once the
   code was accepted.
5. waitForQrCodeScan() (2.4.0+) ends at once when the browser is gone. The
   same report showed a session the client had force-closed probing a dead
   target ("detached Frame") for 30 s before giving up and throwing
   "Unknow error".

`PairingNotCompleted` travels on the existing `catchLinkCodeError` option, the
channel the server already forwards as `phoneCodeError`; it is passed for QR
pairings too, and the server drops it for a session being closed
(`shouldClose`). The client announces it while the pairing dialog is up.
"""

from core import wppconnect_host_layer_patch as host

# --- 1. checkInChat ----------------------------------------------------------

ORIGINAL_CHECK_IN_CHAT_PROBE = (
    "    async checkInChat() {\n"
    "        const inChat = await (0, auth_1.isInsideChat)(this.page).catch(() => null);\n"
)

PATCHED_CHECK_IN_CHAT_PROBE = (
    "    async checkInChat() {\n"
    "        // WinZapp: isMainReady() is a latch wa-js sets from its own\n"
    "        // conn.main_ready event, emitted when it sees the stream mode become\n"
    "        // MAIN after injection. Stream mode MAIN is what that latch means, so\n"
    "        // read it directly too: a page that missed the event still counts.\n"
    "        const inChat = (await (0, auth_1.isInsideChat)(this.page).catch(() => null)) ||\n"
    "            (await this.page\n"
    "                .evaluate(() => WPP.conn.isRegistered() && WPP.whatsapp.Stream.mode === 'MAIN')\n"
    "                .catch(() => null));\n"
)

# --- 2. waitForInChat --------------------------------------------------------

ORIGINAL_WAIT_FOR_IN_CHAT = (
    "    async waitForInChat() {\n"
    "        if (!this.isStarted) {\n"
    "            throw new Error('waitForInChat error: Session not started');\n"
    "        }\n"
    "        if (!this.isLogged) {\n"
    "            return false;\n"
    "        }\n"
    "        const start = Date.now();\n"
    "        while (!this.page.isClosed() && this.isLogged && !this.isInChat) {\n"
    "            if (this.options.deviceSyncTimeout > 0 &&\n"
    "                Date.now() - start >= this.options.deviceSyncTimeout) {\n"
    "                return false;\n"
    "            }\n"
    "            await (0, sleep_1.sleep)(1000);\n"
    "            // main_ready may already have fired on the QR screen, so pairing can\n"
    "            // finish without another event. Publish only the verified transition.\n"
    "            await this.checkInChat();\n"
    "        }\n"
    "        return this.isInChat;\n"
    "    }\n"
)

#: After a link made in this run, how long waitForInChat() waits before
#: reloading the page once, and before ending the wait.
PAIRING_RELOAD_AFTER_MS = 90000
PAIRING_GIVE_UP_AFTER_MS = 240000

PATCHED_WAIT_FOR_IN_CHAT = (
    "    wzReportPairingNotCompleted(reason) {\n"
    "        // WinZapp: the pairing dialog closes only on session-logged, which the\n"
    "        // server sends after create() returns. A pairing that ends any other\n"
    "        // way has to say so, or the dialog waits forever.\n"
    "        if (this.wzPairingReported) {\n"
    "            return;\n"
    "        }\n"
    "        this.wzPairingReported = true;\n"
    "        this.log('error', `Pairing did not complete: ${reason}`);\n"
    "        try {\n"
    "            this.options.catchLinkCodeError?.({\n"
    "                name: 'PairingNotCompleted',\n"
    "                message: reason,\n"
    "                session: this.session,\n"
    "            });\n"
    "        }\n"
    "        catch (error) {\n"
    "            this.log('error', error);\n"
    "        }\n"
    "    }\n"
    "    async waitForInChat() {\n"
    "        if (!this.isStarted) {\n"
    "            throw new Error('waitForInChat error: Session not started');\n"
    "        }\n"
    "        if (!this.isLogged) {\n"
    "            return false;\n"
    "        }\n"
    "        const start = Date.now();\n"
    "        // WinZapp: deviceSyncTimeout is pinned to 0, which leaves this loop\n"
    "        // without an end. After a link made in this run that is a pairing\n"
    "        // dialog left open for good, so bound it: reload once, then either\n"
    "        // carry on (the socket is connected; the server decides CONNECTED\n"
    "        // from the state event) or report the pairing as not completed.\n"
    "        const wzPairing = this.wzLinkedThisRun === true && !(this.options.deviceSyncTimeout > 0);\n"
    "        let wzReloaded = false;\n"
    "        while (!this.page.isClosed() && this.isLogged && !this.isInChat) {\n"
    "            if (this.options.deviceSyncTimeout > 0 &&\n"
    "                Date.now() - start >= this.options.deviceSyncTimeout) {\n"
    "                return false;\n"
    "            }\n"
    f"            if (wzPairing && !wzReloaded && Date.now() - start >= {PAIRING_RELOAD_AFTER_MS}) {{\n"
    "                wzReloaded = true;\n"
    f"                this.log('warn', 'Linked, but WhatsApp Web has not opened the chats {PAIRING_RELOAD_AFTER_MS // 1000}s after the login; reloading the page once.');\n"
    "                await this.page.reload().catch((error) => this.log('warn', `Reload after linking failed: ${error?.message || error}`));\n"
    "                await this.waitForPageLoad().catch(() => null);\n"
    "            }\n"
    f"            if (wzPairing && Date.now() - start >= {PAIRING_GIVE_UP_AFTER_MS}) {{\n"
    "                const wzState = await this.page\n"
    "                    .evaluate(() => ({ socket: WPP.whatsapp.Socket.state, stream: WPP.whatsapp.Stream.mode }))\n"
    "                    .catch(() => null);\n"
    "                if (wzState?.socket === 'CONNECTED') {\n"
    f"                    this.log('warn', `Linked and connected, but WhatsApp Web has not opened the chats {PAIRING_GIVE_UP_AFTER_MS // 1000}s after the login (stream ${{wzState.stream}}); continuing without waiting for them.`);\n"
    "                    return true;\n"
    "                }\n"
    f"                this.wzReportPairingNotCompleted(`WhatsApp Web did not connect within {PAIRING_GIVE_UP_AFTER_MS // 1000}s of the login (socket ${{wzState?.socket ?? 'unknown'}}, stream ${{wzState?.stream ?? 'unknown'}}).`);\n"
    "                return false;\n"
    "            }\n"
    "            await (0, sleep_1.sleep)(1000);\n"
    "            // main_ready may already have fired on the QR screen, so pairing can\n"
    "            // finish without another event. Publish only the verified transition.\n"
    "            await this.checkInChat();\n"
    "        }\n"
    "        return this.isInChat;\n"
    "    }\n"
)

# --- 3. waitForLogin's post-scan check --------------------------------------

_POST_SCAN_OUTCOME = """
                this.log('warn', 'Failed to authenticate');
                this.statusFind?.('qrReadError', this.session);
            }
            else if (authenticated) {
                this.log('http', 'Login with success');
                this.statusFind?.('qrReadSuccess', this.session);
            }
            else {
                this.log('warn', 'Login Fail');
                this.statusFind?.('qrReadFail', this.session);
                this.tryAutoClose();
                throw new Error('Failed to read the QRCode');
            }"""

ORIGINAL_POST_SCAN_CHECK = host.ORIGINAL_POST_SCAN_AUTH_CHECK + _POST_SCAN_OUTCOME
V1_POST_SCAN_CHECK = host.PATCHED_POST_SCAN_AUTH_CHECK + _POST_SCAN_OUTCOME

PATCHED_POST_SCAN_CHECK = """            // Wait for interface update
            await (0, sleep_1.sleep)(200);
            authenticated = await (0, auth_1.isAuthenticated)(this.page).catch(() => null);
            // WhatsApp Web reloads the page right after a successful link; until
            // the new page is injected the probe throws (null). Wait it out
            // instead of closing the browser before the login is saved. A
            // browser that is gone (the session was closed) ends the wait.
            const wzBrowserGone = () => this.page.isClosed() || this.page.browser?.()?.isConnected?.() === false;
            for (let i = 0; authenticated === null && i < 120 && !wzBrowserGone(); i++) {
                await this.waitForPageLoad().catch(() => null);
                await (0, sleep_1.sleep)(500);
                authenticated = await (0, auth_1.isAuthenticated)(this.page).catch(() => null);
            }
            if (authenticated === null) {
                this.log('warn', 'Failed to authenticate');
                this.statusFind?.('qrReadError', this.session);
                if (!wzBrowserGone()) {
                    this.wzReportPairingNotCompleted('WhatsApp Web never confirmed the login after the pairing.');
                }
            }
            else if (authenticated) {
                // Lets waitForInChat() bound its wait: the deviceSyncTimeout it
                // would use is pinned to 0.
                this.wzLinkedThisRun = true;
                this.log('http', 'Login with success');
                this.statusFind?.('qrReadSuccess', this.session);
            }
            else {
                this.log('warn', 'Login Fail');
                this.statusFind?.('qrReadFail', this.session);
                if (!wzBrowserGone()) {
                    this.wzReportPairingNotCompleted('WhatsApp Web reported the device as not linked after the pairing.');
                }
                this.tryAutoClose();
                throw new Error('Failed to read the QRCode');
            }"""

# PR #886's first cut (d61f838cc), shipped in builds up to 5915, flagged the
# success branch instead. An install patched by it is upgraded, not reported.
V0_POST_SCAN_CHECK = host.PATCHED_POST_SCAN_AUTH_CHECK + _POST_SCAN_OUTCOME.replace(
    "                this.log('http', 'Login with success');",
    "                this.__wzCodeAccepted = true;\n"
    "                this.log('http', 'Login with success');",
    1,
)
assert V0_POST_SCAN_CHECK != V1_POST_SCAN_CHECK

# --- 4. the page-load guard --------------------------------------------------

V1_RELOAD_GUARD = host.PATCHED_RELOAD_LOGIN_BY_CODE
ORIGINAL_RELOAD_GUARD = host.ORIGINAL_RELOAD_LOGIN_BY_CODE

# The same first cut's guard. Its else branch still holds the upstream line,
# so the v1 patcher, run over it after an update, wrapped that line again:
# V0_NESTED_RELOAD_GUARD is what such an install now carries. Checked before
# the other two, since both are substrings of it.
V0_RELOAD_GUARD = """            // After a code was accepted WhatsApp Web reloads to finish logging
            // in. Asking for a new code on that page throws the login away.
            if (this.__wzCodeAccepted || this.isLogged) {
                this.log('verbose', 'Pairing code already accepted — not requesting another on reload.');
            }
            else {
                await this.loginByCode(this.options.phoneNumber).catch((error) => this.log('error', error));
            }"""
V0_NESTED_RELOAD_GUARD = V0_RELOAD_GUARD.replace(
    ORIGINAL_RELOAD_GUARD, V1_RELOAD_GUARD, 1
)
assert V0_NESTED_RELOAD_GUARD != V0_RELOAD_GUARD

PATCHED_RELOAD_GUARD = V1_RELOAD_GUARD.replace(
    """            if (wzLoggedIn) {
                this.log('verbose', 'Pairing code already accepted; not requesting another on reload.');
            }""",
    """            if (wzLoggedIn) {
                // Record what the probe saw: a scan wait still running ends on it
                // and waitForLogin() carries the login on to the chats.
                this.isLogged = true;
                this.log('verbose', 'Pairing code already accepted; not requesting another on reload.');
            }""",
    1,
)
assert PATCHED_RELOAD_GUARD != V1_RELOAD_GUARD

# --- 5. waitForQrCodeScan (2.4.0+) -------------------------------------------

V1_WAIT_FOR_QR_CODE_SCAN = host.PATCHED_V240_WAIT_FOR_QR_CODE_SCAN

PATCHED_WAIT_FOR_QR_CODE_SCAN = V1_WAIT_FOR_QR_CODE_SCAN.replace(
    "            catch (error) {\n"
    "                probeFailures++;\n",
    "            catch (error) {\n"
    "                // WinZapp: a closed session's browser is gone for good; probing\n"
    "                // its detached frame for 30s only delays the failure.\n"
    "                if (this.page.isClosed() || this.page.browser?.()?.isConnected?.() === false) {\n"
    "                    this.log('warn', `Browser is gone; ending the scan wait (${error?.name || 'Error'}: ${error?.message || error}).`);\n"
    "                    return;\n"
    "                }\n"
    "                probeFailures++;\n",
    1,
)
assert PATCHED_WAIT_FOR_QR_CODE_SCAN != V1_WAIT_FOR_QR_CODE_SCAN


# (name, anchor that says this runtime has the function, shipped older
#  patched texts to upgrade, upstream text, patched text)
STEPS = (
    ("waitForQrCodeScan browser-gone check", "async waitForQrCodeScan(catchQR)",
     (V1_WAIT_FOR_QR_CODE_SCAN,), None, PATCHED_WAIT_FOR_QR_CODE_SCAN),
    ("waitForLogin post-scan check", "async waitForLogin()",
     (V1_POST_SCAN_CHECK, V0_POST_SCAN_CHECK), ORIGINAL_POST_SCAN_CHECK, PATCHED_POST_SCAN_CHECK),
    # Both texts belong to the runtime whose waitForLogin() this fix is for:
    # older runtimes (2.3.x) also have afterPageScriptInjected(), with another
    # loginByCode() call that must not raise the alarm.
    ("page-load pairing request", "async waitForLogin()",
     (V0_NESTED_RELOAD_GUARD, V0_RELOAD_GUARD, V1_RELOAD_GUARD), ORIGINAL_RELOAD_GUARD, PATCHED_RELOAD_GUARD),
    ("checkInChat stream-mode check", "async waitForInChat()",
     (), ORIGINAL_CHECK_IN_CHAT_PROBE, PATCHED_CHECK_IN_CHAT_PROBE),
    ("waitForInChat pairing bound", "async waitForInChat()",
     (), ORIGINAL_WAIT_FOR_IN_CHAT, PATCHED_WAIT_FOR_IN_CHAT),
)


def patch_pairing_completion(content: str, notes: list) -> str:
    """Apply STEPS in order. Idempotent; a function whose text moved is
    reported as DID NOT MATCH, never guessed at."""
    for name, anchor, older, original, patched in STEPS:
        if patched in content:
            notes.append(f"{name}: already applied.")
            continue
        old = next((text for text in older if text in content), None)
        if old is not None:
            content = content.replace(old, patched, 1)
            notes.append(f"{name}: upgraded — a pairing the phone accepted ends connected or with an error on the pairing dialog.")
        elif original is not None and original in content:
            content = content.replace(original, patched, 1)
            notes.append(f"{name}: patched — a pairing the phone accepted ends connected or with an error on the pairing dialog.")
        elif anchor not in content:
            notes.append(f"{name}: not in this file — nothing to patch.")
        elif original is None:
            # waitForQrCodeScan's upstream text is _patch_qr_reads()'s to
            # replace first; anything else there is its alarm, not this one's.
            notes.append(f"{name}: no known patched scan wait to extend — left untouched.")
        else:
            notes.append(f"{name}: DID NOT MATCH the known source text — left untouched.")
    return content
