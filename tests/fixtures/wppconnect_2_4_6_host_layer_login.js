/*
 * This file is part of WPPConnect.
 *
 * WPPConnect is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Lesser General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * WPPConnect is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU Lesser General Public License for more details.
 *
 * You should have received a copy of the GNU Lesser General Public License
 * along with WPPConnect.  If not, see <https://www.gnu.org/licenses/>.
 */
// Verbatim excerpt of @wppconnect-team/wppconnect 2.4.6
// dist/api/layers/host.layer.js (HostLayer methods, unmodified; checkQrCode()
// and getQrCode() for the QR path), used by
// tests/test_pairing_completion_patch.py to run WinZapp's host.layer.js
// patches under Node against a fake page. License: LGPL-3.0-or-later, as
// above; the full text is tests/fixtures/group_presence_provider.LICENSE.md.
    async afterPageScriptInjected() {
        this.getWAVersion()
            .then((version) => {
            this.log('info', `WhatsApp WEB version: ${version}`);
        })
            .catch(() => null);
        this.getWAJSVersion()
            .then((version) => {
            this.log('info', `WA-JS version: ${version}`);
        })
            .catch(() => null);
        (0, helpers_1.evaluateAndReturn)(this.page, () => {
            WPP.on('conn.main_ready', window.checkInChat);
        }).catch(() => null);
        if (typeof this.options.phoneNumber === 'string') {
            await (0, helpers_1.evaluateAndReturn)(this.page, () => {
                WPP.on('conn.link_code_change', window.onLinkCode);
                WPP.on('conn.link_code_expired', window.onLinkCodeExpired);
                WPP.on('conn.link_code_error', (error) => window.onLinkCodeError(error.message));
            }).catch(() => null);
            await this.loginByCode(this.options.phoneNumber).catch((error) => this.log('error', error));
        }
        else {
            await (0, helpers_1.evaluateAndReturn)(this.page, () => {
                WPP.on('conn.auth_code_change', window.checkQrCode);
            }).catch(() => null);
            this.checkQrCode().catch((error) => this.log('error', error));
        }
        this.checkInChat().catch((error) => this.log('error', error));
    }
    async checkInChat() {
        const inChat = await (0, auth_1.isInsideChat)(this.page).catch(() => null);
        const wasInChat = this.isInChat;
        this.isInChat = !!inChat;
        if (!inChat || wasInChat) {
            return;
        }
        this.log('http', 'Connected');
        try {
            this.statusFind?.('inChat', this.session);
        }
        catch (error) {
            this.log('error', error);
        }
    }
    async waitForQrCodeScan(catchQR) {
        if (catchQR) {
            this.catchQR = catchQR;
        }
        if (!this.isStarted) {
            throw new Error('waitForQrCodeScan error: Session not started');
        }
        while (!this.page.isClosed() && !this.isLogged) {
            await (0, sleep_1.sleep)(200);
            const needScan = await (0, auth_1.needsToScan)(this.page).catch(() => null);
            if (needScan === null)
                continue;
            this.isLogged = !needScan;
        }
    }
    async waitForInChat() {
        if (!this.isStarted) {
            throw new Error('waitForInChat error: Session not started');
        }
        if (!this.isLogged) {
            return false;
        }
        const start = Date.now();
        while (!this.page.isClosed() && this.isLogged && !this.isInChat) {
            if (this.options.deviceSyncTimeout > 0 &&
                Date.now() - start >= this.options.deviceSyncTimeout) {
                return false;
            }
            await (0, sleep_1.sleep)(1000);
            // main_ready may already have fired on the QR screen, so pairing can
            // finish without another event. Publish only the verified transition.
            await this.checkInChat();
        }
        return this.isInChat;
    }
    async waitForPageLoad() {
        while (!this.page.isClosed()) {
            const pageLoad = this.pageLoadPromise;
            if (!pageLoad) {
                await (0, sleep_1.sleep)(50);
                continue;
            }
            const failure = await pageLoad.then(() => null, (error) => ({ error }));
            // Superseded by a newer load: wait for that injection instead
            if (pageLoad !== this.pageLoadPromise) {
                continue;
            }
            if (failure) {
                throw failure.error;
            }
            if (this.isInjected) {
                return;
            }
            // Superseded by a navigation whose 'load' has not fired yet
            if (this.isInjectionExpired()) {
                throw new browser_1.InjectionTimeoutError(this.options.injectionTimeout);
            }
            await (0, sleep_1.sleep)(50);
        }
        throw new Error('Page closed before WAPI injection completed');
    }
    async waitForLogin() {
        this.log('http', 'Waiting page load');
        await this.waitForPageLoad();
        this.log('http', 'Checking is logged...');
        let authenticated = await (0, auth_1.isAuthenticated)(this.page).catch(() => null);
        this.startAutoClose();
        if (authenticated === false) {
            this.log('http', typeof this.options.phoneNumber === 'string'
                ? 'Waiting for Login by Code...'
                : 'Waiting for QRCode Scan...');
            this.statusFind?.('notLogged', this.session);
            await this.waitForQrCodeScan();
            this.log('http', typeof this.options.phoneNumber === 'string'
                ? 'Checking Login by Code status...'
                : 'Checking QRCode status...');
            // Wait for interface update
            await (0, sleep_1.sleep)(200);
            authenticated = await (0, auth_1.isAuthenticated)(this.page).catch(() => null);
            if (authenticated === null) {
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
            }
        }
        else if (authenticated === true) {
            this.log('http', 'Authenticated');
            this.statusFind?.('isLogged', this.session);
        }
        if (authenticated === true) {
            // A stored session takes the "Authenticated" branch above without going
            // through waitForQrCodeScan(), so nothing else may have recorded the
            // login yet. waitForInChat() bails out early when isLogged is false.
            this.isLogged = true;
            // Reinicia o contador do autoclose
            this.cancelAutoClose();
            // Wait for interface update
            await (0, sleep_1.sleep)(200);
            this.startAutoClose(this.options.deviceSyncTimeout);
            this.log('http', 'Checking phone is connected...');
            const inChat = await this.waitForInChat();
            if (!inChat) {
                this.log('warn', 'Phone not connected');
                this.statusFind?.('phoneNotConnected', this.session);
                this.tryAutoClose();
                throw new Error('Phone not connected');
            }
            await this.waitForPageLoad();
            this.cancelAutoClose();
            return true;
        }
        this.tryAutoClose();
        if (this.autoCloseCalled) {
            this.log('error', 'Auto Close Called');
            throw new Error('Auto Close Called');
        }
        if (this.page.isClosed()) {
            this.log('error', 'Page Closed');
            throw new Error('Page Closed');
        }
        this.log('error', 'Unknow error');
        throw new Error('Unknow error');
    }
    async checkQrCode() {
        if (this.qrLoopCancelled || this.page.isClosed())
            return;
        const needScan = await (0, auth_1.needsToScan)(this.page).catch(() => null);
        if (this.qrLoopCancelled || this.page.isClosed())
            return;
        // A navigation can invalidate the execution context while waiting for QR.
        // An unknown result is not proof that the session has registered.
        if (needScan === null)
            return;
        this.isLogged = !needScan;
        if (!needScan) {
            this.attempt = 0;
            return;
        }
        const result = await this.getQrCode();
        if (this.qrLoopCancelled || this.page.isClosed())
            return;
        if (!result?.urlCode || this.urlCode === result.urlCode) {
            return;
        }
        this.urlCode = result.urlCode;
        this.attempt++;
        let qr = '';
        if (this.options.logQR || this.catchQR) {
            qr = await (0, auth_1.asciiQr)(this.urlCode);
        }
        if (this.qrLoopCancelled || this.page.isClosed())
            return;
        if (this.options.logQR) {
            this.log('info', `Waiting for QRCode Scan (Attempt ${this.attempt})...:\n${qr}`, { code: this.urlCode });
        }
        else {
            this.log('verbose', `Waiting for QRCode Scan: Attempt ${this.attempt}`);
        }
        this.catchQR?.(result.base64Image, qr, this.attempt, result.urlCode);
    }
    async getQrCode() {
        let qrResult;
        qrResult = await (0, helpers_1.scrapeImg)(this.page).catch(() => undefined);
        return qrResult;
    }
