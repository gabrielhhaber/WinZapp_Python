"""ChatActionsMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import logging
import threading
import time
import wx
from core.utils import (
    parse_bool_flag as _parse_bool_flag,
    clear_chat_applied,
    clear_chat_keep_starred_echo,
    effective_unread_count,
    mute_response_accepted,
)
from core.api_client import (
    api_get,
    api_post,
)


class ChatActionsMixin:
    """Chat actions: block, mute, archive, delete, clear, typing/recording status
    and pin.
    """

    # ── Block ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _bare_phone_digits(jid: str) -> str:
        """Strip the @suffix and any Baileys device suffix (':N'), leaving
        bare phone digits — the form WPPConnect's /blocklist endpoint returns
        (see get_block_list())."""
        if not jid:
            return ""
        local = jid.split("@", 1)[0]
        return local.split(":", 1)[0]

    def is_contact_blocked(self, jid: str) -> bool:
        # getattr, not a bare attribute access: add_chats_to_ui() can render
        # a cached/offline chat list before prepare_sync() has reached the
        # point where it restores _blocked_contacts from the database (e.g.
        # a re-pairing flow after a stale token), which crashed here with
        # AttributeError and left the chat list stuck empty.
        blocked_contacts = getattr(self, "_blocked_contacts", None) or set()
        digits = self._bare_phone_digits(jid)
        if not digits:
            return False
        if digits in blocked_contacts:
            return True
        # Brazilian mobile 8/9-digit interchangeable form — a contact can be
        # blocked under either digit count depending on how WhatsApp/the
        # phone reported it, same tolerance _get_contact_tolerant() applies.
        if digits.startswith("55"):
            if len(digits) == 13 and digits[4] == "9":
                if (digits[:4] + digits[5:]) in blocked_contacts:
                    return True
            elif len(digits) == 12:
                if (digits[:4] + "9" + digits[4:]) in blocked_contacts:
                    return True
        return False

    def get_block_list(self):
        """Fetch the account's blocked-contacts list from WPPConnect and sync
        it into _blocked_contacts. Block state is account-wide, not a
        per-chat field WPPConnect's list-chats response carries (unlike
        mute/pin/archive), so it needs its own endpoint — called from the
        full sync and the periodic chat/contact poll."""
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/blocklist"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        try:
            resp = api_get(url, headers=headers, timeout=10)
            if resp.status_code not in (200, 201):
                logging.warning("[get_block_list] HTTP %s", resp.status_code)
                return
            data = resp.json()
            entries = data.get("response", []) if isinstance(data, dict) else []
            digits_set = set()
            for entry in entries:
                if isinstance(entry, dict):
                    phone = entry.get("phone", "")
                elif isinstance(entry, str):
                    phone = entry.split("@")[0]
                else:
                    phone = ""
                if phone:
                    digits_set.add(phone)
            if digits_set != self._blocked_contacts:
                self._blocked_contacts = digits_set
                if hasattr(self, "db") and self.db is not None:
                    self.db.set_metadata_json("blocked_contacts", list(self._blocked_contacts))
                wx.CallAfter(self._schedule_set_chats)
        except Exception as e:
            logging.warning("[get_block_list] failed: %s", e)

    def _apply_block_state(self, jid: str, blocked: bool):
        """Local-only half of block/unblock: mutate _blocked_contacts,
        persist to DB metadata, and refresh the chat list. Split out so
        block_contact() can call this again to roll back the optimistic
        change if WhatsApp rejects it. Safe to call off the main thread —
        _schedule_set_chats() is documented safe from any thread."""
        digits = self._bare_phone_digits(jid)
        if not digits:
            return
        if blocked:
            self._blocked_contacts.add(digits)
        else:
            self._blocked_contacts.discard(digits)
        if hasattr(self, "db") and self.db is not None:
            self.db.set_metadata_json("blocked_contacts", list(self._blocked_contacts))
        self._schedule_set_chats()

    def block_contact(self, jid: str, action: str = "block"):
        """action: 'block' or 'unblock'. Runs on a background thread (see
        callers in conversations.py)."""
        blocked = action == "block"
        self._apply_block_state(jid, blocked)
        endpoint = "block-contact" if blocked else "unblock-contact"
        url = (
            f"{self.wpp_server}:{self.wpp_port}"
            f"/api/{self.token}/{endpoint}"
        )
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        try:
            resp = api_post(
                url, json={"phone": jid},
                headers=headers, timeout=10,
            )
            if not resp.ok:
                logging.warning(
                    "[block_contact] API error %s for %s (%s): %s",
                    resp.status_code, jid, action, resp.text[:200],
                )
                # This call used to be fire-and-forget with no response
                # check at all — the menu item never reflected the real
                # block state (always showed "Bloquear", never toggled to
                # "Desbloquear") and a rejected request left WinZapp
                # believing a contact was blocked when WhatsApp never
                # actually blocked it. Roll back immediately instead.
                wx.CallAfter(self._on_block_sync_rejected, jid, blocked)
        except Exception as exc:
            logging.warning("[block_contact] request failed for %s: %s", jid, exc)
            wx.CallAfter(self._on_block_sync_rejected, jid, blocked)

    def _on_block_sync_rejected(self, jid: str, attempted_blocked: bool):
        """Revert an optimistic block/unblock that WhatsApp did not actually
        accept, and tell the user (runs on the wx main thread)."""
        self._apply_block_state(jid, not attempted_blocked)
        if not self.background_mode:
            self.error_sound.play()
            key = "block_contact_failed" if attempted_blocked else "unblock_contact_failed"
            wx.MessageBox(
                self.i18n.t(key),
                self.i18n.t("error").format(app_name=self.app_name),
                wx.OK | wx.ICON_WARNING,
                self,
            )

    # ── Mute ──────────────────────────────────────────────────────────────────

    def _mute_state_jids(self, jid: str) -> set[str]:
        """Return the canonical chat JID and any known phone/LID alias."""
        normalized = self._normalize_jid(jid)
        jids = {normalized}
        if normalized.endswith("@lid"):
            alternate = getattr(self, "_lid_to_phone", {}).get(normalized, "")
        else:
            alternate = getattr(self, "_phone_to_lid", {}).get(normalized, "")
        if alternate:
            jids.add(self._normalize_jid(alternate))
        return jids

    def is_chat_muted(self, jid: str) -> bool:
        for mute_jid in self._mute_state_jids(jid):
            expiry = self._muted_chats.get(mute_jid)
            if expiry == -1 or (expiry is not None and time.time() < expiry):
                return True
        return False

    def _apply_mute_state(self, jid: str, expiry):
        """Local-only half of mute/unmute: mutate _muted_chats, persist to
        DB metadata, and refresh the chat list. Split out from
        mute_chat()/unmute_chat() so _sync_mute_to_server() can call this
        again to roll back the optimistic change if WhatsApp rejects it."""
        for mute_jid in self._mute_state_jids(jid):
            if expiry is None:
                self._muted_chats.pop(mute_jid, None)
            else:
                self._muted_chats[mute_jid] = expiry
        if hasattr(self, "db") and self.db is not None:
            self.db.set_metadata_json("muted_chats", self._muted_chats)
        self._schedule_set_chats()

    def mute_chat(self, jid: str, duration_secs: int):
        """duration_secs=-1 means mute permanently."""
        expiry = -1 if duration_secs == -1 else int(time.time()) + duration_secs
        self._apply_mute_state(jid, expiry)
        self._sync_mute_to_server(jid, duration_secs, rollback_expiry=None)

    def unmute_chat(self, jid: str):
        previous_expiry = self._muted_chats.get(jid)
        self._apply_mute_state(jid, None)
        self._sync_mute_to_server(jid, 0, rollback_expiry=previous_expiry)

    def _sync_mute_to_server(self, jid: str, duration_secs: int, rollback_expiry=None):
        """Send mute/unmute to WPPConnect in a background thread. duration_secs=0 = unmute.
        rollback_expiry is the _muted_chats value to restore if the request
        is rejected (None means "was not muted before this call")."""
        def _do():
            try:
                if duration_secs == 0:
                    wpp_time, wpp_type = 0, "hours"
                elif duration_secs == -1:
                    wpp_time, wpp_type = 8766, "hours"  # ~1 year (closest to permanent)
                elif duration_secs < 3600:
                    # WPPConnect's sendMute also accepts "minutes" granularity
                    # (see WAPI.sendMute's timeType switch: hours/minutes/year)
                    # — using it for sub-hour durations instead of always
                    # rounding up to a full hour matters if this is ever
                    # called with a shorter duration than the UI currently
                    # offers (today's mute presets are all >= 1h, so this
                    # branch is dormant but correct rather than silently
                    # wrong).
                    wpp_time = max(1, duration_secs // 60)
                    wpp_type = "minutes"
                else:
                    wpp_time = duration_secs // 3600
                    wpp_type = "hours"
                url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/send-mute"
                headers = {"Authorization": f"Bearer {self.token}"}

                def _post(dest: str):
                    payload = {
                        "phone": dest,
                        "time": wpp_time,
                        "type": wpp_type,
                        "isGroup": dest.endswith("@g.us"),
                    }
                    return api_post(url, json=payload, headers=headers, timeout=10)

                def _accepted(resp) -> bool:
                    accepted = mute_response_accepted(
                        bool(resp.ok), resp.text, duration_secs == 0
                    )
                    if not accepted or not resp.ok:
                        return accepted
                    try:
                        result = resp.json().get("response", {})
                    except (AttributeError, TypeError, ValueError):
                        return accepted
                    if isinstance(result, dict) and "isMuted" in result:
                        return bool(result["isMuted"]) is (duration_secs != 0)
                    return accepted

                # Prefer the @lid form when one is known — same preference
                # delete_message_for_everyone()/forward_message() already
                # apply. WPPConnect's legacy WAPI.sendMute resolves the
                # target by looking it up in WhatsApp Web's own in-memory
                # chat store (confirmed live: a failure here returns
                # {"erro":true,"to":"<jid>","status":404}, WAPI's own
                # "not found in store" shape) — that lookup can fail under
                # one JID form while the store genuinely has the chat keyed
                # under the other.
                lid_jid = getattr(self, "_phone_to_lid", {}).get(jid, "")
                primary = lid_jid if lid_jid else jid.replace("@s.whatsapp.net", "@c.us")
                resp = _post(primary)
                ok = _accepted(resp)
                if not ok:
                    logging.warning(
                        "[mute_chat] API error %s for %s: %s",
                        resp.status_code, primary, resp.text[:2000],
                    )
                    fallback = (jid.replace("@s.whatsapp.net", "@c.us")
                                if primary == lid_jid else "")
                    if fallback and fallback != primary:
                        logging.info(
                            "[mute_chat] Retrying %s with alternate JID form %s...",
                            jid, fallback,
                        )
                        resp = _post(fallback)
                        ok = _accepted(resp)
                        if ok:
                            logging.info("[mute_chat] Alternate JID form succeeded for %s", jid)
                        else:
                            logging.warning(
                                "[mute_chat] API error %s for %s (alternate form): %s",
                                resp.status_code, fallback, resp.text[:2000],
                            )
                if not ok:
                    # The mute/unmute call was previously fire-and-forget —
                    # nothing checked whether WPPConnect actually applied it,
                    # so a rejected request (bad JID form, session hiccup,
                    # WPPConnect error) left WinZapp showing a chat as muted
                    # that WhatsApp never muted, until the next full resync
                    # silently "corrected" it back — exactly the "mutei para
                    # sempre, funcionou, mas sumiu depois de reabrir o
                    # programa" report. Roll back immediately instead.
                    wx.CallAfter(self._on_mute_sync_rejected, jid, duration_secs != 0, rollback_expiry)
            except Exception as exc:
                logging.warning("[mute_chat] request failed for %s: %s", jid, exc)
                wx.CallAfter(self._on_mute_sync_rejected, jid, duration_secs != 0, rollback_expiry)
        threading.Thread(target=_do, daemon=True).start()

    def _on_mute_sync_rejected(self, jid: str, attempted_mute: bool, rollback_expiry=None):
        """Revert an optimistic mute/unmute that WhatsApp did not actually
        accept, and tell the user (runs on the wx main thread)."""
        self._apply_mute_state(jid, None if attempted_mute else rollback_expiry)
        if not self.background_mode:
            self.error_sound.play()
            key = "mute_chat_failed" if attempted_mute else "unmute_chat_failed"
            wx.MessageBox(
                self.i18n.t(key),
                self.i18n.t("error").format(app_name=self.app_name),
                wx.OK | wx.ICON_WARNING,
                self,
            )

    # ── Archive ───────────────────────────────────────────────────────────────

    def get_archived_unread_count(self) -> int:
        """Number of archived conversations with unread messages.

        Mirrors _update_title()'s main-list unread tally but restricted to
        archived chats — the counterpart that used to be missing entirely
        once archived chats stopped counting toward the window title.
        """
        deleted = self._deleted_chats
        return sum(
            1 for jid, chat in list(self.chats.items())
            if jid not in deleted
            and self.is_chat_archived(jid)
            and not getattr(self, "is_chat_locked", lambda _jid: False)(jid)
            and effective_unread_count(chat) > 0
        )

    def _archived_lookup_jids(self, jid: str) -> list:
        """Every JID string under which *jid*'s archive state might be filed.

        Both the normalized form and the raw one, because the two writers of
        ``_archived_chats`` disagree about which they store: normalize_chats()
        adds the ``self.chats`` KEY exactly as it found it, while
        _set_archived_state() adds the normalized JID. A chat still keyed
        ``@c.us`` (or carrying a ``:N`` device suffix) therefore sits in the set
        under a string _normalize_jid() rewrites, so looking it up normalized
        alone never found it. Plus the LID/phone counterpart, in both forms, for
        the same reason.

        Order matters: it is the order the answers are consulted in, and the
        first definite one wins.
        """
        out = []
        for candidate in (self._normalize_jid(jid), jid):
            if candidate and candidate not in out:
                out.append(candidate)
        jid_norm = self._normalize_jid(jid)
        if jid_norm.endswith("@lid"):
            alt = getattr(self, "_lid_to_phone", {}).get(jid_norm, "")
        else:
            alt = getattr(self, "_phone_to_lid", {}).get(jid_norm, "")
        if alt:
            for candidate in (self._normalize_jid(alt), alt):
                if candidate and candidate not in out:
                    out.append(candidate)
        return out

    @staticmethod
    def _chat_archive_flag(chat):
        """A chat record's stated archive flag, or None when it states none.

        The single parse of that tri-state — _compute_chat_lists() (which
        decides what the Archived tab shows) and is_chat_archived() (which
        decides whether a message may make a sound) have to answer this the
        same way, and used to hold their own copies of it.
        """
        if not isinstance(chat, dict):
            return None
        raw = chat.get("archive")
        if raw is None:
            raw = chat.get("archived")
        return _parse_bool_flag(raw)

    def _chat_entry_for_archive(self, candidate: str):
        """(key, record) for *candidate*, found by ``self.chats`` key or, failing
        that, by a record whose own ``remoteJid`` matches. (None, None) if absent.

        The second lookup is the one that was missing. A chat's key and its
        ``chat["remoteJid"]`` are NOT always the same string — a chat merged or
        renamed in place keeps the dict it already had (see
        _merge_lid_into_phone/deduplicate_chats, and _compute_chat_lists' own
        comment about rendering by remoteJid). The Archived tab decides by the
        KEY, so such a chat shows as archived; a message for it arrives resolved
        to its remoteJid, and a lookup by that string alone found nothing at all
        and answered "not archived".
        """
        chat = self.chats.get(candidate)
        if isinstance(chat, dict):
            return candidate, chat
        # list(): this also runs off the wx thread (on_new_message is dispatched
        # via CallAfter, but _compute_chat_lists' background thread reaches
        # is_chat_archived too), and _extract_lid_mapping() can be adding keys
        # on the Socket.IO thread meanwhile.
        for key, record in list(self.chats.items()):
            if not isinstance(record, dict):
                continue
            if self._normalize_jid(record.get("remoteJid", "")) == candidate:
                return key, record
        return None, None

    def is_chat_archived(self, jid: str) -> bool:
        """Whether this chat lives in the Archived tab.

        Has to agree with _compute_chat_lists(), which is what actually puts a
        row there — it decides ``arch_flag if arch_flag is not None else (key in
        _archived_chats)``. This answers the same question for a JID rather than
        for a record, and every candidate/lookup below exists to reach the same
        record and the same set entry that the list builder used.

        They disagreeing is a real reported bug, not a theoretical one: an
        archived conversation kept announcing "Nova mensagem de X" with the
        window open, because on_new_message()'s ``if archived and not
        is_current_conv: return`` guard was asking this method, and this method
        was answering False for a chat the user could see under Arquivadas.

        Precedence is per candidate and unchanged: a record that states a flag
        settles it, the persisted set only decides when the record says nothing.
        """
        if not jid:
            return False
        for candidate in self._archived_lookup_jids(jid):
            key, chat = self._chat_entry_for_archive(candidate)
            flag = self._chat_archive_flag(chat)
            if flag is not None:
                return flag
            if candidate in self._archived_chats:
                return True
            # The set is keyed by whatever normalize_chats() saw as the key,
            # which is exactly what the list builder tests — so when the record
            # was found under a different key, that key is the membership to
            # check, not the JID we were handed.
            if key and key in self._archived_chats:
                return True
        return False


    def _set_archived_state(self, jid: str, archived: bool):
        """Apply an archive decision to both the chat record and the metadata set.

        Both have to move together: the chat record is what the list builder
        and is_chat_archived() consult first (it carries the server's truth),
        while the set is what survives a restart. Supports LID <-> Phone JID mapping.
        """
        if not jid:
            return
        jid_norm = self._normalize_jid(jid)
        alt_jid = ""
        if jid_norm.endswith("@lid"):
            alt_jid = getattr(self, "_lid_to_phone", {}).get(jid_norm, "")
        else:
            alt_jid = getattr(self, "_phone_to_lid", {}).get(jid_norm, "")

        targets = [jid_norm]
        if alt_jid:
            targets.append(self._normalize_jid(alt_jid))

        for target in targets:
            if archived:
                self._archived_chats.add(target)
            else:
                self._archived_chats.discard(target)
            chat = self.chats.get(target)
            if chat is not None:
                chat["archive"] = archived
                chat["archived"] = archived

        if hasattr(self, "db") and self.db is not None:
            self.db.set_metadata_json("archived_chats", list(self._archived_chats))
            try:
                for target in targets:
                    chat = self.chats.get(target)
                    if chat is not None:
                        self.db.upsert_chat(target, chat)
            except Exception as exc:
                logging.warning("[_set_archived_state] DB update failed for %s: %s", jid, exc)
        self._schedule_set_chats()

    def archive_chat(self, jid: str):
        self._set_archived_state(jid, True)
        self._api_archive_chat(jid, archive=True)

    def unarchive_chat(self, jid: str):
        self._set_archived_state(jid, False)
        self._api_archive_chat(jid, archive=False)

    def _api_archive_chat(self, jid: str, archive: bool):
        url = (f"{self.wpp_server}:{self.wpp_port}"
               f"/api/{self.token}/archive-chat")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        # Prefer `@lid` for API operations if mapped, as WPPConnect expects it
        api_jid = jid
        if not api_jid.endswith("@lid"):
            alt_lid = getattr(self, "_phone_to_lid", {}).get(self._normalize_jid(jid), "")
            if alt_lid:
                api_jid = alt_lid

        try:
            resp = api_post(
                url,
                json={"phone": api_jid, "value": archive, "isGroup": jid.endswith("@g.us")},
                headers=headers,
                timeout=10,
            )
            if not resp.ok:
                print(f"[archive_chat] API error {resp.status_code} for {jid} (api_jid: {api_jid}): {resp.text[:200]}")
        except Exception as exc:
            print(f"[archive_chat] Request failed for {jid}: {exc}")

    # ── Delete / Clear ────────────────────────────────────────────────────────

    def is_chat_deleted(self, jid: str) -> bool:
        return jid in self._deleted_chats

    def delete_chat_local(self, jid: str):
        self._forget_chat_lock(jid)
        if jid not in self._deleted_chats:
            self._deleted_chats.add(jid)
        if jid.endswith("@s.whatsapp.net"):
            lid_jid = getattr(self, "_phone_to_lid", {}).get(jid)
            if lid_jid and lid_jid not in self._deleted_chats:
                self._deleted_chats.add(lid_jid)
        elif jid.endswith("@lid"):
            phone_jid = getattr(self, "_lid_to_phone", {}).get(jid)
            if phone_jid and phone_jid not in self._deleted_chats:
                self._deleted_chats.add(phone_jid)
        self.chats.pop(jid, None)
        if hasattr(self, "db") and self.db is not None:
            self.db.set_metadata_json("deleted_chats", list(self._deleted_chats))
            # Drop the row as well, not just the "deleted" marker.  Leaving it
            # in the database meant the conversation was reloaded into
            # self.chats on the next start and only hidden by the marker — so
            # anything that lost or bypassed the marker (as get_remote_chats
            # did, reading it from the wrong place) brought the chat back.
            try:
                self.db.delete_chat(jid)
            except Exception as exc:
                logging.warning("[delete_chat_local] DB delete failed for %s: %s", jid, exc)
        self._schedule_save()
        self._schedule_set_chats()

    def clear_chat_messages_local(self, jid: str, record_cutoff: bool = True,
                                  keep_starred: bool = True):
        """Empty a conversation locally, keeping it in the chat list.

        Clearing removes the messages and the last-message preview — it must
        NOT remove the conversation itself; that is what "delete chat" does.
        Starred messages survive when `keep_starred` is True, which is the
        default of WhatsApp Web's own "keep starred messages" checkbox; the
        user can untick it in the confirmation to clear those too.
        `record_cutoff` is False when we are only mirroring a clear that already
        happened on the phone (no new cutoff to remember, the server is the
        source of truth).

        Returns the recorded cutoff (or None). The separate cutoff for starred
        messages is NOT written here: clear_chat() writes it through
        _record_starred_clear_cutoff() only once the server confirms it
        cleared them too.
        """
        chat = self.chats.get(jid)
        if not chat:
            return None
        cutoff = None
        records = chat.get("messages", {}).get("messages", {}).get("records", [])
        starred = (
            [m for m in records if isinstance(m, dict) and m.get("starred")]
            if keep_starred else []
        )
        chat.setdefault("messages", {}).setdefault("messages", {})["records"] = starred
        chat["unreadCount"] = 0
        if record_cutoff:
            cutoff = int(time.time())
            self.settings.setdefault("cleared_chats", {})[jid] = cutoff
            self.save_settings()
        self._schedule_save(dirty_jid=jid)
        # Recomputes lastMessage/t from the survivors (a kept starred message,
        # or None/0 if there aren't any) and persists the chat row itself.
        previous_t = chat.get("t")
        self._recompute_chat_last_message(jid)
        if not starred and not chat.get("t"):
            # No survivors means _recompute_chat_last_message() just zeroed
            # chat["t"], which _chat_last_ts() treats as ts=1 — sorting the
            # cleared chat to the very bottom of the list. Clearing must keep
            # the conversation at its current position (only the preview goes
            # away), so restore the pre-clear timestamp for sort purposes.
            chat["t"] = previous_t
            if hasattr(self, "db") and self.db is not None:
                try:
                    self.db.upsert_chat(jid, chat)
                except Exception as exc:
                    logging.warning(
                        "[clear_chat_messages_local] DB upsert failed for %s: %s", jid, exc
                    )
        if hasattr(self, "db") and self.db is not None:
            try:
                keep_ids = [
                    m.get("key", {}).get("id", "") for m in starred
                    if m.get("key", {}).get("id")
                ]
                self.db.delete_chat_messages_except(jid, keep_ids)
            except Exception as exc:
                logging.warning("[clear_chat_messages_local] DB clear failed for %s: %s", jid, exc)
        return cutoff

    def _announce_starred_clear_unsupported(self):
        """Say, once per process, that the installed client/api kept the
        starred messages on the phone. Runs on the main thread, so the flag
        needs no lock: a bulk clear of 20 chats must not read the same long
        sentence 20 times, and the API version cannot change mid-process.
        The menu path is built from the menu's own keys so it cannot drift
        from what the user will actually find there."""
        if getattr(self, "_starred_clear_unsupported_announced", False):
            return
        self._starred_clear_unsupported_announced = True
        t = self.i18n.t
        self.output(t("clear_chat_starred_kept_on_phone").format(
            menu=t("menu_help").replace("&", ""),
            option=t("menu_force_reinstall_wpp").replace("&", ""),
        ))

    def _record_starred_clear_cutoff(self, jid: str, cutoff):
        """Remember that a clear of `jid` also dropped its starred messages.

        The ordinary cutoff exempts starred messages (see
        _is_cleared_message), so without this the next sync would bring the
        ones just cleared back. Only ever advanced, never removed: a later
        clear that keeps starred messages must not resurrect these.
        """
        cutoff = int(cutoff or time.time())
        starred_cutoffs = self.settings.setdefault("cleared_starred_chats", {})
        if cutoff > int(starred_cutoffs.get(jid) or 0):
            starred_cutoffs[jid] = cutoff
            self.save_settings()

    def delete_chat(self, jid: str):
        """Delete chat locally and sync to WPPConnect API."""
        self.delete_chat_local(jid)
        if jid.endswith("@g.us"):
            # NEVER send delete-chat for a group.  WhatsApp has no concept of
            # "delete this group conversation but stay in it": its internal
            # sendDelete on a group you are still a member of exits the group
            # first — which is how users who only meant to tidy up their chat
            # list found themselves removed from groups.  Deleting a group is
            # therefore local-only here; leaving is a separate, explicit action
            # (leave_group / the "Sair do grupo" menu item).
            logging.info("[delete_chat] %s is a group — deleting locally only "
                         "(a server-side delete would leave the group).", jid)
            return
        def _api():
            phone = jid.replace("@s.whatsapp.net", "@c.us")
            url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/delete-chat"
            headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            try:
                r = api_post(
                    url, json={"phone": [phone], "isGroup": phone.endswith("@g.us")},
                    headers=headers, timeout=10,
                )
                if not r.ok:
                    logging.warning("[delete_chat] API error %s for %s: %s", r.status_code, jid, r.text[:200])
            except Exception as exc:
                logging.warning("[delete_chat] Request failed for %s: %s", jid, exc)
        threading.Thread(target=_api, daemon=True).start()

    def clear_chat(self, jid: str, keep_starred: bool = True):
        """Clear chat messages locally and sync to WPPConnect API."""
        cutoff = self.clear_chat_messages_local(jid, keep_starred=keep_starred)
        def _api():
            phone = jid.replace("@s.whatsapp.net", "@c.us")
            url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/clear-chat"
            headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            try:
                # An older client/api ignores keepStarred and keeps the starred
                # messages on the phone. That is why the starred cutoff waits
                # for the server's echo below: recording it regardless would
                # hide, in WinZapp only and for good, messages still on the
                # phone. Without it the next sync brings them back instead.
                r = api_post(
                    url,
                    json={
                        "phone": [phone],
                        "isGroup": phone.endswith("@g.us"),
                        "keepStarred": bool(keep_starred),
                    },
                    headers=headers, timeout=10,
                )
                if not r.ok:
                    logging.warning("[clear_chat] API error %s for %s: %s", r.status_code, jid, r.text[:200])
                elif not keep_starred:
                    try:
                        body = r.json()
                    except Exception:
                        body = None
                    echo = clear_chat_keep_starred_echo(body)
                    if echo is False and clear_chat_applied(body, phone):
                        wx.CallAfter(self._record_starred_clear_cutoff, jid, cutoff)
                    elif echo is False:
                        # The server understood keepStarred=false, but WhatsApp
                        # Web did not confirm the clear: nothing proves the
                        # starred messages are gone from the phone.
                        logging.warning(
                            "[clear_chat] %s: the clear was not confirmed by "
                            "WhatsApp Web — starred messages not treated as cleared.", jid,
                        )
                    else:
                        logging.warning(
                            "[clear_chat] %s: server did not confirm keepStarred=false "
                            "(outdated client/api?) — starred messages kept on the phone.", jid,
                        )
                        wx.CallAfter(self._announce_starred_clear_unsupported)
            except Exception as exc:
                logging.warning("[clear_chat] Request failed for %s: %s", jid, exc)
        threading.Thread(target=_api, daemon=True).start()

    def _resolve_jid_for_chat_state(self, jid: str) -> str:
        """Resolve to the active JID for chat state, preferring @lid if mapped."""
        if not jid:
            return jid
        if jid.endswith(("@g.us", "@broadcast")):
            return jid.replace("@s.whatsapp.net", "@c.us")
        
        normalized = jid.replace("@c.us", "@s.whatsapp.net")
        lid = getattr(self, "_phone_to_lid", {}).get(normalized, "")
        if lid:
            return lid.replace("@s.whatsapp.net", "@lid")
            
        if jid.endswith("@lid"):
            return jid
            
        return jid.replace("@s.whatsapp.net", "@c.us")

    def send_typing_status(self, jid: str, value: bool, is_group: bool = False):
        """Notify WPPConnect that the user started or stopped typing."""
        def _api():
            phone = self._resolve_jid_for_chat_state(jid)
            url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/typing"
            headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            try:
                r = api_post(
                    url,
                    json={"phone": phone, "value": value, "isGroup": is_group},
                    headers=headers,
                    timeout=10,
                )
                if not r.ok:
                    logging.warning("[send_typing_status] API error %s: %s", r.status_code, r.text)
            except Exception as exc:
                logging.warning("[send_typing_status] Request failed: %s", exc)
        threading.Thread(target=_api, daemon=True).start()

    def send_recording_status(self, jid: str, value: bool, is_group: bool = False):
        """Notify WPPConnect that the user started or stopped recording audio."""
        def _api():
            phone = self._resolve_jid_for_chat_state(jid)
            url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/recording"
            headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            try:
                r = api_post(
                    url,
                    json={"phone": phone, "duration": 0, "value": value, "isGroup": is_group},
                    headers=headers,
                    timeout=10,
                )
                if not r.ok:
                    logging.warning("[send_recording_status] API error %s: %s", r.status_code, r.text)
            except Exception as exc:
                logging.warning("[send_recording_status] Request failed: %s", exc)
        threading.Thread(target=_api, daemon=True).start()

    def _is_cleared_message(self, jid: str, msg: dict) -> bool:
        """
        True if `msg` predates the user's last "clear chat" action for `jid`.

        Clearing a conversation records a cutoff timestamp in
        settings["cleared_chats"]. Without consulting it, the next history sync
        (or a WebSocket re-delivery) would simply repopulate the chat, making the
        clear appear to do nothing. Messages received after the clear have a
        newer timestamp and are kept.

        Starred messages kept by a clear are not "cleared", whatever their
        timestamp. clear_chat_messages_local() deliberately keeps them (starring is meant
        to make a message durable, same as WhatsApp itself), but every path
        that rebuilds a conversation — the history sync, the on-disk cache
        merge, a WebSocket re-delivery — filtered them right back out through
        this cutoff, so the survivors it had just saved disappeared again on
        the next sync or restart. Reported live as "limpar uma conversa
        tambem apaga as mensagens favoritas".

        Unless the user unticked "keep starred messages" when clearing: that
        records settings["cleared_starred_chats"], the cutoff starred
        messages are judged against instead.
        """
        if not isinstance(msg, dict):
            return False
        cutoff_key = "cleared_starred_chats" if msg.get("starred") else "cleared_chats"
        cutoff = self.settings.get(cutoff_key, {}).get(jid)
        if not cutoff:
            return False
        try:
            ts = int(msg.get("messageTimestamp", 0) or 0)
        except (ValueError, TypeError):
            return False
        return bool(ts) and ts < cutoff

    # ── Pin ───────────────────────────────────────────────────────────────────

    def is_chat_pinned(self, jid: str) -> bool:
        return jid in self._pinned_chats

    def _apply_pin_state(self, jid: str, pinned: bool):
        """Local-only half of pin/unpin: mutate _pinned_chats (+ its alt-JID
        mirror), persist to DB metadata, and refresh the chat list. Split out
        from pin_chat()/unpin_chat() so _sync_pin_to_server() can call this
        again to roll back the optimistic change if WhatsApp rejects it,
        without recursing back into a server call."""
        normalized = self._normalize_jid(jid)
        if pinned:
            self._pinned_chats.add(normalized)
        else:
            self._pinned_chats.discard(normalized)
        # Also mirror onto the alternate JID form if present
        if normalized.endswith("@lid"):
            alt = getattr(self, "_lid_to_phone", {}).get(normalized, "")
            if alt:
                alt = self._normalize_jid(alt)
                self._pinned_chats.add(alt) if pinned else self._pinned_chats.discard(alt)
        else:
            alt = getattr(self, "_phone_to_lid", {}).get(normalized, "")
            if alt:
                self._pinned_chats.add(alt) if pinned else self._pinned_chats.discard(alt)

        if hasattr(self, "db") and self.db is not None:
            self.db.set_metadata_json("pinned_chats", list(self._pinned_chats))
        self._schedule_set_chats()

    def pin_chat(self, jid: str):
        self._apply_pin_state(jid, True)
        self._sync_pin_to_server(jid, pinned=True)

    def unpin_chat(self, jid: str):
        self._apply_pin_state(jid, False)
        self._sync_pin_to_server(jid, pinned=False)

    def _sync_pin_to_server(self, jid: str, pinned: bool):
        def _do():
            try:
                # Prefer `@lid` for API operations if mapped, as WPPConnect expects it
                api_jid = jid
                if not api_jid.endswith("@lid"):
                    alt_lid = getattr(self, "_phone_to_lid", {}).get(self._normalize_jid(jid), "")
                    if alt_lid:
                        api_jid = alt_lid

                if api_jid.endswith("@s.whatsapp.net"):
                    api_jid = api_jid.rsplit("@", 1)[0] + "@c.us"
                url = (f"{self.wpp_server}:{self.wpp_port}"
                       f"/api/{self.token}/pin-chat")
                payload = {
                    "phone": [api_jid],
                    "state": "true" if pinned else "false",
                    "isGroup": jid.endswith("@g.us"),
                }
                resp = api_post(
                    url, json=payload,
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=10,
                )
                if not resp.ok:
                    logging.warning("[pin_chat] API error %s for %s (api_jid: %s): %s",
                                    resp.status_code, jid, api_jid, resp.text[:200])
                    # WhatsApp rejected the change — most commonly because it
                    # only allows 3 pinned chats at once, an existing-account
                    # rule WPPConnect enforces server-side that WinZapp never
                    # checked before sending the request. The optimistic local
                    # update above (_apply_pin_state, already applied before
                    # this thread ran) was never actually accepted by
                    # WhatsApp, so left uncorrected it silently drifted out of
                    # sync — the chat looked pinned in WinZapp until the next
                    # periodic chat-list poll (up to 60s later) quietly
                    # "unpinned" it again, which is exactly the erratic
                    # pin behaviour reported. Roll it back immediately and
                    # tell the user why instead of waiting for that poll.
                    wx.CallAfter(self._on_pin_sync_rejected, jid, pinned)
            except Exception as exc:
                logging.warning("[pin_chat] request failed for %s: %s", jid, exc)
        threading.Thread(target=_do, daemon=True).start()

    def _on_pin_sync_rejected(self, jid: str, attempted_pinned: bool):
        """Revert an optimistic pin/unpin that WhatsApp did not actually
        accept, and tell the user (runs on the wx main thread)."""
        self._apply_pin_state(jid, not attempted_pinned)
        if not self.background_mode:
            self.error_sound.play()
            key = "pin_chat_failed" if attempted_pinned else "unpin_chat_failed"
            wx.MessageBox(
                self.i18n.t(key),
                self.i18n.t("error").format(app_name=self.app_name),
                wx.OK | wx.ICON_WARNING,
                self,
            )
