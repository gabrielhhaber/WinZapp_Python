"""StatusLoadingMixin — part of StatusPanel (see status_tab/__init__.py).

Moved verbatim out of status_panel.py. Methods run with ``self`` bound to
the StatusPanel instance, so every attribute set in StatusPanel.__init__/
init_UI is available here.
"""

import logging
import wx
from core.api_client import api_get
from core.transcription import statuses as transcription_statuses
from core.utils import format_number


class StatusLoadingMixin:
    """Loading statuses: the API fetch, parsing, merging with the local cache and
    my-status reconciliation.
    """

    def _load_statuses(self):
        """
        Build the status list.

        Primary source: the account's StatusV3Store via
        GET /api/{session}/statuses — the user's own posted statuses come
        straight from the account (not from the local DB/in-memory cache),
        and contacts' statuses are pulled from the browser store. Falls back
        to the live status@broadcast messages collected in
        MainWindow._status_updates when the API is unreachable or returns
        nothing (e.g. the Status view was never opened in the browser yet).
        """
        mw   = self.main_window
        i18n = mw.i18n
        wx.CallAfter(self._set_list_loading)
        my_statuses, contacts = self._fetch_statuses_from_api()
        api_ok = getattr(self, "_last_status_api_ok", False)
        my_status_ready = getattr(self, "_last_my_status_ready", False)
        # A non-empty legacy response is safe to reconcile. An empty list is
        # authoritative only when the API confirms getMyStatus() was ready.
        if api_ok:
            self._reconcile_my_status_cache(
                my_statuses, authoritative_empty=my_status_ready
            )
        # Merge, never replace: the API's StatusV3Store may only hold the
        # pages loaded so far, while _status_updates (seeded from the DB at
        # startup) keeps the stories that arrived via status@broadcast
        # earlier. Showing both (deduped by message id) covers the whole
        # picture instead of dropping whichever source has less.
        status_updates = getattr(mw, "_status_updates", {})
        records = []
        for participant, msgs in list(status_updates.items()):
            for msg in msgs:
                records.append(msg)
        if records:
            fb_my, fb_contacts = self._parse_statuses(records, i18n)
            my_statuses = self._merge_status_lists(my_statuses, fb_my)
            contacts = self._merge_status_contacts(contacts, fb_contacts)
            # The API's copy of a status comes first in the merge and knows
            # nothing of a transcription saved with the cached one.
            shown = list(my_statuses)
            for entry in contacts:
                shown.extend(entry.get("statuses") or ())
            transcription_statuses.carry_over(shown, records)
        wx.CallAfter(self._populate_list, my_statuses, contacts)

    def _reconcile_my_status_cache(
        self, remote_my_statuses: list, *, authoritative_empty: bool = False
    ) -> None:
        """Delete cached own stories absent from authoritative WhatsApp.

        Empty lists are destructive only after the API confirms that
        WPP.status.getMyStatus() actually returned a loaded status model.
        Older/custom APIs without that readiness marker keep the safe legacy
        behavior: a non-empty list can reconcile, an empty one cannot.
        """
        if not remote_my_statuses and not authoritative_empty:
            return
        mw = self.main_window
        remote_ids = {
            (status.get("key") or {}).get("id")
            for status in remote_my_statuses
            if isinstance(status, dict)
        }
        local_records = [
            status
            for bucket in getattr(mw, "_status_updates", {}).values()
            for status in bucket
        ]
        local_my, _ = self._parse_statuses(local_records, mw.i18n)
        stale_ids = {
            (status.get("key") or {}).get("id")
            for status in local_my
            if isinstance(status, dict)
        } - remote_ids
        for message_id in stale_ids:
            if message_id:
                mw.remove_failed_status_update(message_id, refresh=False)
        if stale_ids:
            logging.info(
                "[status_panel] Removed %d local own status(es) absent from WhatsApp",
                len(stale_ids),
            )

    @staticmethod
    def _merge_status_lists(a: list, b: list) -> list:
        """Union of two status-dict lists, deduped by key.id (order: a first)."""
        seen = set()
        out = []
        for s in list(a) + list(b):
            if not isinstance(s, dict):
                continue
            mid = (s.get("key") or {}).get("id")
            if mid and mid in seen:
                continue
            if mid:
                seen.add(mid)
            out.append(s)
        return out

    @staticmethod
    def _merge_status_contacts(a: list, b: list) -> list:
        """Union of two contact-status lists, grouped by jid, deduped by id."""
        by_jid = {}
        for entry in list(a) + list(b):
            if not isinstance(entry, dict):
                continue
            jid = entry.get("jid")
            if not jid:
                continue
            merged = by_jid.get(jid)
            if merged is None:
                merged = {
                    "name": entry.get("name", ""),
                    "jid": jid,
                    "statuses": [],
                    "viewed_all": entry.get("viewed_all", False),
                }
                by_jid[jid] = merged
            seen = {(s.get("key") or {}).get("id") for s in merged["statuses"]}
            for s in entry.get("statuses") or []:
                if not isinstance(s, dict):
                    continue
                mid = (s.get("key") or {}).get("id")
                if mid and mid in seen:
                    continue
                if mid:
                    seen.add(mid)
                merged["statuses"].append(s)
        return list(by_jid.values())

    def _fetch_statuses_from_api(self) -> tuple:
        """Query WPPConnect's GET /api/{session}/statuses (StatusV3Store).

        Returns ``(my_statuses, contacts)`` in the same shape as
        _parse_statuses() — raw store messages are normalized through
        WebSocketClient._normalize_wpp_message() (the same converter the
        message sync uses), so both the API and the WebSocket paths feed the
        panel identical dicts.
        """
        mw   = self.main_window
        i18n = mw.i18n
        try:
            url = f"{mw.wpp_server}:{mw.wpp_port}/api/{mw.token}/statuses"
            headers = {"Authorization": f"Bearer {mw.token}", "Content-Type": "application/json"}
            resp = api_get(url, headers=headers, timeout=15)
            if resp.status_code not in (200, 201):
                self._last_status_api_ok = False
                self._last_my_status_ready = False
                return [], []
            body = resp.json() or {}
            data = body.get("response") if isinstance(body, dict) else None
        except Exception as exc:
            logging.warning("[status_panel] statuses API failed, falling back to WebSocket cache: %s", exc)
            self._last_status_api_ok = False
            self._last_my_status_ready = False
            return [], []
        if not isinstance(data, dict):
            self._last_status_api_ok = False
            self._last_my_status_ready = False
            return [], []

        ws  = getattr(mw, "ws", None)
        raw = []
        for msgs in (data.get("myStatus") or []):
            raw.append(msgs)
        for entry in (data.get("contacts") or []):
            for msgs in (entry.get("msgs") or []):
                raw.append(msgs)

        records = []
        for wm in raw:
            if not isinstance(wm, dict):
                continue
            if ws is not None:
                try:
                    records.append(ws._normalize_wpp_message(wm))
                    continue
                except Exception as exc:
                    logging.warning("[status_panel] failed to normalize API status: %s", exc)
            records.append(wm)
        self._last_status_api_ok = True
        # New APIs distinguish "loaded and genuinely empty" from "not ready".
        # Non-empty responses remain authoritative for backward compatibility.
        self._last_my_status_ready = bool(data.get("myStatusReady")) or bool(
            data.get("myStatus")
        )
        return self._parse_statuses(records, i18n)

    def _parse_statuses(self, items, i18n) -> tuple:
        """
        Separate own statuses from other people's.

        Returns
        -------
        (my_statuses, contacts)
            my_statuses : list of status dicts posted by this account
                          (key.fromMe, or participant resolving to self —
                          see _is_self_jid() below)
            contacts    : list of {"name", "jid", "statuses"} for other people
        """
        my_statuses = []
        contacts    = []

        if not isinstance(items, list):
            return my_statuses, contacts

        # Group by participant JID (use participant over remoteJid for
        # status@broadcast entries, which is how WhatsApp encodes them).
        grouped: dict = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            # StatusV3 can retain administrative tombstones after a story is
            # revoked, deleted or expired. They are not displayable stories.
            msg_type = str(item.get("messageType") or "")
            raw_type = str(item.get("type") or "").lower()
            if msg_type in ("protocolMessage", "reactionMessage") or raw_type in (
                "revoked", "protocol", "protocolmessage",
                "reaction", "reactionmessage",
            ):
                continue
            if any(bool(item.get(flag)) for flag in (
                "isRevoked", "revoked", "isDeleted", "deleted",
                "isExpired", "expired", "isStatusExpired",
            )):
                continue

            key = item.get("key", {})
            participant = (key.get("participant") or item.get("participant") or "")
            # A status posted from a different linked device, or synced
            # back with a phone-number variant that differs from the one
            # WinZapp itself is paired under (extra/missing Brazilian 9th
            # digit, etc.), can arrive with fromMe=False even though the
            # participant is genuinely this account — checked via
            # _is_self_jid() (the same phone-digit-tolerant JID comparison
            # used everywhere else) rather than trusting fromMe alone.
            is_mine = key.get("fromMe", False) or (
                participant and self.main_window._is_self_jid(participant)
            )
            if is_mine:
                my_statuses.append(item)
                continue
            remote_jid  = key.get("remoteJid", "")
            # status@broadcast is the channel; real sender is in participant
            if remote_jid == "status@broadcast" and participant:
                jid = participant
            else:
                jid = remote_jid or participant
            if not jid or jid == "status@broadcast":
                continue
            name = self._resolve_name(jid) or format_number(jid)
            if jid not in grouped:
                grouped[jid] = {"name": name, "jid": jid, "statuses": []}
            grouped[jid]["statuses"].append(item)

        # A contact only counts as fully "viewed" once every one of their
        # current statuses has been opened at least once — matches the
        # official client's own "seen"/"unseen" ring distinction. See
        # _mark_status_viewed()/_populate_list() for where this list is
        # written and read.
        viewed_ids = set(
            self.main_window.settings.get("status_panel", {}).get("viewed_status_ids", [])
        )
        for entry in grouped.values():
            statuses = entry.get("statuses", [])
            entry["viewed_all"] = bool(statuses) and all(
                s.get("key", {}).get("id") in viewed_ids for s in statuses
            )
            contacts.append(entry)

        return my_statuses, contacts

    def _resolve_name(self, jid: str) -> str:
        # Delegate to the same resolver chats use (address-book "name"
        # preferred over the WhatsApp profile "pushName", @lid/@c.us
        # variants tried, bad-name filtering) — this used to read
        # contact.get("pushName") directly, which always showed the
        # person's own WhatsApp display name here even when a different
        # name was saved for them in the address book, unlike every chat
        # list/conversation in the app.
        mw = self.main_window
        return mw._resolve_contact_name({"remoteJid": jid}) or ""
