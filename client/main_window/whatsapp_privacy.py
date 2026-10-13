"""WhatsAppPrivacyMixin — part of MainWindow (see main_window/__init__.py).

The paired account's own privacy settings, as File > WhatsApp settings
(ui/dialogs/whatsapp_settings_dialog.py) shows them. They live on WhatsApp, not
in WinZapp's settings, and are reached through the ``/privacy`` routes that
api_patches adds (privacyController.ts), since WPPConnect Server has none.

Every method is safe to call from a worker thread (no wx object) and returns
instead of raising: None on failure for reads, an error string (or None on
success) for writes, so the dialog can list each failure on its own. Nothing
about a contact reaches the log.

Methods run with ``self`` bound to the MainWindow instance.
"""

import logging

from core import whatsapp_privacy
from core.api_client import api_post
from core.utils import contact_dedup_key


class WhatsAppPrivacyMixin:
    """Account privacy and Status audience of the paired WhatsApp account."""

    #: Seconds. A privacy call is quick; a frozen page is not worth waiting on.
    _PRIVACY_TIMEOUT = 15

    def _privacy_post(self, route: str, payload: dict):
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/{route}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        return api_post(url, json=payload, headers=headers, timeout=self._PRIVACY_TIMEOUT)

    @staticmethod
    def _privacy_error(response) -> str:
        """The error code a privacy route answered with (and, for a failed
        setter, the kind of exception), never the response text."""
        try:
            body = response.json()
        except Exception:
            return f"HTTP {response.status_code}"
        if not isinstance(body, dict):
            return f"HTTP {response.status_code}"
        code = body.get("code") or f"HTTP {response.status_code}"
        kind = body.get("kind")
        return f"{code} ({kind})" if kind else str(code)

    def open_whatsapp_settings(self, event=None):
        """File > WhatsApp settings."""
        from ui.dialogs.whatsapp_settings_dialog import WhatsAppSettingsDialog

        dialog = WhatsAppSettingsDialog(self, self)
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    def fetch_privacy_settings(self) -> "dict | None":
        """``{"settings": {...six account settings...}, "status": {...} | None}``,
        or None on any failure — the caller treats that as "could not load"."""
        try:
            response = self._privacy_post("privacy", {})
            if response.status_code not in (200, 201):
                logging.warning("[privacy] read failed: %s", self._privacy_error(response))
                return None
            body = response.json()
        except Exception as exc:
            logging.warning("[privacy] read failed: %s", type(exc).__name__)
            return None
        answer = body.get("response") if isinstance(body, dict) else None
        if not isinstance(answer, dict) or not isinstance(answer.get("settings"), dict):
            logging.warning("[privacy] read answered without settings")
            return None
        return answer

    def set_privacy_setting(self, setting: str, value: str) -> "str | None":
        """One account setting (lastSeen, online, about, profilePicture,
        readReceipts, groupAdd). None on success, else an error string."""
        try:
            response = self._privacy_post("privacy/set", {"setting": setting, "value": value})
        except Exception as exc:
            return type(exc).__name__
        if response.status_code in (200, 201):
            return None
        error = self._privacy_error(response)
        logging.warning("[privacy] %s not applied: %s", setting, error)
        return error

    def status_list_id(self, jid: str) -> str:
        """The id WhatsApp's status list takes for *jid*: its @lid when the
        bridge knows one, else the phone form (core.whatsapp_privacy)."""
        return whatsapp_privacy.status_list_id(jid, getattr(self, "_phone_to_lid", None))

    def set_status_privacy(self, setting: str, people: list) -> "tuple[bool, str | None]":
        """Who can see my Status. Returns (confirmed, error): *error* is None
        when the server took the call, and *confirmed* says whether a fresh read
        then showed WhatsApp holding that setting with exactly those people."""
        try:
            response = self._privacy_post(
                "privacy/status", {"setting": setting, "list": list(people)})
        except Exception as exc:
            return False, type(exc).__name__
        if response.status_code not in (200, 201):
            error = self._privacy_error(response)
            logging.warning("[privacy] status audience not applied: %s", error)
            return False, error
        answer = self.fetch_privacy_settings()
        reported = whatsapp_privacy.parse_status_privacy(answer.get("status") if answer else None)
        confirmed = whatsapp_privacy.status_list_confirmed(
            list(people), reported, setting, lambda jid: contact_dedup_key(self, jid))
        if not confirmed:
            logging.warning("[privacy] status audience not confirmed by the read-back")
        return confirmed, None
