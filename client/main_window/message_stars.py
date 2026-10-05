"""One star write followed by a read; ambiguous writes are never retried."""

from urllib.parse import quote
import logging
import requests
import wx

from core.api_client import api_get, api_post
from core.message_edit import connection_refused, response_not_sent
from core.message_stars import STAR_FIELDS, apply_remote_star


class MessageStarsMixin:
    def _invalidate_star_sync_for_lock(self):
        job = getattr(self, "_star_sync_job", None)
        if isinstance(job, dict) and self.is_chat_locked(job.get("jid", "")):
            job["invalidated"] = True

    def _insert_message_preserving_stars(self, jid, message):
        """Worker-only: recover flags on an old row outside the resident page."""
        saved = self.db.merge_message_star_states(jid, [message])[0]
        self.db.insert_message(jid, saved)
        if bool(saved.get("starred")) != bool(message.get("starred")):
            state = {k: saved[k] for k in STAR_FIELDS if k in saved}
            wx.CallAfter(self._apply_preserved_star, jid, (message.get("key") or {}).get("id"), state)

    def _apply_preserved_star(self, jid, mid, state):
        if (getattr(self, "_shutting_down", False) or
                (self.is_chat_locked(jid) and not getattr(self, "_chat_lock_unlocked", False))):
            return
        records = self.chats.get(jid, {}).get("messages", {}).get("messages", {}).get("records", [])
        message = next((m for m in records if (m.get("key") or {}).get("id") == mid), None)
        panel = getattr(self, "conversations_panel", None)
        if message is not None and apply_remote_star(message, state):
            if panel is not None and (panel.conversation or {}).get("remoteJid") == jid:
                panel._repaint_or_repopulate([mid])

    def _persist_and_repaint_star(self, message, jid):
        mid = (message.get("key") or {}).get("id")
        state = {k: message[k] for k in STAR_FIELDS if k in message}

        def persist():
            try:
                self.db.update_message_star_state(jid, mid, state)
            except Exception:
                logging.exception("[star] could not persist a remote star observation")

        self._msg_bg_executor.submit(persist)
        panel = getattr(self, "conversations_panel", None)
        if panel is not None and (panel.conversation or {}).get("remoteJid") == jid:
            wx.CallAfter(panel._repaint_or_repopulate, [mid])

    def star_message(self, jid, key, star):
        """Return confirmed/refused/unknown; the setter's old-state echo is not ACK.

        WA-JS 4.6.1 builds StarMessageReturn before sendStarMsgs, so its `star`
        is the PREVIOUS value. Read the message again instead of trusting it.
        This reads the linked device's store; it does not request phone history.
        """
        target = getattr(self, "_phone_to_lid", {}).get(jid) or jid
        message_id = self._serialize_msg_id(target, key)
        if not key.get("id") or not message_id:
            return "refused"
        base = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}"
        headers = {"Authorization": f"Bearer {self._get_wa_token()}"}
        try:
            response = api_post(base + "/star-message", json={
                "messageId": message_id, "star": bool(star)}, headers=headers, timeout=15)
            if response.status_code in (400, 401, 403, 404, 422, 429) or response_not_sent(response.text):
                return "refused"
        except requests.exceptions.ConnectTimeout:
            return "refused"
        except requests.exceptions.ConnectionError as exc:
            if connection_refused(exc):
                return "refused"
        except Exception:
            logging.warning("[star] write outcome unknown; checking without retry")
        try:
            response = api_get(base + "/message-by-id/" + quote(message_id, safe=""),
                               headers=headers, timeout=15)
            body = response.json() if response.status_code in (200, 201) else {}
            raw = body.get("response") if isinstance(body, dict) else None
            # deviceController.returnSucess wraps getMessageById under data.
            if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
                raw = raw["data"]
            raw_id = raw.get("id") if isinstance(raw, dict) else None
            if isinstance(raw_id, dict):
                raw_id = raw_id.get("_serialized")
            if (isinstance(body, dict) and str(body.get("status", "")).lower() == "success"
                    and isinstance(raw_id, str) and raw_id == message_id
                    and isinstance(raw.get("star"), bool) and raw["star"] is bool(star)):
                return "confirmed"
        except Exception:
            logging.warning("[star] could not verify the linked-device star state")
        return "unknown"
