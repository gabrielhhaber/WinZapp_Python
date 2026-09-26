"""GroupsMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import logging
import threading
import time
import wx
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
)
from core.utils import (
    parse_bool_flag as _parse_bool_flag,
    group_setting_notif_value,
)
from core.api_client import (
    api_get,
    api_post,
)
from main_window.identity_rules import (
    group_send_permission_from_metadata,
    participant_digits,
    set_group_participant_admin,
    unexpired_group_send_verdict,
)


class GroupsMixin:
    """Group metadata: names, send permissions, admin/settings/subject changes and
    group management actions.
    """

    _GROUP_SEND_PERMS_MAX_AGE_SECONDS = 24 * 3600

    def _is_group_send_restricted(self, chat: dict) -> bool:
        """True when `chat` is a WhatsApp group set to "only admins can send
        messages" (Baileys/WPPConnect's groupMetadata.announce) and the
        current user isn't one of those admins.

        Only ever reads group metadata local sync already has — this must
        stay synchronous and side-effect-free since it runs from
        """
        jid = chat.get("remoteJid", "")
        if not jid.endswith("@g.us"):
            return False
        stored = unexpired_group_send_verdict(
            getattr(self, "_group_send_perms", {}).get(jid),
            time.time(), self._GROUP_SEND_PERMS_MAX_AGE_SECONDS)
        if stored is None:
            stored = {}
        verdict = group_send_permission_from_metadata(
            chat,
            participant_digits(getattr(self, "my_jid", "")),
            participant_digits(getattr(self, "my_lid", "")),
            self._phone_digits_equivalent,
            known_am_admin=stored.get("am_admin"),
        )
        if verdict is None:
            verdict = stored
        return bool(verdict.get("announce")) and not bool(verdict.get("am_admin"))

    def _record_group_send_perms(self, jid: str, chat: dict) -> "dict | None":
        if not hasattr(self, "_group_send_perms"):
            self._group_send_perms = {}
        previous = unexpired_group_send_verdict(
            self._group_send_perms.get(jid),
            time.time(), self._GROUP_SEND_PERMS_MAX_AGE_SECONDS)
        verdict = group_send_permission_from_metadata(
            chat,
            participant_digits(getattr(self, "my_jid", "")),
            participant_digits(getattr(self, "my_lid", "")),
            self._phone_digits_equivalent,
            known_am_admin=(previous or {}).get("am_admin"),
        )
        if verdict is None:
            return None
        if (previous
                and previous.get("announce") == verdict["announce"]
                and previous.get("am_admin") == verdict["am_admin"]):
            return previous
        verdict["t"] = int(time.time())
        self._group_send_perms[jid] = verdict
        return verdict

    def _persist_group_send_perms(self):
        db = getattr(self, "db", None)
        if db is None:
            return
        try:
            db.set_metadata_json(
                "group_send_perms", dict(getattr(self, "_group_send_perms", {}))
            )
        except Exception as exc:
            logging.warning(
                "[group_send_perms] failed to persist send permissions: %s", exc)

    def _fill_group_name(self, jid: str) -> str:
        """Fetch group info from API and cache the name.

        Called lazily when a group has no cached name. Returns the group
        name or empty string on failure.
        """
        try:
            url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/group-info/{jid}"
            headers = {"Authorization": f"Bearer {self.token}"}
            resp = api_get(url, headers=headers, timeout=10)
            if resp.ok:
                body = resp.json()
                info = body.get("response", body) if isinstance(body, dict) else {}
                name = info.get("name") or info.get("subject", "")
                if name:
                    if not hasattr(self, "_group_name_cache"):
                        self._group_name_cache = {}
                    self._group_name_cache[jid] = name
                    return name
        except Exception:
            pass
        return ""

    def _resolve_group_name_async(self, jid: str):
        """Look up a newly-seen group's name in the background.

        _fill_group_name() does a blocking HTTP request, so this must not run
        on the wx main thread (on_new_message is called via wx.CallAfter).
        """
        def _worker():
            name = self._fill_group_name(jid)
            if not name:
                return
            chat = self.chats.get(jid)
            if chat is None or self._group_name_from_chat_dict(chat):
                return
            chat["name"] = name
            self._schedule_save(dirty_jid=jid)
            wx.CallAfter(self._schedule_set_chats)
        threading.Thread(target=_worker, daemon=True).start()

    def _apply_group_subject_change(self, remote_jid: str, chat: dict, msg: dict,
                                    live: bool = False) -> None:
        """Rename an already-known group chat when its WhatsApp subject changes.

        The "gp2"/subject-change system message (subtype "subject", new name
        in "body" — see WebSocketClient's gp2 handling) already got rendered
        as an in-chat notification, but nothing updated chat["name"] itself:
        the group kept showing its old name everywhere else (chat list,
        window title, tray tooltip, dialogs) until the next full sync
        happened to re-fetch group-info, which could be minutes/hours away
        or never for a group with no other activity. Applying it immediately
        here — from the same event that already told us the new name — keeps
        the chat list in sync instead of looking like the renamed group
        vanished.

        WPPConnect does not always fill that body in, though (observed live:
        the notification arrives and renders, but carries no new name, so this
        used to return empty-handed and the rename reached nothing). `live`
        turns on a background /group-info lookup for exactly that case — see
        _resolve_subject_change_async(). Only the live funnel passes it: doing
        it from history backfill would fire one HTTP request per past rename
        for names that are already long superseded.
        """
        if not remote_jid.endswith("@g.us"):
            return
        if msg.get("messageType") != "groupNotification":
            return
        notif = (msg.get("message") or {}).get("groupNotification") or {}
        if notif.get("subtype") != "subject":
            return
        new_name = (notif.get("body") or "").strip()
        if not new_name:
            logging.info(
                "[_apply_group_subject_change] Subject change for %s carried no new name "
                "(notification keys: %s) — resolving it from group-info: %s",
                remote_jid, sorted(notif.keys()), live,
            )
            if live:
                self._resolve_subject_change_async(remote_jid, chat, notif)
            return
        if chat.get("name") == new_name:
            return
        self._store_group_subject(remote_jid, chat, new_name)

    # Notification subtypes that change who is actually in the group — as
    # opposed to e.g. "promote"/"demote" (admin status only) or "subject"
    # (name only), neither of which add or remove anyone from the mention list.
    _GROUP_MEMBERSHIP_NOTIF_SUBTYPES = frozenset({"add", "remove", "invite", "leave"})

    def _refresh_mention_cache_on_membership_change(self, remote_jid: str, msg: dict) -> None:
        """Re-fetch @mention participants when a live add/remove notification
        arrives for the group currently open.

        _fetch_group_participants() (conversations.py) only ever runs when a
        group conversation is *opened* — nothing re-ran it while one stayed
        open, so a member who joined mid-conversation was invisible to @
        suggestions until the user closed and reopened the chat (or WinZapp
        happened to re-sync). The groupNotification that announces the join
        arrives over the same live socket messages already flow through, so
        reacting to it here catches the cache up within one HTTP round trip
        instead of leaving it stale for the rest of the session.
        """
        if not remote_jid.endswith("@g.us"):
            return
        if msg.get("messageType") != "groupNotification":
            return
        notif = (msg.get("message") or {}).get("groupNotification") or {}
        if notif.get("subtype") not in self._GROUP_MEMBERSHIP_NOTIF_SUBTYPES:
            return
        cp = getattr(self, "conversations_panel", None)
        if cp is None or cp.conversation is None:
            return
        if self._normalize_jid(cp.conversation.get("remoteJid", "")) != self._normalize_jid(remote_jid):
            return
        threading.Thread(
            target=cp._fetch_group_participants,
            args=(remote_jid,),
            daemon=True,
        ).start()

    _GROUP_ANNOUNCE_NOTIF_SUBTYPES = frozenset({"announce", "announcement", "restrict_messages"})
    _GROUP_RESTRICT_NOTIF_SUBTYPES = frozenset({"restrict", "locked", "settings"})
    _GROUP_ADMIN_NOTIF_SUBTYPES = frozenset({"promote", "promotion", "demote", "demotion"})

    def _apply_group_settings_change(self, remote_jid: str, chat: dict, msg: dict) -> None:
        if not remote_jid.endswith("@g.us"):
            return
        if msg.get("messageType") != "groupNotification":
            return
        notif = (msg.get("message") or {}).get("groupNotification") or {}
        subtype = (notif.get("subtype") or "").lower()
        if subtype in self._GROUP_ADMIN_NOTIF_SUBTYPES:
            self._apply_group_admin_change(remote_jid, chat, notif, subtype)
            return
        is_announce = subtype in self._GROUP_ANNOUNCE_NOTIF_SUBTYPES
        if not is_announce and subtype not in self._GROUP_RESTRICT_NOTIF_SUBTYPES:
            return
        value = group_setting_notif_value(notif)
        if value is None:
            logging.info(
                "[_apply_group_settings_change] %s notification stated no value "
                "(keys: %s) — local state left untouched", subtype, sorted(notif.keys()))
            return
        group_meta = chat.get("groupMetadata")
        if not isinstance(group_meta, dict):
            group_meta = {}
            chat["groupMetadata"] = group_meta
        group_meta["announce" if is_announce else "restrict"] = value
        if not is_announce:
            return
        if self._record_group_send_perms(remote_jid, chat) is not None:
            self._persist_group_send_perms()
        cp = getattr(self, "conversations_panel", None)
        if cp is not None:
            wx.CallAfter(cp.refresh_composer_permissions, remote_jid)

    def _group_admin_notif_targets_me(self, notif: dict) -> "bool | None":
        targets = []
        for raw in (notif.get("recipients") or []):
            if isinstance(raw, dict):
                raw = raw.get("_serialized") or raw.get("id") or ""
            if isinstance(raw, str) and raw:
                targets.append(raw)
        if not targets:
            return None
        if not getattr(self, "my_jid", ""):
            return None
        undecidable = False
        for target in targets:
            if self._is_self_jid(target):
                return True
            if (target.endswith("@lid")
                    and target not in getattr(self, "_lid_to_phone", {})
                    and not getattr(self, "my_lid", "")):
                undecidable = True
        return None if undecidable else False

    def _apply_group_admin_change(self, remote_jid: str, chat: dict, notif: dict,
                                  subtype: str) -> None:
        targets_me = self._group_admin_notif_targets_me(notif)
        if targets_me is False:
            return
        if not hasattr(self, "_group_send_perms"):
            self._group_send_perms = {}
        cp = getattr(self, "conversations_panel", None)
        if targets_me is None:
            logging.info(
                "[_apply_group_admin_change] %s notification named no target "
                "this account can be matched against — dropping the stored "
                "verdict so the composer fails open", subtype)
            if self._group_send_perms.pop(remote_jid, None) is not None:
                self._persist_group_send_perms()
                if cp is not None:
                    wx.CallAfter(cp.refresh_composer_permissions, remote_jid)
            return
        is_admin = subtype in ("promote", "promotion")
        group_meta = chat.get("groupMetadata")
        if not isinstance(group_meta, dict):
            group_meta = {}
        set_group_participant_admin(
            group_meta.get("participants") or chat.get("participants") or [],
            is_admin,
            participant_digits(getattr(self, "my_jid", "")),
            participant_digits(getattr(self, "my_lid", "")),
            self._phone_digits_equivalent,
        )
        announce = _parse_bool_flag(group_meta.get("announce"))
        if announce is None:
            announce = _parse_bool_flag(chat.get("announce"))
        if announce is None:
            stored = unexpired_group_send_verdict(
                self._group_send_perms.get(remote_jid),
                time.time(), self._GROUP_SEND_PERMS_MAX_AGE_SECONDS)
            announce = None if stored is None else bool(stored.get("announce"))
        if announce is None:
            changed = self._group_send_perms.pop(remote_jid, None) is not None
        else:
            self._group_send_perms[remote_jid] = {
                "announce": bool(announce),
                "am_admin": is_admin,
                "t": int(time.time()),
            }
            changed = True
        if changed:
            self._persist_group_send_perms()
        if cp is not None:
            wx.CallAfter(cp.refresh_composer_permissions, remote_jid)

    def _resolve_subject_change_async(self, jid: str, chat: dict, notif: dict) -> None:
        """Fetch the group's current subject after a rename notification that
        did not carry it, and fill it in everywhere.

        /group-info is a live lookup and does know the new name — that is why
        the group-data dialog shows it correctly while the chat list does not:
        the list is built from list-chats, which serialises WhatsApp Web's own
        chat store, and that store can still be holding the old subject.

        The resolved name is written back into the notification's own body so
        the timeline row upgrades itself from "X changed the group name" to
        "X changed the group name to Y" (group_notif_subject_changed_to) —
        the renderer already prefers that wording whenever a body is present.

        _fill_group_name() blocks on HTTP, and the callers of this run on the
        wx main thread, hence the thread — same reason and shape as
        _resolve_group_name_async().
        """
        def _worker():
            name = self._fill_group_name(jid)
            if not name:
                logging.info(
                    "[_resolve_subject_change_async] group-info returned no name for %s", jid)
                return
            # Same dict the message record holds, so the stored notification
            # gains the name too and survives a conversation reopen.
            notif["body"] = name
            renamed = chat.get("name") != name
            if renamed:
                self._store_group_subject(jid, chat, name)
            self._schedule_save(dirty_jid=jid)
            wx.CallAfter(self._schedule_set_chats)
            # Re-render the open conversation so the notification row picks up
            # the name it was missing when it was first drawn.
            panel = getattr(self, "conversations_panel", None)
            if panel is not None and (panel.conversation or {}).get("remoteJid") == jid:
                wx.CallAfter(panel.populate_messages, True)
        threading.Thread(target=_worker, daemon=True).start()

    def _store_group_subject(self, jid: str, chat: dict, new_name: str) -> None:
        """Apply a resolved group name to the chat, the name cache and the
        conversation currently on screen."""
        chat["name"] = new_name
        self._group_name_cache = getattr(self, "_group_name_cache", {})
        self._group_name_cache[jid] = new_name
        if hasattr(self, "conversations_panel"):
            wx.CallAfter(self.conversations_panel.update_conversation_name, jid, new_name)

    def _reconcile_group_info_name(self, jid: str, info: dict) -> None:
        """Feed an authoritative /group-info name back into the chat list."""
        if not isinstance(info, dict):
            return
        new_name = (info.get("subject") or info.get("name") or "").strip()
        chat = self.chats.get(jid)
        if not new_name or chat is None or chat.get("name") == new_name:
            return
        self._store_group_subject(jid, chat, new_name)
        self._schedule_save(dirty_jid=jid)
        wx.CallAfter(self._schedule_set_chats)

    def on_group_subject_updated(self, remote_jid: str, new_subject: str) -> None:
        """Handle a live group subject update emitted from a gp2 notification."""
        # See _live_events_ready() — this is the third live funnel, gated for
        # the same reasons as on_new_message()/on_historical_message().
        #
        # It went ungated for as long as nothing emitted 'groups.update' at
        # all, which this method used to document about itself.  The gap
        # stopped being theoretical once createSessionUtil.ts started emitting
        # the event for gp2/subject notifications, which is why the gate lands
        # together with that.
        #
        # A rename dropped here is recovered by the same gp2 notification
        # arriving again through history backfill (on_historical_message →
        # _apply_group_subject_change), not by the sync: nothing re-fetches
        # /group-info for a group that already has a name, and list-chats
        # serialises WhatsApp Web's own store, which can still be holding the
        # old subject (see _apply_group_subject_change's docstring).
        if not self._live_events_ready():
            return
        if remote_jid not in self.chats:
            return

        new_name = new_subject.strip()
        if not new_name:
            return

        chat = self.chats[remote_jid]
        if chat.get("name") == new_name:
            return

        # Not new_name itself: a group subject is free text (often a family
        # or company name) with no pattern the logging formatter's phone/JID
        # masking can catch.
        logging.info(f"[on_group_subject_updated] Updating group {remote_jid} name ({len(new_name)} chars)")
        self._store_group_subject(remote_jid, chat, new_name)

        self._schedule_save(dirty_jid=remote_jid)
        self._schedule_set_chats()

    def _resolve_missing_group_names(self):
        """Retry group-info lookups for groups still unnamed after sync.

        Runs on the background sync thread. Uses a few parallel workers so a
        large number of unresolved groups doesn't add much wall-clock time to
        the sync.
        """
        unresolved = [
            jid for jid, chat in list(self.chats.items())
            if jid.endswith("@g.us") and not self._group_name_from_chat_dict(chat)
        ]
        if not unresolved:
            return
        max_workers = min(6, len(unresolved))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = {pool.submit(self._fill_group_name, jid): jid for jid in unresolved}
            for fut in as_completed(futs):
                jid = futs[fut]
                try:
                    name = fut.result()
                except Exception:
                    name = ""
                if name:
                    self.chats[jid]["name"] = name
                    self._schedule_save(dirty_jid=jid)


    def get_group_info(self, jid: str) -> dict:
        """Fetch group metadata via GET /api/{session}/group-info/{groupId}"""
        url = (
            f"{self.wpp_server}:{self.wpp_port}"
            f"/api/{self.token}/group-info/{jid}"
        )
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        try:
            r = api_get(url, headers=headers, timeout=10)
            logging.info(f"[get_group_info] status={r.status_code} for {jid}")
            if r.status_code in (200, 201):
                res_data = r.json() or {}
                response = res_data.get("response") or {}
                logging.info(f"[get_group_info] response type={type(response).__name__} keys={list(response.keys()) if isinstance(response, dict) else response}")
                if isinstance(response, dict):
                    self._reconcile_group_info_name(jid, response)
                    return response
                return {}
        except Exception as e:
            logging.error(f"[get_group_info] error: {e}")
        return {}

    # ── Group ─────────────────────────────────────────────────────────────────

    def leave_group(self, jid: str):
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/leave-group"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        try:
            api_post(url, json={"groupId": jid}, headers=headers, timeout=10)
        except Exception:
            pass
        # Archive instead of delete so the message history is preserved locally.
        self.archive_chat(jid)

    def create_group(self, name: str, participants: list) -> tuple:
        """
        Create a WhatsApp group with the given name and participant numbers.
        participants: list of phone number or JID strings (e.g. ["5511999999999@s.whatsapp.net", "63977983840477@lid"])
        Returns (True, group_jid) on success, (False, error_message) on failure.
        """
        # Normalize participant JIDs for WPPConnect
        normalized_participants = []
        for p in participants:
            if "@" in p:
                p_norm = p.replace("@s.whatsapp.net", "@c.us")
                normalized_participants.append(p_norm)
            else:
                # Default to c.us for raw typed digits (phone numbers)
                normalized_participants.append(f"{p}@c.us")

        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/create-group"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        payload = {
            "name":         name,
            "participants": normalized_participants,
        }
        try:
            r = api_post(url, json=payload, headers=headers, timeout=30)
            if r.status_code in (200, 201):
                resp = r.json().get("response", {})
                gid = resp.get("gid", {})
                if isinstance(gid, dict):
                    gid = gid.get("_serialized", "")
                return True, gid or ""
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as exc:
            return False, str(exc)

    def add_group_members(self, group_jid: str, participant_jids: list) -> tuple:
        """
        Add one or more participants to a group.
        Returns (True, "") on success, (False, error_message) on failure.

        HTTP 201 alone is not proof anyone was actually added: WPPConnect's
        addParticipant controller answers "success" unconditionally, and the
        one signal that reflects what WhatsApp itself did is buried in
        response.result — a dict per participant carrying a wa-js `code`
        (200/409 = really in the group now, 403 = the target's privacy
        settings blocked a direct add and WhatsApp sent an invite instead,
        anything else = not added and no invite either). Reported live: a
        403 case surfaced as an ordinary success dialog, and the "member" was
        never in the group's participant list at all.
        """
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/add-participant-group"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        # groupController.ts's addParticipant() (upstream, unpatched) reads
        # req.body.phone — same field name remove/promote/demote already use
        # below via _group_participant_action(). This used to send
        # "participantId" instead, a field the controller never reads at
        # all, so phone came through as undefined and every add attempt
        # failed with a 500 inside contactToArray(undefined).
        payload = {
            "groupId": group_jid,
            "phone":   [j if "@" in j else f"{j}@c.us" for j in participant_jids],
        }
        try:
            r = api_post(url, json=payload, headers=headers, timeout=15)
            if r.status_code not in (200, 201):
                return False, f"HTTP {r.status_code}: {r.text[:200]}"
            try:
                result_groups = r.json().get("response", {}).get("result", [])
            except Exception:
                result_groups = []
            invited, failed = [], []
            for group_result in result_groups:
                if not isinstance(group_result, dict):
                    continue
                for jid, info in group_result.items():
                    if not isinstance(info, dict):
                        continue
                    try:
                        code = int(info.get("code"))
                    except (TypeError, ValueError):
                        code = None
                    if code in (200, 409):
                        continue  # genuinely in the group now (or already was)
                    contact = getattr(self, "contacts", {}).get(jid, {})
                    display = (
                        contact.get("name")
                        or contact.get("pushName")
                        or jid.split("@")[0]
                    )
                    if code == 403 and info.get("invite_code"):
                        invited.append(display)
                    else:
                        failed.append(display)
            if not invited and not failed:
                return True, ""
            parts = []
            if invited:
                parts.append(self.i18n.t("add_member_privacy_invite_sent").format(
                    names=", ".join(invited)))
            if failed:
                parts.append(self.i18n.t("add_member_could_not_add").format(
                    names=", ".join(failed)))
            return False, " ".join(parts)
        except Exception as exc:
            return False, str(exc)

    def _group_participant_action(self, endpoint: str, group_jid: str, participant_jids: list) -> tuple:
        """Shared POST for remove/promote/demote-participant-group — all
        three take the same {groupId, phone} shape."""
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        payload = {
            "groupId": group_jid,
            "phone": [j if "@" in j else f"{j}@c.us" for j in participant_jids],
        }
        try:
            r = api_post(url, json=payload, headers=headers, timeout=15)
            if r.status_code in (200, 201):
                return True, ""
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as exc:
            return False, str(exc)

    def remove_group_members(self, group_jid: str, participant_jids: list) -> tuple:
        """Remove one or more participants from a group.
        Returns (True, "") on success, (False, error_message) on failure."""
        return self._group_participant_action("remove-participant-group", group_jid, participant_jids)

    def promote_group_members(self, group_jid: str, participant_jids: list) -> tuple:
        """Promote one or more participants to group admin."""
        return self._group_participant_action("promote-participant-group", group_jid, participant_jids)

    def demote_group_members(self, group_jid: str, participant_jids: list) -> tuple:
        """Demote one or more participants from group admin."""
        return self._group_participant_action("demote-participant-group", group_jid, participant_jids)

    def set_group_subject(self, group_jid: str, title: str) -> tuple:
        """Change a group's name/subject. Returns (True, "") or (False, error)."""
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/group-subject"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        payload = {"groupId": group_jid, "title": title}
        try:
            r = api_post(url, json=payload, headers=headers, timeout=15)
            if r.status_code in (200, 201):
                return True, ""
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as exc:
            return False, str(exc)

    def set_group_description(self, group_jid: str, description: str) -> tuple:
        """Change a group's description. Returns (True, "") or (False, error)."""
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/group-description"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        payload = {"groupId": group_jid, "description": description}
        try:
            r = api_post(url, json=payload, headers=headers, timeout=15)
            if r.status_code in (200, 201):
                return True, ""
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as exc:
            return False, str(exc)
