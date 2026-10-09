"""One bounded active-chat refresh; workers never render captured old notes."""
import threading
import time
from urllib.parse import quote

import wx
from core.api_client import api_get
from core.contact_presence import aliases, cached, is_subscribed, merge
from core.contact_presence_query import query_targets, read_snapshot
from core.conversation_view import conversation_in_view


class ContactPresenceMixin:
    def _fetch_contact_presence(self, jid):
        targets = query_targets(self._normalize_jid(jid),
                                getattr(self, "_lid_to_phone", {}),
                                getattr(self, "_phone_to_lid", {}))
        if not targets:
            return None
        endpoint = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/last-seen/"
        headers = {"Authorization": f"Bearer {self.token}"}

        def request(target, timeout):
            phone = target if target.endswith("@lid") else target.split("@")[0]
            return api_get(endpoint + quote(phone, safe=''), headers=headers, timeout=timeout)

        return read_snapshot(targets, request, time.monotonic)

    def _refresh_open_contact_presence(self):
        panel = getattr(self, "conversations_panel", None)
        if (getattr(self, "_shutting_down", False)
                or panel is None or panel.conversation is None):
            return
        jid = panel.conversation.get("remoteJid", "")
        panel._refresh_presence_note(jid)
        if (not conversation_in_view(panel) or not self.IsShown()
                or self.IsIconized() or not self.IsActive()
                or getattr(self, "_window_hidden", False)
                or not getattr(self, "_wa_connected", False)):
            return
        if not jid.endswith(("@s.whatsapp.net", "@c.us", "@lid")):
            return
        if getattr(self, "_contact_presence_inflight", False):
            return
        now = time.monotonic()
        key = aliases(self, jid)[0]
        attempts = getattr(self, "_contact_presence_attempts", {})
        if now - attempts.get(key, -30) < 10:
            return
        self._contact_presence_attempts = attempts
        attempts[key] = now
        visit = getattr(panel, "_contact_presence_visit", 0)
        epoch = getattr(self, "_contact_presence_epoch", 0)
        identity = aliases(self, jid)
        query_identity = query_targets(self._normalize_jid(jid),
                                       getattr(self, "_lid_to_phone", {}),
                                       getattr(self, "_phone_to_lid", {}))
        revision = cached(self, jid, fresh=False) or None
        # The subscription outlives the tick; only a failed one or a new
        # connection epoch (cache cleared) needs asking again.
        if not is_subscribed(self, jid):
            self.subscribe_presence(jid)
        self._contact_presence_inflight = True

        def worker():
            try:
                result = self._fetch_contact_presence(jid)
            except Exception:
                result = None

            def finish():
                self._contact_presence_inflight = False
                same_visit = (getattr(panel, "_contact_presence_visit", 0) == visit
                              and panel.conversation is not None
                              and bool(panel.conversation.get("remoteJid"))
                              and aliases(self, panel.conversation.get("remoteJid", ""))[0] == aliases(self, jid)[0])
                if (result is not None and same_visit
                        and not getattr(self, "_shutting_down", False)
                        and getattr(self, "_wa_connected", False)
                        and epoch == getattr(self, "_contact_presence_epoch", 0)
                        and identity == aliases(self, jid)
                        and query_identity == query_targets(self._normalize_jid(jid),
                                                            getattr(self, "_lid_to_phone", {}),
                                                            getattr(self, "_phone_to_lid", {}))
                        and revision is (cached(self, jid, fresh=False) or None)):
                    merge(self, jid, result)
                # Read the current cache, including events received during HTTP.
                self._refresh_open_contact_presence_note()
                if not same_visit or epoch != getattr(self, "_contact_presence_epoch", 0):
                    self._refresh_open_contact_presence()

            wx.CallAfter(finish)

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self._contact_presence_inflight = False
            raise

    def _refresh_open_contact_presence_note(self):
        if getattr(self, "_shutting_down", False):
            return
        panel = getattr(self, "conversations_panel", None)
        if panel is not None and panel.conversation is not None:
            panel._refresh_presence_note(panel.conversation.get("remoteJid", ""))

    def _invalidate_contact_presence(self):
        self._contact_presence_epoch = getattr(self, "_contact_presence_epoch", 0) + 1
        self._presence_cache.clear()
        self._contact_presence_attempts = {}
        self._subscribed_presence_cache = {}
        for timer in getattr(self, "_presence_timers", {}).values():
            timer.Stop()
        self._presence_timers = {}
        self._composing_chats = {}
        panel = getattr(self, "conversations_panel", None)
        if panel is not None and hasattr(panel, "refresh_typing_row"):
            panel.refresh_typing_row()
        self._refresh_open_contact_presence_note()
