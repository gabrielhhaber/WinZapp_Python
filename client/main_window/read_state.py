"""ReadStateMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import logging
import threading
import time
import wx
from core.api_client import api_post
from core.bulk_read_state import run_bulk_read_state
import requests


class ReadStateMixin:
    """Read/unread state: marking conversations read or unread and keeping the
    local-read anchor.
    """

    def _persist_locally_read_at(self):
        """Write the read-ack map to DB metadata.

        Kept tiny (one int per chat) and written only when the map actually
        changes — a mark-as-read, or an entry retiring because the server has
        since reported genuinely newer activity for that chat.
        """
        db = getattr(self, "db", None)
        if db is None:
            return
        try:
            db.set_metadata_json(
                "locally_read_at", dict(getattr(self, "_locally_read_at", {}))
            )
        except Exception as exc:
            logging.warning("[mark_as_read] failed to persist locally_read_at: %s", exc)

    def _anchor_unread_to_local_read(self, remote_jid: str) -> None:
        """Record that this chat's _new_since_read counter starts from a read.

        Both identities are stored — the raw key mark_conversation_as_read()
        was called with, and its normalized form — purely so the lookup
        cannot depend on which of the two a caller happened to hold. It is
        belt-and-braces rather than a bridge: _resolve_chat_for_event()
        returns the key self.chats actually holds the chat under, and
        mark_conversation_as_read() is called with that same key, so today
        the second entry is inert.

        It is deliberately NOT an @lid bridge. _normalize_jid() leaves @lid
        untouched on purpose, and resolving one here would be wrong rather
        than merely redundant: a read of the @lid entry would anchor the
        phone entry, whose own _new_since_read has no read behind it, and
        that unearned anchor is precisely what authorises the clamp that
        collapses a backlog. When _merge_lid_into_phone() renames a key, the
        anchor, _new_since_read and _locally_read_at are orphaned together —
        the clamp's own `_new_since_read` guard then reads false and it does
        not run, which is the safe direction.
        """
        if not hasattr(self, "_unread_read_anchors"):
            self._unread_read_anchors = set()
        self._unread_read_anchors.add(remote_jid)
        self._unread_read_anchors.add(self._normalize_jid(remote_jid))

    def _drop_unread_local_read_anchor(self, remote_jid: str) -> None:
        """Forget the anchor — the read behind it was undone or reversed."""
        anchors = getattr(self, "_unread_read_anchors", None)
        if not anchors:
            return
        anchors.discard(remote_jid)
        anchors.discard(self._normalize_jid(remote_jid))

    def _unread_anchored_to_local_read(self, remote_jid: str) -> bool:
        """Whether _new_since_read[jid] counts from a local read of this chat.

        The distinction is the whole point of the anchor. on_new_message()
        creates a _new_since_read entry for ANY chat that receives a message,
        read here or not, so the counter alone cannot say whether it measures
        "since the user read this chat" (a real ceiling for the absolute
        total WhatsApp Web reports) or merely "since this process started"
        (no ceiling at all — everything unread before the launch is missing
        from it). Only mark_conversation_as_read() sets the anchor, and it is
        deliberately in memory only: after a restart the counter starts from
        zero again, so the ceiling it would imply is gone too.
        """
        anchors = getattr(self, "_unread_read_anchors", None)
        if not anchors:
            return False
        return remote_jid in anchors or self._normalize_jid(remote_jid) in anchors

    def mark_conversation_as_read(
        self, remote_jid: str, force: bool = False, batched: bool = False
    ):
        """Mark conversation as read locally and notify WPPConnect.

        ``batched=True`` is for mark_conversations_as_read(): the local part
        runs as usual, but the DB persist and the /send-seen are left to the
        caller, and the remote job is returned as
        ``(remote_jid, previous_unread, read_timestamp)`` — None when nothing
        needs sending.
        """
        chat = self.chats.get(remote_jid)
        if chat is None:
            return

        unread = int(chat.get("unreadCount") or 0)
        chat["unreadCount"] = 0
        # Remember this chat's last-activity timestamp as of the moment we
        # cleared it locally — see the periodic list-chats merge in
        # get_remote_chats(), which uses this to tell a genuinely new
        # unreadCount apart from WhatsApp Web's own server-side read receipt
        # simply not having caught up with this mark-as-read yet.
        if not hasattr(self, "_locally_read_at"):
            self._locally_read_at = {}
        self._locally_read_at[remote_jid] = int(chat.get("t", 0) or 0)
        # Persisted (not just in-memory): the stale server-side unread count
        # this guard exists to reject outlives the process, so the guard has
        # to as well — see prepare_sync()'s "8. locally_read_at" block.
        # A batch persists once at the end instead of one DB write per chat.
        if not batched:
            self._persist_locally_read_at()
        if not hasattr(self, "_new_since_read"):
            self._new_since_read = {}
        self._new_since_read[remote_jid] = 0
        # From here on this chat's _new_since_read entry means "arrivals since
        # a read that actually happened", which is the only reading that lets
        # on_chat_unread_update() clamp an absolute server total to it — see
        # _unread_anchored_to_local_read().
        self._anchor_unread_to_local_read(remote_jid)
        self._schedule_save(dirty_jid=remote_jid)
        # Immediate single-row update: unlike _schedule_set_chats()/set_chats(),
        # this isn't suppressed while a media sync is running, so the badge
        # clears right away instead of only after the sync eventually finishes
        # (previously observed as a 10-20+ second — or longer — delay).
        wx.CallAfter(self._refresh_chat_row_in_list, self._normalize_jid(remote_jid))
        wx.CallAfter(self._schedule_set_chats)

        if unread == 0 and not force:
            return None

        if batched:
            # Taken now, not at failure time: this is the value the
            # _locally_read_at marker above was set to, which is what the
            # rollback compares against.
            return (remote_jid, unread, int(chat.get("t", 0) or 0))

        self._sync_conversation_read_state(
            remote_jid,
            unread=False,
            on_failure=lambda: wx.CallAfter(
                self._restore_unread_after_send_seen_failure,
                remote_jid,
                unread,
                int(chat.get("t", 0) or 0),
            ),
        )

    def mark_conversations_as_read(self, remote_jids, force: bool = False) -> int:
        """Mark many conversations as read: locally at once, remotely paced.

        Call from the main thread: it mutates self.chats. Badges clear
        immediately; the /send-seen calls go through
        core.bulk_read_state.run_bulk_read_state() — a small pool, retried in
        rounds until every chat is confirmed or WhatsApp stops answering —
        instead of one simultaneous request per chat, which timed out under
        its own load. Chats WhatsApp never confirmed get their unread count
        back and the user is told how many. Returns how many are being sent.
        """
        jobs = {}
        for jid in remote_jids:
            job = self.mark_conversation_as_read(jid, force=force, batched=True)
            if job is not None:
                jobs[job[0]] = job
        self._persist_locally_read_at()
        if not jobs:
            return 0
        logging.info("[mark_read_bulk] Sending read state for %d chats.", len(jobs))

        def _worker():
            failed = run_bulk_read_state(
                list(jobs),
                lambda jid: self._send_read_state_blocking(jid, False, attempts=1),
            )
            logging.info(
                "[mark_read_bulk] Done: %d confirmed, %d failed.",
                len(jobs) - len(failed), len(failed),
            )
            if failed:
                wx.CallAfter(self._on_bulk_read_failed, [jobs[jid] for jid in failed])

        threading.Thread(target=_worker, daemon=True).start()
        return len(jobs)

    def _on_bulk_read_failed(self, failed_jobs):
        """Roll back the chats a bulk mark-as-read could not confirm.

        One DB write and one list rebuild for the whole batch: done per chat,
        a failed run of hundreds froze the main thread and flooded the screen
        reader right as the failure was being announced.
        """
        restored = False
        for remote_jid, previous_unread, read_timestamp in failed_jobs:
            restored |= bool(self._restore_unread_after_send_seen_failure(
                remote_jid, previous_unread, read_timestamp, batched=True
            ))
        if restored:
            self._persist_locally_read_at()
            self._schedule_set_chats()
        self.output(
            self.i18n.t("mark_read_bulk_failed").format(count=len(failed_jobs)),
            # Can arrive up to ~2 min after the command; not worth cutting off
            # whatever the screen reader is reading by then.
            interrupt=False,
        )

    def _sync_conversation_read_state(self, remote_jid: str, unread: bool, on_failure):
        """Apply a read-state change remotely in the background."""
        def _do_api():
            # Guarded here, not only inside the sender: an exception escaping
            # it would end the thread without on_failure(), leaving the
            # optimistic local change in place with nothing behind it.
            try:
                ok = self._send_read_state_blocking(remote_jid, unread)
            except Exception:
                logging.exception("[read_state] Unexpected send-seen failure")
                ok = False
            if not ok:
                on_failure()
        threading.Thread(target=_do_api, daemon=True).start()

    def _send_read_state_blocking(
        self, remote_jid: str, unread: bool, attempts: int = 3
    ) -> bool:
        """POST /send-seen, trying the known JID aliases; True once confirmed."""
        # Prefer @lid JID for WPPConnect if mapped
        target_phone = remote_jid
        if not target_phone.endswith("@lid"):
            alt_lid = getattr(self, "_phone_to_lid", {}).get(self._normalize_jid(remote_jid), "")
            if alt_lid:
                target_phone = alt_lid

        if target_phone.endswith("@s.whatsapp.net"):
            target_phone = target_phone.rsplit("@", 1)[0] + "@c.us"
        
        is_lid_target = target_phone.endswith("@lid")

        def _send_seen(phone: str, is_lid: bool) -> "requests.Response | None":
            url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/send-seen"
            headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            payload = {
                "phone": phone,
                "isGroup": phone.endswith("@g.us"),
                "unread": unread,
            }
            if is_lid:
                payload["isLid"] = True
            return api_post(url, json=payload, headers=headers, timeout=10)

        try:
            fallback_phone = remote_jid
            if fallback_phone.endswith("@lid"):
                fallback_phone = getattr(self, "_lid_to_phone", {}).get(
                    fallback_phone, fallback_phone
                )
            else:
                fallback_phone = getattr(self, "_phone_to_lid", {}).get(
                    self._normalize_jid(fallback_phone), fallback_phone
                )
            if fallback_phone.endswith("@s.whatsapp.net"):
                fallback_phone = fallback_phone.rsplit("@", 1)[0] + "@c.us"

            targets = [(target_phone, is_lid_target)]
            if fallback_phone != target_phone:
                targets.append((fallback_phone, fallback_phone.endswith("@lid")))

            for attempt in range(attempts):
                for phone, is_lid in targets:
                    try:
                        resp = _send_seen(phone, is_lid)
                    except Exception as exc:
                        logging.warning(
                            "[mark_as_read] Request failed for %s (attempt %s): %s",
                            phone, attempt + 1, exc,
                        )
                        continue
                    if resp.ok:
                        try:
                            results = resp.json().get("response", {}).get("data")
                        except (AttributeError, TypeError, ValueError):
                            results = None
                        if isinstance(results, list) and results and all(
                            result is True for result in results
                        ):
                            return True
                    logging.warning(
                        "[read_state] API response %s for %s (unread=%s, attempt %s): %s",
                        resp.status_code, phone, unread, attempt + 1, resp.text[:200],
                    )
                if attempt < attempts - 1:
                    time.sleep(attempt + 1)
        except Exception:
            logging.exception("[mark_as_read] Unexpected send-seen failure")
        return False

    def _restore_unread_after_send_seen_failure(
        self, remote_jid: str, previous_unread: int, read_timestamp: int,
        batched: bool = False,
    ) -> bool:
        """Undo an optimistic local read when WhatsApp rejected every attempt.

        Returns whether anything was undone. ``batched=True`` leaves the DB
        persist and the list refresh to the caller (see _on_bulk_read_failed).
        """
        normalized = self._normalize_jid(remote_jid)
        chat = self.chats.get(normalized) or self.chats.get(remote_jid)
        if chat is None:
            return False
        marker = getattr(self, "_locally_read_at", {}).get(normalized)
        if marker is None:
            marker = getattr(self, "_locally_read_at", {}).get(remote_jid)
        if (
            marker != read_timestamp
            or int(chat.get("t", 0) or 0) != read_timestamp
            or int(chat.get("unreadCount") or 0) != 0
            or max(
                getattr(self, "_new_since_read", {}).get(normalized, 0),
                getattr(self, "_new_since_read", {}).get(remote_jid, 0),
            ) > 0
        ):
            return False
        chat["unreadCount"] = max(0, int(previous_unread or 0))
        self._locally_read_at.pop(normalized, None)
        self._locally_read_at.pop(remote_jid, None)
        # The read is being undone, so the anchor it installed goes with it.
        self._drop_unread_local_read_anchor(normalized)
        self._drop_unread_local_read_anchor(remote_jid)
        self._schedule_save(dirty_jid=normalized)
        if batched:
            return True
        self._persist_locally_read_at()
        self._refresh_chat_row_in_list(normalized)
        self._schedule_set_chats()
        return True

    def mark_conversation_as_unread(self, remote_jid: str):
        chat = self.chats.get(remote_jid)
        if chat is not None:
            previous_unread = int(chat.get("unreadCount") or 0)
            timestamp = int(chat.get("t", 0) or 0)
            chat["unreadCount"] = 1
            if hasattr(self, "_locally_read_at"):
                self._locally_read_at.pop(remote_jid, None)
                self._persist_locally_read_at()
            if hasattr(self, "_new_since_read"):
                self._new_since_read.pop(remote_jid, None)
            # Explicitly marking a chat unread undoes the read this anchor
            # stood for; anything counted from here on is arrivals in a chat
            # with no local read behind it again.
            self._drop_unread_local_read_anchor(remote_jid)
            self._schedule_save(dirty_jid=remote_jid)
            wx.CallAfter(self.set_chats)
            self._sync_conversation_read_state(
                remote_jid,
                unread=True,
                on_failure=lambda: wx.CallAfter(
                    self._restore_unread_after_mark_unread_failure,
                    remote_jid,
                    previous_unread,
                    timestamp,
                ),
            )

    def _restore_unread_after_mark_unread_failure(
        self, remote_jid: str, previous_unread: int, timestamp: int
    ):
        """Undo an optimistic mark-unread if no newer state replaced it."""
        chat = self.chats.get(remote_jid)
        if (
            chat is None
            or int(chat.get("t", 0) or 0) != timestamp
            or int(chat.get("unreadCount") or 0) != 1
        ):
            return
        chat["unreadCount"] = max(0, previous_unread)
        # Deliberately does not put _locally_read_at or the read anchor back:
        # this undoes a mark-UNREAD, so the chat returns to a read-looking
        # state with no ceiling attached. Leaving both absent means the next
        # server count is taken as it comes instead of being clamped to a
        # local counter that no read backs — the safe direction, and the same
        # one _restore_unread_after_send_seen_failure() takes from the other
        # side by dropping the anchor outright.
        self._schedule_save(dirty_jid=remote_jid)
        self._refresh_chat_row_in_list(remote_jid)
        self._schedule_set_chats()
