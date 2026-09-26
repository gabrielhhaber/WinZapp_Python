"""ChatEventsMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import logging
import time
import wx
from main_window.message_rules import (
    _discount_non_countable_unread,
    note_unread_discount_state,
    reconcile_open_chat_unread,
)
from core.utils import (
    effective_unread_count,
    is_phone_like,
)
from core.locale_format import (
    get_datetime_format,
    get_time_format,
)


class ChatEventsMixin:
    """Chat-level server events: message status (acks), presence, unread counters,
    archive and pin updates.
    """

    def on_message_status_update(self, update: dict, skip_panel_refresh: bool = False):
        """
        Handle a messages.update WebSocket event on the main thread.
        Updates MessageUpdate list on the cached message record and refreshes
        the status icon shown in the active conversation.

        skip_panel_refresh: skip the visible row refresh instead of firing it
        here. Used only by mark_audio_message_played() when a voice note's
        auto-chain is about to move list focus to the next one — the caller
        (ConversationsPanel._auto_chain_next_audio()) fires that refresh
        itself once it's actually safe to, see that method's own docstring.
        """
        key       = update.get("key", {})
        msg_id    = key.get("id", "")
        status    = update.get("status", "") or str(update.get("update", {}).get("status", ""))
        if not msg_id or not status:
            return
        remote_jid = self._normalize_jid(key.get("remoteJid", ""))
        logging.info(f"[on_message_status_update] msg_id={msg_id} status={status} remote_jid={remote_jid}")

        # Try all known JID forms (@lid <-> @s.whatsapp.net) to find the chat
        candidates = [remote_jid]
        if remote_jid.endswith("@lid"):
            phone_jid = getattr(self, "_lid_to_phone", {}).get(remote_jid, "")
            if phone_jid:
                candidates.append(phone_jid)
        elif remote_jid.endswith("@s.whatsapp.net"):
            lid = getattr(self, "_phone_to_lid", {}).get(remote_jid, "")
            if lid:
                candidates.append(lid)

        chat_jid = next((j for j in candidates if j in self.chats), None)
        found_msg = None
        found_chat_jid = None

        if chat_jid:
            records = (
                self.chats[chat_jid]
                    .get("messages", {})
                    .get("messages", {})
                    .get("records", [])
            )
            for msg in records:
                if msg.get("key", {}).get("id") == msg_id:
                    msg.setdefault("MessageUpdate", []).append({"status": status, "ts": time.time()})
                    found_msg = msg
                    found_chat_jid = chat_jid
                    logging.info(f"[on_message_status_update] Updated status to {status} for msg_id={msg_id} in records of chat={chat_jid}")
                    break
            if not found_msg:
                logging.warning(f"[on_message_status_update] Message {msg_id} not found in records of chat {chat_jid}")
        else:
            logging.warning(f"[on_message_status_update] Chat not found in self.chats for candidates: {candidates}")

        # ── Fallback: scan all chats in memory when the initial candidates miss ──
        # This happens when a status event arrives with remote_jid equal to our own
        # LID (the account LID), not the recipient's JID, so none of the candidates
        # matched. The message is actually stored in the recipient's chat.
        if not found_msg:
            for jid, chat_data in self.chats.items():
                if jid in candidates:
                    continue
                recs = (
                    chat_data.get("messages", {})
                             .get("messages", {})
                             .get("records", [])
                )
                for msg in recs:
                    if msg.get("key", {}).get("id") == msg_id:
                        msg.setdefault("MessageUpdate", []).append({"status": status, "ts": time.time()})
                        found_msg = msg
                        found_chat_jid = jid
                        logging.info(
                            f"[on_message_status_update] Fallback: updated status to {status} "
                            f"for msg_id={msg_id} in chat={jid}"
                        )
                        break
                if found_msg:
                    break

        # ── Persist updated message record to DB ─────────────────────────────────
        # insert_message rewrites message_json (which carries MessageUpdate, what
        # the UI reads) *and* the indexed status column, so both stay in step.
        if found_msg and found_chat_jid:
            try:
                self.db.insert_message(found_chat_jid, found_msg)
            except Exception as e:
                logging.error(f"[on_message_status_update] Failed to persist status update to DB: {e}")

        # A failed ack retires the "sending"/"unconfirmed" state a virtual message
        # may still be showing: WhatsApp has given its verdict, so stop implying
        # the send is still in flight.
        try:
            if found_msg and int(status) < 0:
                found_msg.pop("_send_unconfirmed", None)
                found_msg["_local_pending"] = False
                found_msg["_send_failed"] = True
                logging.warning("[on_message_status_update] msg_id=%s reported FAILED by WhatsApp "
                                "(status=%s) — marking as not delivered", msg_id, status)
        except (TypeError, ValueError):
            pass

        if hasattr(self, "conversations_panel") and not skip_panel_refresh:
            self.conversations_panel.refresh_message_status(msg_id, status)

        # The chat list's preview line carries this message's delivery status
        # too — _last_msg_preview() appends _map_status(last) to it — but the
        # only repaint fired above is the open conversation's own row. Nothing
        # here ever told the conversations list to repaint, so a chat kept
        # showing "Pendente" until some unrelated event (another message
        # landing anywhere, a sync) happened to trigger set_chats(). Reported
        # live as a preview still reading "Pendente" seconds after the message
        # had visibly been sent.
        #
        # Update this chat's row in the conversations list, cheaply.
        #
        # move_chat_row_to_top() rather than refresh_chat_row_text(): an ack
        # usually only changes the row's TEXT (Pendente -> Enviada), but not
        # always its position. A message you just sent enters the list as a
        # local pending record and the ack is what settles the chat's real
        # ordering timestamp, so an ack genuinely can be the thing that should
        # float the chat up. Repainting the row in place — which is all
        # refresh_chat_row_text() does — left a chat you had just posted to
        # sitting where it was, because the ack had taken over from the
        # set_chats() call that used to do the reordering. move_chat_row_to_top()
        # covers both: it repaints in place when the chat is already at the top
        # of its group, moves the row when it now outranks it, and returns
        # False (falling back to the full recompute below) whenever the
        # position isn't something it can settle on its own.
        #
        # The point of all three paths is the same: never re-resolve every
        # chat's name and rebuild every chat's row text just to change one row,
        # which on an account with several hundred chats is seconds of work per
        # ack. Skipped entirely when the status isn't part of the preview, so
        # turning that setting off also turns off the work.
        if found_msg and self.settings.get("user_interface", {}).get(
            "show_delivery_status_in_chat_list", True
        ):
            if not self.move_chat_row_to_top(found_chat_jid):
                self._schedule_set_chats()

    def _presence_label_for_chat(
        self,
        chat_jid_norm: str,
        is_group: bool,
        *,
        resolve_missing: bool = True,
    ) -> str:
        """Return the typing/recording label to append to a chat-list row, or ''."""
        active = getattr(self, "_composing_chats", {}).get(chat_jid_norm, {})
        if not active:
            return ""
        participant_jid, action = next(iter(active.items()))
        if action == "composing":
            action_label = self.i18n.t("typing_indicator")
        elif action == "recording":
            action_label = self.i18n.t("recording_indicator")
        else:
            return ""
        if is_group:
            if resolve_missing:
                name = self._resolve_jid_name(participant_jid, chat_jid_norm)
            else:
                name = self._resolve_jid_name(
                    participant_jid,
                    chat_jid_norm,
                    resolve_missing=False,
                )
            if name:
                return self.i18n.t("group_presence_indicator").format(
                    name=name, action=action_label
                )
        return action_label

    def _presence_announcement_for_chat(self, chat: dict) -> str:
        """Build the text Alt+T speaks for `chat`: typing/recording if
        currently happening, else online/last-seen from the presence cache
        (populated live by on_presence_update() — subscribe_presence() is
        called whenever a conversation is opened), else "no info".

        Deliberately never makes a network call here (get_last_seen() can
        take up to 10s) — Alt+T must announce instantly from whatever
        WhatsApp has already pushed us, same as the chat-list typing label.
        """
        jid = chat.get("remoteJid", "")
        is_group = jid.endswith("@g.us")
        chat_jid_norm = self._normalize_jid(jid)
        if chat_jid_norm.endswith("@lid"):
            chat_jid_norm = getattr(self, "_lid_to_phone", {}).get(chat_jid_norm, chat_jid_norm)

        label = self._presence_label_for_chat(chat_jid_norm, is_group)
        if label:
            return label

        if is_group:
            return self.i18n.t("presence_unavailable")

        presence = getattr(self, "_presence_cache", {}).get(chat_jid_norm, {})
        lkp = presence.get("lastKnownPresence", "")
        if lkp == "available":
            return self.i18n.t("presence_online")

        last_seen = presence.get("lastSeen")
        if last_seen:
            try:
                from datetime import datetime as _dt
                ts = int(last_seen)
                if ts > 1_000_000_000_000:
                    ts //= 1000
                dt = _dt.fromtimestamp(ts)
                if dt.date() == _dt.now().date():
                    time_str = dt.strftime(get_time_format(self.i18n.t("time_fmt")))
                else:
                    time_str = dt.strftime(get_datetime_format(self.i18n.t("datetime_fmt")))
                return self.i18n.t("presence_last_seen").format(time=time_str)
            except Exception:
                pass

        return self.i18n.t("presence_unavailable")

    def _on_global_toggle_audio_playback(self, event):
        """Ctrl+Alt+Shift+P: pause/resume whichever voice or audio message is
        currently loaded in the player, from anywhere in the window —
        regardless of which conversation it belongs to or which one is open.
        Playback state (self._audio_stream etc.) lives on the single
        conversations_panel instance, not per-conversation, so this works
        even while looking at a different conversation than the one playing.
        """
        cp = getattr(self, "conversations_panel", None)
        if cp is not None:
            cp.toggle_current_audio_playback()

    def _on_global_alt_t(self, event):
        """Alt+T: announce the current/selected conversation's presence
        (typing/recording, online, or last seen) — works with the chat list
        focused (uses the selected row) or a conversation already open (uses
        that conversation), from anywhere in the window.
        """
        cp = getattr(self, "conversations_panel", None)
        if cp is None:
            return
        chat = cp.conversation
        if chat is None and hasattr(cp, "_selected_chat_from_list"):
            chat = cp._selected_chat_from_list()
        if not chat:
            return
        self.output(self._presence_announcement_for_chat(chat))

    def _refresh_chat_row_in_list(self, chat_jid_norm: str):
        """Update only the chat-list row for chat_jid_norm via SetItem(), in
        whichever panel (main or archived) currently displays it.

        Replaces the full _schedule_set_chats() rebuild for changes that don't
        affect list membership/order (presence, unread count going to 0).
        SetItem() on a single row prevents NVDA from re-reading the entire
        list, and applies immediately instead of waiting out the 300ms
        _schedule_set_chats() debounce.
        """
        # Title/tray tooltip unread count: cheap (one pass over self.chats),
        # so recompute it here directly rather than waiting for the next
        # debounced _apply_chat_lists() rebuild.
        self._update_title()

        is_group = chat_jid_norm.endswith("@g.us")
        for panel in (
            getattr(self, "conversations_panel", None),
            getattr(self, "archived_conversations_panel", None),
        ):
            if panel is None:
                continue
            lst       = getattr(panel, "conversations_list", None)
            displayed = getattr(panel, "chats_list", [])
            names     = getattr(panel, "chat_names", [])
            if lst is None:
                continue
            for idx, chat in enumerate(displayed):
                if self._normalize_jid(chat.get("remoteJid", "")) != chat_jid_norm:
                    continue
                if idx >= lst.GetItemCount():
                    # displayed/chats_list (this panel's backing array) has
                    # drifted ahead of the ListCtrl's actual row count — e.g.
                    # a debounced full rebuild (_apply_chat_lists) is
                    # mid-flight on another callback and hasn't inserted this
                    # many rows yet. There's nothing to patch until that
                    # rebuild finishes and re-syncs both; the next presence/
                    # unread event will retry. Falls through to the crash
                    # this guard exists for otherwise ("invalid item index in
                    # SetItem").
                    break
                unread = effective_unread_count(chat)
                conv_filter = getattr(panel, '_conv_filter', 'all')
                if conv_filter == 'unread' and unread == 0:
                    # No longer belongs in the "unread" filtered view. Remove it
                    # outright (and keep the backing arrays in sync) immediately
                    # instead of waiting for the next debounced full rebuild —
                    # this used to be the only way such a row ever disappeared,
                    # and letting several of these pile up made the backing
                    # arrays' indices drift from the ListCtrl's real item count
                    # (the "Couldn't retrieve information about list control
                    # item N" crashes).
                    try:
                        lst.DeleteItem(idx)
                    except Exception:
                        break
                    del displayed[idx]
                    if idx < len(names):
                        del names[idx]
                    displayed_jids = getattr(panel, '_displayed_jids', None)
                    if displayed_jids is not None and idx < len(displayed_jids):
                        del displayed_jids[idx]
                    break
                name   = names[idx] if idx < len(names) else ""
                unread_str = (
                    f" {unread} " + (
                        self.i18n.t("unread_messages") if unread > 1
                        else self.i18n.t("unread_message")
                    )
                    if unread > 0 else ""
                )
                preview   = self._last_msg_preview(chat)
                item_text = name + unread_str
                if preview:
                    item_text += f" {preview}"
                label = self._presence_label_for_chat(chat_jid_norm, is_group)
                if label:
                    item_text += f" {label}"
                # Mirrors add_chats_to_ui()'s _build_item_text() — without
                # these, a single-row refresh (presence/unread changes, which
                # fire far more often than a full rebuild) silently dropped
                # the pinned/muted/blocked suffix from a row until the next
                # full rebuild happened to run.
                if self.is_chat_pinned(chat_jid_norm):
                    item_text += f" ({self.i18n.t('pinned_suffix')})"
                if self.is_chat_muted(chat_jid_norm):
                    item_text += f" ({self.i18n.t('muted')})"
                if self.is_contact_blocked(chat_jid_norm):
                    item_text += f" ({self.i18n.t('blocked')})"
                # Only touch the row when the visible text actually changes. Presence
                # bursts (online/offline toggles that don't alter the row) otherwise
                # rewrote the focused item's text repeatedly, making NVDA announce the
                # conversation name over and over while the user sat idle on the list.
                try:
                    if lst.GetItemText(idx, 0) != item_text:
                        lst.SetItem(idx, 0, item_text)
                except Exception:
                    # GetItemText/SetItem failing here almost always means idx
                    # is no longer valid for this ListCtrl (see the item-count
                    # guard above) — unconditionally retrying SetItem with the
                    # same idx just raised the exact same wx assertion again,
                    # uncaught this time, crashing the app instead of no-op'ing.
                    pass
                break

    def on_presence_update(self, jid: str, presences: dict):
        """Update the presence cache and composing indicators. Speaks changes
        via AO2 when the active conversation has a new composing event, and refreshes
        the data-button note for the open conversation.

        presences: {jid_str: {"lastKnownPresence": str, "lastSeen": int|None}, ...}
        """
        logging.info("[on_presence_update] jid=%s, presences=%s", jid, presences)

        if not jid or not isinstance(presences, dict):
            return

        chat_jid_norm = self._normalize_jid(jid)
        if chat_jid_norm.endswith("@lid"):
            chat_jid_norm = self._lid_to_phone.get(chat_jid_norm, chat_jid_norm)

        composing_chats = getattr(self, "_composing_chats", None)
        if composing_chats is None:
            self._composing_chats = {}
            composing_chats = self._composing_chats

        # Determine the open conversation JID (may be None)
        panel     = getattr(self, "conversations_panel", None)
        conv      = getattr(panel, "conversation", None) if panel else None
        conv_jid  = ""
        if conv is not None:
            conv_jid = self._normalize_jid(conv.get("remoteJid", ""))
            if conv_jid.endswith("@lid"):
                conv_jid = self._lid_to_phone.get(conv_jid, conv_jid)

        presence_changed = False

        # Check if this presence event belongs to the currently active conversation
        def is_active_chat(cjid, open_jid):
            if not open_jid:
                return False
            if cjid == open_jid:
                return True
            p1 = self._lid_to_phone.get(cjid, cjid)
            p2 = self._lid_to_phone.get(open_jid, open_jid)
            if p1 == p2:
                return True
            l1 = self._phone_to_lid.get(cjid, cjid)
            l2 = self._phone_to_lid.get(open_jid, open_jid)
            if l1 == l2:
                return True
            return False

        _ppm_updated = False
        for participant_jid, data in presences.items():
            if not isinstance(data, dict):
                continue
            canonical = self._normalize_jid(participant_jid)
            if canonical.endswith("@lid"):
                canonical = self._lid_to_phone.get(canonical, canonical)

            # ── Persist pushName learned from presence so @lid contacts show
            # the correct name even before they appear in _lid_to_phone. ──────
            if canonical.endswith("@s.whatsapp.net"):
                contact_entry = self.contacts.get(canonical)
                if contact_entry:
                    push = (contact_entry.get("pushName") or "").strip()
                    if push and not push.isdigit() and not is_phone_like(push):
                        if self._presence_pushname_map.get(canonical) != push:
                            self._presence_pushname_map[canonical] = push
                            _ppm_updated = True
                        # Also index the corresponding @lid if known, so callers
                        # can look up by lid_jid directly without bridging.
                        lid = getattr(self, "_phone_to_lid", {}).get(canonical, "")
                        if lid and self._presence_pushname_map.get(lid) != push:
                            self._presence_pushname_map[lid] = push
                            _ppm_updated = True

            old_lkp = self._presence_cache.get(canonical, {}).get("lastKnownPresence", "")
            new_lkp = data.get("lastKnownPresence", "unavailable")

            self._presence_cache[canonical] = {
                "lastKnownPresence": new_lkp,
                "lastSeen": data.get("lastSeen"),
            }

            if new_lkp != old_lkp:
                presence_changed = True

            # Update composing/recording index for this chat
            if chat_jid_norm not in composing_chats:
                composing_chats[chat_jid_norm] = {}
            timer_key = (chat_jid_norm, canonical)
            if new_lkp in ("composing", "recording"):
                composing_chats[chat_jid_norm][canonical] = new_lkp
                # Reset the 10-second auto-clear timer on every new event
                old_timer = self._presence_timers.pop(timer_key, None)
                if old_timer is not None:
                    try:
                        old_timer.Stop()
                    except Exception:
                        pass
                def _make_clear(cjid, part):
                    def _clear():
                        self._composing_chats.get(cjid, {}).pop(part, None)
                        self._presence_timers.pop((cjid, part), None)
                        self._refresh_chat_row_in_list(cjid)
                    return _clear
                self._presence_timers[timer_key] = wx.CallLater(
                    10_000, _make_clear(chat_jid_norm, canonical)
                )
            else:
                composing_chats[chat_jid_norm].pop(canonical, None)
                old_timer = self._presence_timers.pop(timer_key, None)
                if old_timer is not None:
                    try:
                        old_timer.Stop()
                    except Exception:
                        pass

            # Speak via AO2 only when a composing/recording event starts in the ACTIVE conversation.
            # Events from other chats are intentionally silent to avoid interrupting the user.
            if new_lkp != old_lkp and new_lkp in ("composing", "recording"):
                speech = self.settings.get("speech_content", {})
                announce_enabled = (
                    speech.get("announce_typing", True) if new_lkp == "composing"
                    else speech.get("announce_recording", True)
                )
                active_match = is_active_chat(chat_jid_norm, conv_jid)
                # Typing/recording indicators are only meaningful while the user
                # is actually looking at WinZapp — a conversation left open when
                # the window was minimized to the tray must not keep announcing.
                window_active = (
                    not getattr(self, "_window_hidden", False)
                    and self.IsShown()
                    and not self.IsIconized()
                    and self.IsActive()
                )
                logging.info("[on_presence_update] announce_enabled=%s, is_active_chat=%s, window_active=%s (chat_jid_norm=%s, conv_jid=%s)",
                             announce_enabled, active_match, window_active, chat_jid_norm, conv_jid)
                if announce_enabled and active_match and window_active:
                    # Mute/archive suppress background notifications, not the
                    # live state of a conversation the user deliberately has
                    # open. The active-chat and active-window gates above are
                    # the relevant boundary for typing/recording speech.
                    name = self._resolve_jid_name(canonical, chat_jid_norm)
                    logging.info("[on_presence_update] resolved name=%s for canonical=%s", name, canonical)
                    if name:
                        try:
                            i18n_key = "typing_text" if new_lkp == "composing" else "recording_text"
                            msg_text = self.i18n.t(i18n_key).format(name=name)
                            logging.info("[on_presence_update] speaking: %s", msg_text)
                            self.speak_output.output(msg_text)
                        except Exception as e:
                            logging.error("[on_presence_update] speak error: %s", e)

        # Persist the updated pushName map to database metadata.
        if _ppm_updated and hasattr(self, "db") and self.db is not None:
            try:
                self.db.set_metadata_json("presence_pushname_map", dict(self._presence_pushname_map))
            except Exception as db_err:
                logging.warning("[on_presence_update] Failed to save presence_pushname_map: %s", db_err)

        # Update only the affected row — avoids DeleteAllItems()+Append() rebuild
        # that causes NVDA to re-read the full list and stutter during TTS echo.
        if presence_changed:
            self._refresh_chat_row_in_list(chat_jid_norm)

        # Refresh the data-button note for the open conversation
        if panel is None or conv is None:
            return
        if conv_jid in self._presence_cache:
            panel._refresh_presence_note(conv_jid)

    @staticmethod
    def _remote_read_confirmed(unread_count: int, previous_unread: int | None) -> bool:
        """True when a chats-update's zero is a real read somewhere else.

        WhatsApp Web reports unreadCount=0 for two completely different
        things, and for a long time WinZapp could not tell them apart:

        1. The user read the chat on their phone or another linked device —
           the count genuinely fell from N to 0.
        2. The chat was merely loaded into WhatsApp Web's own Store, which
           announces 0 before the real value is populated. Nothing was read;
           the event carries no information whatsoever.

        Treating (2) as (1) is what erased locally counted arrivals and left
        every notification announcing "1 mensagem não lida". Treating (1) as
        (2) leaves a badge lit for messages the user already read elsewhere.
        The difference is whether the count actually *fell*, which the page
        knows and now forwards as previousUnreadCount.

        Unknown (None) is deliberately read as "not confirmed": that is the
        conservative side — the badge survives until the next full sync
        reconciles it, rather than unread messages being silently dropped.

        All three shapes were observed on a live session, on group chats —
        which is where this was reported, and which the code used to claim
        could not happen at all:

            unreadCount=1/2/3, previous=0/1/2   messages arriving
            unreadCount=0,     previous=0       the Store load, means nothing
            unreadCount=0,     previous=2       a chat opened on the phone
        """
        if unread_count != 0:
            return False
        return isinstance(previous_unread, int) and previous_unread > 0

    def _resolve_chat_for_event(self, jid: str):
        """Find the stored chat a chats-update event refers to.

        Returns ``(key, chat)`` — the key self.chats actually holds the chat
        under — or ``(normalized, None)`` when nothing matches.

        WhatsApp emits these events keyed by whichever identity its own Store
        holds the chat under, which on @lid-enabled accounts is the linked
        device id and not the phone JID WinZapp normalizes everything else to
        (_normalize_jid deliberately leaves @lid alone). Looking the event up
        by that JID alone therefore found nothing and dropped it in silence —
        a chat read on the phone kept its badge lit with nothing in the log
        to say why. on_chat_pin_update() already bridged the two identities
        inline for exactly this reason; the unread and archive handlers never
        did, so the same event reached one handler and was thrown away by the
        other two.
        """
        normalized = self._normalize_jid(jid)
        chat = self.chats.get(normalized)
        if chat is not None:
            return normalized, chat
        if normalized.endswith("@lid"):
            alt = getattr(self, "_lid_to_phone", {}).get(normalized, "")
        else:
            alt = getattr(self, "_phone_to_lid", {}).get(normalized, "")
        if alt:
            alt = self._normalize_jid(alt)
            chat = self.chats.get(alt)
            if chat is not None:
                return alt, chat
        return normalized, None

    def on_chat_unread_update(self, jid: str, unread_count: int, previous_unread: int | None = None):
        """Handle unread-count change from chats.update (e.g. read on another device).

        *previous_unread* is what WhatsApp Web's own chat model held before
        this change, when the page could tell us (None = unknown). A zero
        that fell from a positive previous count is a genuine read somewhere
        else — the phone, another linked device — and must clear the badge.
        A zero whose previous count was also zero is the chat simply being
        loaded into the Store with nothing behind it, which is the event that
        used to wipe locally counted arrivals; see _remote_read_confirmed().
        """
        # This whole path used to be silent: four separate `return`s, no
        # logging anywhere, and no way to tell from log.log whether a read
        # made on the phone had reached WinZapp at all, been thrown away by
        # one of the guards, or never been emitted by WhatsApp Web in the
        # first place. Every exit now says which one it was — the events are
        # rare enough (a handful per read) that this costs nothing.
        normalized, chat = self._resolve_chat_for_event(jid)
        if chat is None:
            logging.info(
                "[unread] %s -> %s: dropped, no such chat (unread=%s, previous=%s).",
                jid, normalized, unread_count, previous_unread,
            )
            return
        # During the initial sync the WPPConnect handshake can emit
        # chats-update with unreadCount=0 BEFORE get_remote_chats() has
        # fetched the real list — accepting that would wipe the locally
        # stored (never-read) badge and persist the 0 to the DB, so every
        # conversation the user hasn't actually read shows as read right
        # after opening the app. The list-chats merge in get_remote_chats()
        # is the authoritative source for the real counts; ignore live
        # chats.update while it (or the initial sync) is still running.
        # Both halves are load-bearing and neither implies the other. F5's
        # _run_sync() sets _initial_sync_running before it waits on ui_ready
        # (up to 5s) and only clears _sync_completed after that wait returns —
        # so for that window a resync is demonstrably in flight while
        # _sync_completed is still True, and a chats.update arriving there would
        # be accepted mid-resync, which is exactly what this gate forbids.
        if getattr(self, "_initial_sync_running", False) or not getattr(self, "_sync_completed", False):
            logging.info(
                "[unread] %s: dropped, sync gate (running=%s, completed=%s, "
                "unread=%s, previous=%s).",
                normalized, getattr(self, "_initial_sync_running", False),
                getattr(self, "_sync_completed", False), unread_count, previous_unread,
            )
            return
        old_count = int(chat.get("unreadCount") or 0)
        if old_count == unread_count:
            # Logged like every other exit. This one used to be silent, and it
            # is the one that makes a report impossible to diagnose: the event
            # shows up in the socket log and then the trail simply stops, with
            # no way to tell "the badge already agreed with the server" apart
            # from "the handler never ran at all" (a stalled UI thread, a chat
            # that failed to resolve). Seen live: a stretch of a session where
            # every chats-update reached websocket_client's own log and then
            # produced nothing at all here — this exit is the only one that
            # could have swallowed them, but nothing written down proved it.
            logging.info(
                "[unread] %s: no change (already %s, previous=%s).",
                normalized, unread_count, previous_unread,
            )
            return  # no actual change — skip expensive rebuild + save
        # The server sometimes counts own (fromMe) messages — and system events
        # (group promotes, joins/leaves, revokes) — as unread. Correct for both
        # by inspecting the tail of the locally-stored message list.
        records = (
            (chat.get("messages") or {})
            .get("messages", {})
            .get("records", [])
        )
        if unread_count > 0 and records:
            unread_count = _discount_non_countable_unread(records, unread_count)
        # A zero has nothing to discount, so it counts as final either way.
        discounted = bool(records) or unread_count == 0
        old_count = int(chat.get("unreadCount") or 0)
        if old_count == unread_count:
            logging.info(
                "[unread] %s: no change after discounting non-countable "
                "messages (already %s, previous=%s).",
                normalized, unread_count, previous_unread,
            )
            return
        # Never resurrect unread count for a conversation the user already read
        # locally (mark_conversation_as_read set it to 0). The server may still
        # carry a stale unread count from before the read-ack arrived.
        # NOTE: _last_open_jid lives on ConversationsPanel, not MainWindow —
        # this used to read `self._last_open_jid` directly, which never
        # existed on MainWindow and so always fell back to "", silently
        # disabling this guard entirely.
        cp = getattr(self, "conversations_panel", None)
        # Use the *real* open state (the conversation panel actually showing
        # this jid) instead of _last_open_jid: that field is never cleared on
        # close, so a chat the user already left kept being treated as "open"
        # forever and any chats-update for it was force-zeroed.
        # ...and only while it is also READ. The open branch below treats the
        # panel showing a chat as proof the user has read it, which stops
        # being true the moment a read is deliberately undone with the
        # conversation still on screen. Two reachable paths do exactly that:
        # Ctrl+Shift+M on the open chat (_on_accel_toggle_read ->
        # mark_conversation_as_unread), and _restore_unread_after_send_seen_
        # failure() putting a backlog back after WhatsApp refused every
        # /send-seen attempt. Both leave _new_since_read at 0, and
        # reconcile_open_chat_unread() answers 0 for that — so the next
        # chats-update (or the 60s resync at the latest) silently erased the
        # unread state the user had just asked for, backlog and all. The
        # anchor is what separates "open" from "read": without it this falls
        # through to the ordinary closed-chat branches, which already refuse
        # a server count below the local one and accept an honest higher one.
        _open_now = (
            cp is not None
            and cp.conversation is not None
            and cp.conversation.get("remoteJid") == normalized
            and self._unread_anchored_to_local_read(normalized)
        )
        read_at_t = getattr(self, "_locally_read_at", {}).get(normalized)
        # A zero that fell from a positive count is somebody actually reading
        # the chat elsewhere; a zero with no such drop behind it carries no
        # information at all (see _remote_read_confirmed).
        _remote_read = self._remote_read_confirmed(unread_count, previous_unread)
        if _open_now:
            # Chat is genuinely open. Keep a nonzero count only for messages
            # that arrived while the window was hidden/minimized (tracked in
            # _new_since_read); otherwise the open conversation is read.
            #
            # "Open" outlives the window: closing with Alt+F4 only hides to
            # tray (see _on_close), so a conversation left open stays open
            # with the window gone — which is precisely when this branch has
            # to survive the spurious WA-JS zeros described in the branch
            # below. A plain min() against those collapsed the count to 0
            # after every message, so the next arrival counted up from zero
            # again and its toast forever announced "1 mensagem não lida"
            # while the conversation really held several. The `elif` guard
            # below is unreachable here (this branch already matched), so the
            # same protection has to be spelled out on this side too.
            local_new = getattr(self, "_new_since_read", {}).get(normalized, 0)
            unread_count, _clear_new = reconcile_open_chat_unread(
                unread_count, local_new, remote_read_confirmed=_remote_read
            )
            if _clear_new:
                self._new_since_read.pop(normalized, None)
        elif read_at_t is None and unread_count < old_count and not _remote_read:
            # WA-JS fires chat.unread_count_changed (forwarded to us as
            # chats-update by createSessionUtil.ts) when a 1:1 chat is loaded
            # into the browser Store — often with unreadCount=0 BEFORE the real
            # value is populated — and those events keep arriving for a while
            # AFTER the initial sync finished, so the _initial_sync_running
            # guard above has already been passed. A chat never read locally
            # this session and not currently open must never have its locally
            # counted unread reduced by such a server event; the local counter
            # (incremented by on_new_message or set by get_remote_chats) is the
            # authoritative source.
            #
            # This used to claim groups never hit this path because the WA-JS
            # event was 1:1 only. That is false, and measured to be false:
            # listening on the live Socket.IO stream shows @g.us chats
            # emitting chats-update both with a climbing count and with a bare
            # unreadCount=0, exactly like private chats. The claim mattered —
            # it is why a report of this bug happening in groups looked
            # inconsistent with the code.
            #
            # This guard used to swallow real reads too, because a load-time
            # zero and a phone read looked identical from here: reading a chat
            # on the phone left its badge lit in WinZapp until some later sync
            # happened to correct it. With previousUnreadCount telling the two
            # apart, only the uninformative kind is rejected.
            logging.info(
                # Named for what the branch tests, not for the case that
                # motivated it. The condition is `unread_count < old_count`
                # with no confirmed remote read — it rejects any count that
                # falls below the local one, and the load-time zero is only the
                # commonest shape of that. Calling every one of them a "server
                # zero" sent the reader looking for a zero that is not there:
                # seen live as `dropped, uninformative server zero (32 -> 8,
                # previous=7)`, where nothing was zero and the guard was right
                # anyway (WhatsApp Web's store was under-counting by 24).
                "[unread] %s: dropped, server count below local and no "
                "confirmed remote read (%s -> %s, previous=%s).",
                normalized, old_count, unread_count, previous_unread,
            )
            return
        elif (
            read_at_t is None
            and unread_count > old_count
            and not _remote_read
            and getattr(self, "_new_since_read", {}).get(normalized)
            and self._unread_anchored_to_local_read(normalized)
        ):
            # Once the read_at_t entry that protected a chat has been
            # consumed and popped (see the `elif read_at_t is not None`
            # branch below, which pops it after its own clamp), a LATER
            # chats-update for the same chat can still arrive reporting a
            # higher unreadCount than what was actually read locally —
            # WhatsApp Web's own total, which that same branch already
            # documents as sometimes still counting messages already read
            # locally but not yet acknowledged by the server. Without this
            # guard that inflated total was accepted verbatim (there is no
            # other elif left to catch it), which is what pulled an
            # already-read message back into "unread" (first_unread_index()
            # draws the separator by counting backwards from unreadCount, so
            # a count too high by N drags N already-read messages along with
            # it).
            #
            # _new_since_read counts arrivals since the last local read —
            # but ONLY for a chat that has actually had one. on_new_message()
            # creates the entry from nothing for any chat that receives a
            # message, so in a chat never read here it counts arrivals since
            # the process started, and clamping an absolute total to it
            # throws away every unread message that predates this launch.
            # Measured on a live session, in the four busiest groups on the
            # account: `34876 -> 21`, `7232 -> 3`, `3366 -> 2`, `2767 -> 6`,
            # each one restored to its real value by the next 60s resync and
            # collapsed again by the chats-update a second later — the badge
            # visibly flipping between 34 thousand and 21 for as long as the
            # app stayed open. _unread_anchored_to_local_read() is what tells
            # the two kinds of entry apart: without a local read behind it
            # there is no locally verified total to clamp to, and the
            # server's own count (already discounted above) is the best
            # answer available.
            unread_count = min(unread_count, self._new_since_read[normalized])
            logging.info(
                "[unread] %s: %s -> %s (previous=%s, open=%s, read_ack=%s).",
                normalized, old_count, unread_count, previous_unread, _open_now, read_at_t,
            )
            chat["unreadCount"] = unread_count
            note_unread_discount_state(chat, discounted)
            self._schedule_save(dirty_jid=normalized)
            self._schedule_set_chats()
            return
        elif read_at_t is not None:
            incoming_t = int(chat.get("t", 0) or 0)
            if incoming_t <= read_at_t:
                unread_count = 0
            else:
                # A genuinely new message arrived after the local read-ack.
                # The server reports an ABSOLUTE total that can still be
                # counting messages already read locally, so it is clamped
                # DOWN to what we counted ourselves since the read — min(),
                # not max(). Flipping this to max() reinstated the exact bug
                # this whole path exists to prevent: one new message with a
                # stale server total of 4 showed a badge of 4 instead of 1.
                #
                # The zero case is handled first rather than in an elif, so it
                # survives whether or not local tracking has an entry: a
                # server-sent 0 must never wipe a badge for messages that
                # arrived after the read-ack (1:1 WA-JS
                # chat.unread_count_changed does exactly that), and with
                # local_new set the old elif could never be reached at all.
                local_new = getattr(self, "_new_since_read", {}).get(normalized)
                if unread_count == 0 and _remote_read:
                    # A read confirmed on another device — the count really
                    # fell from something (see _remote_read_confirmed) — covers
                    # the WHOLE chat, including the messages that arrived after
                    # our local read-ack. That is exactly what separates it
                    # from the uninformative WA-JS load-time zero the branch
                    # below defends against, and it has to be checked first or
                    # the defense swallows the real read.
                    #
                    # Reported live: an audio arrived after the conversation
                    # had been read locally, was then read on the phone, and
                    # the badge stayed lit at "1 mensagem não lida" — WhatsApp
                    # Web sent unreadCount=0/previousUnreadCount=1 and this
                    # branch put the 1 straight back. The other two branches
                    # (open conversation, and the `unread_count < old_count`
                    # guard above) already consult _remote_read; this one was
                    # simply missed, which is why the bug showed up in 1:1
                    # chats while groups looked fixed.
                    unread_count = 0
                elif unread_count == 0 and old_count > 0:
                    unread_count = local_new or old_count
                elif local_new:
                    unread_count = min(unread_count, local_new)
                self._locally_read_at.pop(normalized, None)
                self._persist_locally_read_at()
                if hasattr(self, "_new_since_read"):
                    self._new_since_read.pop(normalized, None)
        logging.info(
            "[unread] %s: %s -> %s (previous=%s, open=%s, read_ack=%s).",
            normalized, old_count, unread_count, previous_unread, _open_now, read_at_t,
        )
        chat["unreadCount"] = unread_count
        note_unread_discount_state(chat, discounted)
        self._schedule_save(dirty_jid=normalized)
        self._schedule_set_chats()

    def on_chat_archive_update(self, jid: str, archived: bool):
        """Handle archive/unarchive status change from chats.update."""
        normalized, chat = self._resolve_chat_for_event(jid)
        if chat is None:
            return
        self._set_archived_state(normalized, archived)

    def on_chat_pin_update(self, jid: str, is_pinned: bool):
        """Handle pin/unpin status change from chats.update."""
        normalized = self._normalize_jid(jid)
        chat = self.chats.get(normalized)
        if chat is None:
            if normalized.endswith("@lid"):
                alt = getattr(self, "_lid_to_phone", {}).get(normalized, "")
                if alt: chat = self.chats.get(self._normalize_jid(alt))
            else:
                alt = getattr(self, "_phone_to_lid", {}).get(normalized, "")
                if alt: chat = self.chats.get(alt)

        if is_pinned:
            self._pinned_chats.add(normalized)
            if normalized.endswith("@lid"):
                alt_phone = getattr(self, "_lid_to_phone", {}).get(normalized, "")
                if alt_phone:
                    self._pinned_chats.add(self._normalize_jid(alt_phone))
            else:
                alt_lid = getattr(self, "_phone_to_lid", {}).get(normalized, "")
                if alt_lid:
                    self._pinned_chats.add(alt_lid)
        else:
            self._pinned_chats.discard(normalized)
            if normalized.endswith("@lid"):
                alt_phone = getattr(self, "_lid_to_phone", {}).get(normalized, "")
                if alt_phone:
                    self._pinned_chats.discard(self._normalize_jid(alt_phone))
            else:
                alt_lid = getattr(self, "_phone_to_lid", {}).get(normalized, "")
                if alt_lid:
                    self._pinned_chats.discard(alt_lid)

        if chat is not None:
            chat["pin"] = is_pinned

        if hasattr(self, "db") and self.db is not None:
            self.db.set_metadata_json("pinned_chats", list(self._pinned_chats))
        self._schedule_set_chats()
