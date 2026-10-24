"""One bounded active-chat refresh; workers never render captured old notes."""
import threading
import time
from urllib.parse import quote

import wx
from core.api_client import api_get
from core.contact_presence import aliases, cached, is_subscribed
from core.contact_presence_query import PresenceRead, query_targets, read_presence
from core.contact_presence_recovery import PendingPresenceRecovery
from core.contact_presence_refresh import (
    DeferredPresenceRefresh, current_scope, needs_snapshot, take_readiness_followup,
)
from ui.dialogs.personal_presence import notify_personal_presence
from main_window.presence_work import expire_presence_read, presence_work, wake_waiting_presence
from main_window.presence_visibility import contact_presence_in_view


class ContactPresenceMixin:
    def _fetch_contact_presence(self, jid):
        return self._query_contact_presence(jid).snapshot

    def _query_contact_presence(self, jid):
        targets = query_targets(self._normalize_jid(jid),
                                getattr(self, "_lid_to_phone", {}),
                                getattr(self, "_phone_to_lid", {}))
        if not targets:
            return PresenceRead("unsupported")
        endpoint = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/last-seen/"
        headers = {"Authorization": f"Bearer {self.token}"}

        def request(target, timeout):
            phone = target if target.endswith("@lid") else target.split("@")[0]
            return api_get(endpoint + quote(phone, safe=''), headers=headers, timeout=timeout)

        return read_presence(targets, request, time.monotonic)

    def _cancel_deferred_contact_presence(self):
        pending = getattr(self, "_contact_presence_deferred", None)
        if pending is not None:
            pending.cancel()

    def _defer_contact_presence(self, scope, delay=None):
        if not needs_snapshot(self):
            self._cancel_deferred_contact_presence()
            return
        pending = getattr(self, "_contact_presence_deferred", None)
        if pending is None:
            pending = self._contact_presence_deferred = DeferredPresenceRefresh()
        pending.remember(scope)
        if delay is not None:
            pending.schedule(scope, delay, wx.CallLater, self._retry_contact_presence)

    def _retry_contact_presence(self, scope):
        pending = getattr(self, "_contact_presence_deferred", None)
        if pending is None or pending.pending != scope:
            return
        if current_scope(self) != scope or not needs_snapshot(self):
            pending.cancel()
            return
        self._refresh_open_contact_presence()

    def _on_contact_presence_subscription_finished(self, targets, epoch, cache, routing):
        if (epoch != getattr(self, "_contact_presence_epoch", 0)
                or routing != (self.wpp_server, self.wpp_port, self.token)
                or cache is not getattr(self, "_subscribed_presence_cache", None)):
            return
        scope = current_scope(self)
        if scope and set(scope[-1]).intersection(self._normalize_jid(t) for t in targets):
            self._retry_contact_presence(scope)

    def _refresh_open_contact_presence(self):
        panel = getattr(self, "conversations_panel", None)
        if (getattr(self, "_shutting_down", False)
                or panel is None or panel.conversation is None):
            self._cancel_deferred_contact_presence()
            return
        jid = panel.conversation.get("remoteJid", "")
        panel._refresh_presence_note(jid)
        if (not contact_presence_in_view(self)
                or cached(self, jid, fresh=False).get("restricted")):
            self._cancel_deferred_contact_presence()
            return
        scope = current_scope(self)
        work = presence_work(self, wx)
        current_job = getattr(self, "_contact_presence_read_job", None)
        if current_job is not None and not current_job.active:
            current_job.expire()
            return
        if work.reap():
            return
        if getattr(self, "_contact_presence_inflight", False):
            self._defer_contact_presence(scope)
            return
        now = time.monotonic()
        key = aliases(self, jid)[0]
        attempts = getattr(self, "_contact_presence_attempts", {})
        delay = 10 - (now - attempts.get(key, -30))
        if delay > 0:
            self._defer_contact_presence(scope, delay)
            return
        # Consume external demand before dispatch. Only a separate one-use
        # initial-readiness allowance may continue an explicit pending result.
        self._cancel_deferred_contact_presence()
        self._contact_presence_attempts = attempts
        revision = cached(self, jid, fresh=False) or None
        recovery = getattr(self, "_contact_presence_recovery", None)
        if recovery is None:
            recovery = self._contact_presence_recovery = PendingPresenceRecovery()
        # Ordinary ticks reuse the subscription. A separate budget permits
        # recovery only after repeated explicit pending observations.
        if not is_subscribed(self, jid):
            self.subscribe_presence(jid)
        inflight = {self._normalize_jid(t) for t in
                    getattr(self, "_presence_subscription_inflight", set())}
        if inflight.intersection(scope[-1]) and needs_snapshot(self):
            self._defer_contact_presence(scope)
            return
        job = work.claim("get", scope[-1])
        if job is None:
            self._defer_contact_presence(scope)
            return
        attempts[key] = now
        readiness_followup = take_readiness_followup(self, scope[-1])
        self._contact_presence_inflight = True
        self._contact_presence_read_job = job
        routing = self.wpp_server, self.wpp_port, self.token

        def worker():
            result = PresenceRead("error")
            try:
                if job.active and routing == (self.wpp_server, self.wpp_port, self.token):
                    result = self._query_contact_presence(jid)
            except Exception:
                result = PresenceRead("error")
            finally:
                job.finish()

            def finish():
                job.stop_timer()
                if getattr(self, "_contact_presence_read_job", None) is not job:
                    wake_waiting_presence(self, ensure=(
                        getattr(self, "_contact_presence_read_job", None) is None
                        and current_scope(self) != scope))
                    return
                if not job.active:
                    job.expire()
                    return
                self._contact_presence_read_job = None
                self._contact_presence_inflight = False
                if (result is not None and current_scope(self) == scope
                        and not getattr(self, "_shutting_down", False)
                        and getattr(self, "_wa_connected", False)
                        and routing == (self.wpp_server, self.wpp_port, self.token)
                        and revision is (cached(self, jid, fresh=False) or None)):
                    recovery.observe(scope[-1], result.outcome, time.monotonic(), revision)
                    if result.snapshot is not None:
                        self.on_presence_update(jid, {jid: result.snapshot}, refresh_offline=False)
                    elif (result.outcome == "pending"
                          and not (cached(self, jid, fresh=False) or {}).get("restricted")
                          and contact_presence_in_view(self)):
                        self.subscribe_presence(jid, recovery=True)
                        if readiness_followup:
                            self._defer_contact_presence(scope, 10)
                # Read the current cache, including events received during HTTP.
                self._refresh_open_contact_presence_note()
                if current_scope(self) != scope:
                    self._refresh_open_contact_presence()
                else:
                    pending = getattr(self, "_contact_presence_deferred", None)
                    if pending is not None and pending.pending is not None:
                        self._retry_contact_presence(pending.pending)

            wx.CallAfter(finish)

        try:
            job.arm(lambda: expire_presence_read(self, job, scope, revision, recovery))
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            job.cancel_unstarted()
            self._contact_presence_read_job = None
            self._contact_presence_inflight = False
            raise

    def _refresh_open_contact_presence_note(self):
        if getattr(self, "_shutting_down", False):
            return
        panel = getattr(self, "conversations_panel", None)
        if panel is not None and panel.conversation is not None:
            panel._refresh_presence_note(panel.conversation.get("remoteJid", ""))

    def _invalidate_contact_presence(self):
        self._cancel_deferred_contact_presence()
        work = getattr(self, "_contact_presence_work", None)
        if work is not None:
            work.invalidate()
        self._contact_presence_read_job = None
        self._contact_presence_inflight = False
        self._contact_presence_epoch = getattr(self, "_contact_presence_epoch", 0) + 1
        self._presence_cache.clear()
        self._contact_presence_attempts = {}
        self._subscribed_presence_cache = {}
        self._presence_subscription_inflight = set()
        self._contact_presence_post_jobs = {}
        self._presence_subscription_confirmed = set()
        self._contact_presence_readiness_used = set()
        self._contact_presence_readiness_candidates = set()
        self._contact_presence_recovery = PendingPresenceRecovery()
        for timer in getattr(self, "_presence_timers", {}).values():
            timer.Stop()
        self._presence_timers = {}
        self._composing_chats = {}
        notify_personal_presence(self)
        panel = getattr(self, "conversations_panel", None)
        if panel is not None and hasattr(panel, "refresh_typing_row"):
            panel.refresh_typing_row()
        self._refresh_open_contact_presence_note()
