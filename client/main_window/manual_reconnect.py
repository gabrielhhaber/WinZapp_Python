"""ManualReconnectMixin — Sync > "Reconnect now" (core/manual_reconnect.py).

Runs off the wx thread: probing WhatsApp's host, the Socket.IO handshake and
a session restart all block for seconds. One at a time; the outcome is said.
"""

import logging
import threading
import time

import wx

from core import manual_reconnect

#: how long a restarted session is waited for before saying "still restarting"
RESTART_WAIT_SECONDS = 60


class ManualReconnectMixin:
    def reconnect_now(self, _event=None):
        i18n = self.i18n
        if getattr(self, "_manual_reconnect_running", False):
            self.output(i18n.t("reconnect_started"), interrupt=True)
            return
        self._manual_reconnect_running = True
        self.output(i18n.t("reconnect_started"), interrupt=True)
        if getattr(self, "_user_offline", False):
            # asking to connect is asking to leave the offline mode chosen by hand
            self._user_offline = False
            self._apply_offline_state()
        threading.Thread(target=self._manual_reconnect_worker, daemon=True,
                         name="manual-reconnect").start()

    def _manual_reconnect_worker(self):
        outcome = manual_reconnect.FAILED
        try:
            outcome = manual_reconnect.reconnect(
                self._probe_whatsapp_host,
                self._reconnect_websocket_now,
                self._manual_reconnect_connected,
                self._manual_reconnect_restart,
                self._manual_reconnect_wait)
        except Exception:
            logging.exception("[reconnect] the manual reconnect failed")
        finally:
            self._manual_reconnect_running = False
        logging.info("[reconnect] manual reconnect: %s", outcome)
        if not getattr(self, "_shutting_down", False):
            wx.CallAfter(self.output, self.i18n.t(manual_reconnect.OUTCOME_KEYS[outcome]), True)

    def _manual_reconnect_connected(self) -> bool:
        """The engine asked now (the health check updates _wa_connected)."""
        try:
            self.check_wa_connection_http()
        except Exception:
            logging.info("[reconnect] the connection check failed", exc_info=True)
        return bool(getattr(self, "_wa_connected", False))

    def _manual_reconnect_restart(self) -> bool:
        """The session restarted in place; asked by the user, so the cooldown
        between automatic restarts does not apply."""
        self._last_wpp_session_restart_ts = 0
        return bool(self._restart_wpp_session(reason="the user asked to reconnect"))

    def _manual_reconnect_wait(self, timeout=RESTART_WAIT_SECONDS, sleep=time.sleep) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if getattr(self, "_shutting_down", False):
                return False
            if self._manual_reconnect_connected():
                return True
            sleep(3)
        return False
