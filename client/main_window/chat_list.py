"""ChatListMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import logging
import threading
import time
import wx
from core.call_log import (
    CALL_LOG_MESSAGE_TYPE,
    LEGACY_CALL_LOG_TYPE,
    call_log_label,
    is_call_log,
)
from core.utils import (
    append_selected_marker,
    effective_unread_count,
    format_number,
    is_phone_like,
    is_voice_message,
    looks_like_binary_blob,
    normalize_for_search,
    plan_row_updates,
)
from core.locale_format import (
    get_datetime_format,
    get_time_format,
)
from main_window.message_rules import is_countable_message


class ChatListMixin:
    """The chat list: navigation to a JID, computing and applying the chat lists,
    scheduled refreshes, last-message previews, archived list and row
    rendering.
    """

    # ── Navigate to conversation by JID ──────────────────────────────────────

    def navigate_to_conversation_jid(self, jid: str, name: str = ""):
        """Bring the window to front and open the conversation matching jid.

        Only calls restore_window() when the window is actually hidden; if it
        is already visible the caller (e.g. _do_open) has already restored it
        and a second SetForegroundWindow call would steal focus at an unexpected
        moment (e.g. the user has already moved to another app after clicking
        the toast).
        """
        if self._window_hidden:
            self.restore_window()
        if not hasattr(self, "conversations_panel"):
            return
        # The stored JID, not the one passed in: group participants arrive as
        # @c.us, device-suffixed or in the other Brazilian digit form, and
        # both the archived lookup and navigate_to_jid() compare raw strings
        # (an archived chat reached that way opened in the main list's layout).
        if jid.endswith(("@s.whatsapp.net", "@c.us", "@lid")):
            from ui.dialogs.new_conversation import NewConversationDialog
            _, existing = NewConversationDialog._find_existing_chat(self, jid)
            if existing is not None:
                jid = existing.get("remoteJid") or jid

        if getattr(self, "is_chat_locked", lambda _jid: False)(jid):
            if not self._chat_lock_unlocked:
                if not self.unlock_chat_lock_vault(show_panel=False):
                    return
            wanted = self._chat_lock_candidates(jid)
            chat = next((
                candidate for candidate in self.chats.values()
                if isinstance(candidate, dict)
                and (
                    candidate.get("remoteJid", "") in wanted
                    or bool(self._chat_lock_candidates(
                        candidate.get("remoteJid", "")
                    ) & wanted)
                )
            ), None)
            if chat is not None:
                self.open_locked_conversation(chat)
            return
        # Same bug on_alt_1() fixed for its own hotkey, reached from a
        # different entry point: a toast click (or the participant-list
        # dialog) can call this while Status or the Archived list is the
        # panel actually shown, and both navigate_to_jid()/
        # navigate_to_conversation() SetFocus()/Select() controls inside
        # conversations_panel regardless of whether it's visible — leaving
        # NVDA focus stuck on an invisible control that only Alt+1 could
        # recover. Make the correct top-level panel visible first.
        if self.is_chat_archived(jid) and hasattr(self, "archived_conversations_panel"):
            chat = None
            for candidate in self.archived_conversations_panel.chats_list:
                if candidate.get("remoteJid", "") == jid:
                    chat = candidate
                    break
            if chat is not None:
                # Mirrors ArchivedConversationsPanel.on_conversation_selected()'s
                # panel dance, plus hiding status_panel — that method never
                # needs to because it only runs while Status is already
                # hidden, but we can be called from there directly.
                self.archived_conversations_panel.Hide()
                if hasattr(self, "status_panel"):
                    self.status_panel.Hide()
                if hasattr(self, "calls_panel"):
                    self.calls_panel.Hide()
                self.conversations_panel.conversations_label.Hide()
                self.conversations_panel.conversations_list.Hide()
                self.conversations_panel.Show()
                self.content_panel.Layout()
                self.conversations_panel.navigate_to_conversation(chat)
                return
            # Stale archived state (or panel missing) — fall through to the
            # non-archived path below as a defensive fallback.
        if hasattr(self, "archived_conversations_panel"):
            self.archived_conversations_panel.Hide()
        if hasattr(self, "locked_conversations_panel"):
            self.locked_conversations_panel.Hide()
        if hasattr(self, "status_panel"):
            self.status_panel.Hide()
        if hasattr(self, "calls_panel"):
            self.calls_panel.Hide()
        self.conversations_panel.conversations_label.Show()
        self.conversations_panel.conversations_list.Show()
        self.conversations_panel.Show()
        self.content_panel.Layout()
        if self.conversations_panel.navigate_to_jid(jid):
            return
        # No row under this exact JID: a group participant the user never
        # talked to (or whose chat lives under an equivalent JID) used to
        # leave them in the group with nothing happening.
        chat = self._chat_for_private_conversation(jid, name)
        if chat is not None:
            self.conversations_panel.navigate_to_conversation(chat)

    def _chat_for_private_conversation(self, jid: str, name: str = ""):
        """The chat to open for a one-to-one conversation with *jid*.

        An existing chat under any equivalent JID (@c.us, the bridged @lid,
        the Brazilian 9th-digit variant) is reused, exactly as "Nova conversa"
        does. Otherwise a phone JID gets a new chat, registered the way "Nova
        conversa" registers a number nobody has talked to yet. An @lid with no
        chat and anything that is not a person get None: chats are keyed by
        the phone JID, and an unbridged @lid is not one.
        """
        from ui.dialogs.new_conversation import NewConversationDialog
        if not jid or not jid.endswith(("@s.whatsapp.net", "@c.us", "@lid")):
            return None
        norm_jid, existing = NewConversationDialog._find_existing_chat(self, jid)
        if existing is not None:
            return existing
        if not norm_jid.endswith("@s.whatsapp.net"):
            return None
        # A number-only participant still needs a name, or _compute_chat_lists()
        # drops the empty chat from the list and Escape leaves no row to come
        # back to; "Nova conversa" falls back to the formatted number too.
        if not name or self._is_bad_contact_name(name):
            name = format_number(norm_jid)
        chat = {"remoteJid": norm_jid, "pushName": name}
        self.chats[norm_jid] = chat
        self._schedule_set_chats()
        return chat

    def _compute_chat_lists(self):
        """Compute sorted/filtered chat lists. Safe to run on a background thread."""
        deleted  = self._deleted_chats
        archived = self._archived_chats
        pinned   = self._pinned_chats
        my_jid   = getattr(self, "my_jid", "")

        # Dedup: if both a @lid JID and its corresponding phone JID exist as
        # separate keys in self.chats, only render the one with more content
        # (prefer @lid since that's the active WPPConnect chat). Build a set of
        # phone JIDs that are already covered by a @lid entry so we can skip them.
        # Snapshot, not the live dict: this method runs on a background
        # thread while _extract_lid_mapping() can be adding pairs on the
        # Socket.IO one, and iterating the live dict raises "dictionary
        # changed size during iteration". dict() copies in C under the GIL,
        # so no lock is needed for a plain copy.
        lid_to_phone = dict(getattr(self, "_lid_to_phone", {}))
        phone_to_lid = getattr(self, "_phone_to_lid", {})
        _covered_by_lid: set[str] = set()
        for lid_jid, phone_jid in lid_to_phone.items():
            if lid_jid in self.chats and phone_jid in self.chats:
                # Both exist — keep the one with more messages (usually lid).
                lid_msgs = len(self.chats[lid_jid].get("messages", {}).get("messages", {}).get("records", []))
                phone_msgs = len(self.chats[phone_jid].get("messages", {}).get("messages", {}).get("records", []))
                if lid_msgs >= phone_msgs:
                    _covered_by_lid.add(phone_jid)
                else:
                    _covered_by_lid.add(lid_jid)

        main_chats, main_names = [], []
        arch_chats, arch_names = [], []
        locked_chats, locked_names = [], []

        # Every row the UI renders is identified by ``chat["remoteJid"]``, not by
        # the ``self.chats`` key it was stored under — and the two are NOT always
        # the same string (a chat merged/renamed in place keeps the dict it had,
        # and `_merge_lid_into_phone`/`deduplicate_chats` rewrite one of them).
        # Two keys resolving to the same remoteJid therefore produced two
        # identical-looking rows, and because every lookup downstream
        # (`_displayed_jids`, focus restore, `on_conversation_selected`) matches
        # on remoteJid, activating either row could open whichever one came
        # first — reported live as archived groups appearing twice and opening
        # a different group than the one announced. Render each remoteJid once.
        _seen_render_jids: set[str] = set()

        for jid, chat in list(self.chats.items()):
            if jid in deleted:
                continue
            if jid in _covered_by_lid:
                continue  # duplicate – already shown via the other JID
            render_jid = chat.get("remoteJid") or jid
            if render_jid in _seen_render_jids:
                continue
            _seen_render_jids.add(render_jid)

    
            records_wrapper = chat.get("messages") or {}
            records = []
            if isinstance(records_wrapper, dict):
                inner_wrapper = records_wrapper.get("messages") or {}
                if isinstance(inner_wrapper, dict):
                    records = inner_wrapper.get("records") or []
            last_msg  = chat.get("lastMessage")
            unread    = int(chat.get("unreadCount", 0) or 0)
            is_pinned = jid in pinned

            # Old versions persisted phantom one-to-one chats whose only record
            # is WhatsApp's encryption-key maintenance notification. Hide those
            # records too, so upgrading fixes existing databases without deleting
            # any real conversation or user data.
            technical_types = {"e2e_notification", "notification"}
            only_technical_records = bool(records) and all(
                isinstance(record, dict)
                and (record.get("messageType") or record.get("type")) in technical_types
                and record.get("subtype") in ("encrypt", None, "")
                and not record.get("body")
                for record in records
            )
            if (
                not jid.endswith("@g.us")
                and only_technical_records
                and not chat.get("t")
                and unread <= 0
                and not is_pinned
            ):
                continue

            # Skip chats with absolutely no content AND no identity.
            # We do NOT skip based on missing messages alone: during and just
            # after sync many valid chats have empty records but still carry a
            # name/pushName from the WPPConnect list-chats response.
            # A cleared conversation is *supposed* to be empty: it must stay in
            # the list (with no preview) instead of vanishing as if deleted.
            is_cleared   = jid in self.settings.get("cleared_chats", {})
            has_content  = bool(records or last_msg or unread > 0 or is_pinned or is_cleared)

            is_group = jid.endswith("@g.us")
            resolved_name = ""
            msg_push = ""
            # Resolve the contact name BEFORE deciding whether to drop the chat.
            # This used to happen ~30 lines below the skip, so a chat whose name
            # was perfectly resolvable through self.contacts was still judged
            # "no identity" on the raw dict alone and dropped before the lookup
            # ever ran.  On a real account that silently hid 218 of 539
            # conversations — every individual chat that WhatsApp Web had not
            # yet loaded messages for (list-chats returns `msgs: null`, so
            # lastMessage is empty for all of them) and that had no unread
            # count, even though all 263 had a matching contact record.
            if not is_group:
                resolved_name = self._resolve_contact_name(chat)

            name_hint    = (chat.get("name") or chat.get("pushName") or resolved_name or
                            self._group_name_from_chat_dict(chat)).strip()
            has_identity = bool(name_hint and not name_hint.isdigit() and len(name_hint) > 1)
            if not has_content and not has_identity:
                continue

            def get_valid_name(val):
                return "" if self._is_bad_contact_name(val) else val.strip()

            if jid.endswith("@lid"):
                phone_jid = getattr(self, "_lid_to_phone", {}).get(jid) or self._find_alt_jid_from_messages(chat)
            else:
                phone_jid = jid

            if is_group:
                # A group's real name may only be under groupMetadata.subject
                # in the raw chat dict — see _group_name_from_chat_dict().
                name = get_valid_name(self._group_name_from_chat_dict(chat))
                if not name:
                    cached = getattr(self, "_group_name_cache", {}).get(jid, "")
                    if cached:
                        name = cached
                    else:
                        fetched = self._fill_group_name(jid)
                        if fetched:
                            chat["name"] = fetched
                            name = fetched
            else:
                # Chat individual: resolved_name já foi calculado acima, antes
                # do descarte — reaproveitado aqui em vez de resolver de novo.
                chat_push = get_valid_name(chat.get("pushName", ""))
                name = resolved_name or chat_push
                if not name:
                    msg_push = self.find_name_through_messages(chat)
                    name = msg_push or get_valid_name(chat.get("name", ""))
            
            # Treat placeholders as empty to trigger phone number fallback
            if name and (self._is_bad_contact_name(name) or name == self.i18n.t("unknown_contact")):
                name = ""

            if not name or not name.strip():
                if jid.endswith("@g.us"):
                    name = self.i18n.t("unknown_group")
                else:
                    if phone_jid and not phone_jid.endswith("@lid"):
                        name = format_number(phone_jid)
                    else:
                        msg_jid_num = self.find_jid_through_messages(chat)
                        if msg_jid_num:
                            name = msg_jid_num
                        elif self._format_jid_for_display(jid):
                            name = self._format_jid_for_display(jid)
                        else:
                            # Let's check contact cache and chat metadata for a fallback (like masked number +55∙∙∙∙∙∙∙∙12)
                            c_obj = self.contacts.get(jid) or {}
                            fallback = c_obj.get("formattedName") or c_obj.get("pushName") or chat.get("formattedName") or chat.get("pushName") or ""
                            fallback_clean = fallback.strip()
                            if fallback_clean and "sem nome" not in fallback_clean.lower() and fallback_clean != self.i18n.t("unknown_contact"):
                                name = fallback_clean
                            elif jid.endswith("@lid"):
                                name = self.i18n.t("unknown_contact")
                            else:
                                numeric = jid.split("@")[0].split(":")[0]
                                if numeric.isdigit():
                                    name = format_number(numeric)
                                else:
                                    name = self.i18n.t("unknown_contact")
            
            # Name-resolution tracing. DEBUG, not INFO, and with lazy %-args
            # rather than an f-string, because both halves of that mattered:
            # this runs once per chat inside _compute_chat_lists(), which the
            # chat list rebuilds on every mapping learned. Measured on a live
            # 935-chat sync: 336,741 of the session's 348,332 log lines came
            # from this one call — 97% of the log, 74 MB of the 77 MB, roughly
            # 690 synchronous disk writes a second sustained for eight
            # minutes. An f-string would still be built on every call even
            # with the level raised, so the format arguments stay deferred.
            if jid.endswith("@lid") or name == self.i18n.t("unknown_contact"):
                logging.debug(
                    "[Name Resolution] jid=%s phone_jid=%s resolved_name=%s "
                    "msg_name=%s chat_name=%s push_name=%s -> final_name=%r",
                    jid, phone_jid, resolved_name, msg_push,
                    chat.get("name"), chat.get("pushName"), name,
                )
            if my_jid and not jid.endswith("@g.us") and self._is_self_jid(jid):
                name = self.i18n.t("self_chat_name")
            if self.is_chat_locked(render_jid) or self.is_chat_locked(jid):
                locked_chats.append(chat)
                locked_names.append(name)
                continue

            # Same parse is_chat_archived() uses — the two must answer this
            # identically, or a chat sits under Arquivadas while the
            # notification path believes it is a normal conversation and
            # announces its messages out loud.
            arch_flag = self._chat_archive_flag(chat)
            # An explicit flag on the chat record (server truth) wins over the
            # persisted set; the set only decides when the record says nothing.
            is_archived = arch_flag if arch_flag is not None else (jid in archived)
            if is_archived:
                arch_chats.append(chat)
                arch_names.append(name)
            else:
                main_chats.append(chat)
                main_names.append(name)

        # Pinned chats float to the top; within each group sort by most-recent
        # message timestamp descending (newest first), then alphabetically.
        #
        # Only counts is_countable_message() records — a system event (group
        # join/leave, settings change, revoke, ...) stored in this chat's
        # records must never push it back to the top of the list just
        # because its timestamp is the newest one on file. chat["t"]/
        # lastMessage are already never set from a non-countable message
        # (see on_new_message()/on_historical_message()), but this also
        # scans every raw record directly, so it needs the same filter.
        _chat_last_ts = self._chat_last_ts

        def _sort_key(pair):
            c, n = pair
            return self._chat_sort_key(c, n, pinned)

        pairs = sorted(zip(main_chats, main_names), key=_sort_key)
        main_chats = [c for c, _ in pairs]
        main_names = [n for _, n in pairs]

        arch_pairs = sorted(zip(arch_chats, arch_names), key=_sort_key)
        arch_chats = [c for c, _ in arch_pairs]
        arch_names = [n for _, n in arch_pairs]

        locked_pairs = sorted(zip(locked_chats, locked_names), key=_sort_key)
        locked_chats = [c for c, _ in locked_pairs]
        locked_names = [n for _, n in locked_pairs]

        return (
            main_chats, main_names,
            arch_chats, arch_names,
            locked_chats, locked_names,
        )

    def _apply_chat_lists(
        self, main_chats, main_names, arch_chats, arch_names,
        locked_chats, locked_names,
    ):
        """Apply sorted chat lists to panels and refresh UI. Must run on main thread."""
        if not hasattr(self, "conversations_panel"):
            return  # UI not yet initialized; skip silently
        self.chat_names = main_names

        # Save focused JIDs from the CURRENT (old) displayed lists BEFORE
        # overwriting chats_list.  add_chats_to_ui() maps focused_idx (from
        # the live ListCtrl) against chats_list to recover the JID — but
        # chats_list is about to be replaced with a reordered copy, so
        # focused_idx would point to the wrong chat after the assignment.
        _panel = self.conversations_panel
        _fi = _panel.conversations_list.GetFocusedItem()
        _panel._preserved_focused_jid = (
            _panel.chats_list[_fi].get("remoteJid")
            if 0 <= _fi < len(_panel.chats_list) else None
        )
        if hasattr(self, "archived_conversations_panel"):
            _ap = self.archived_conversations_panel
            _afi = _ap.conversations_list.GetFocusedItem()
            _ap._preserved_focused_jid = (
                _ap.chats_list[_afi].get("remoteJid")
                if 0 <= _afi < len(_ap.chats_list) else None
            )

        self._locked_chat_rows = (list(locked_chats), list(locked_names))

        # _all_chats_list / _all_chat_names always hold the full sorted list.
        # add_chats_to_ui() reads these to apply search / filter, then writes
        # back to chats_list / chat_names so indices stay consistent.
        self.conversations_panel._all_chats_list = main_chats
        self.conversations_panel._all_chat_names = main_names
        self.conversations_panel.chats_list = main_chats
        self.conversations_panel.chat_names = main_names

        if hasattr(self, "archived_conversations_panel"):
            self.archived_conversations_panel._all_chats_list = arch_chats
            self.archived_conversations_panel._all_chat_names = arch_names
            self.archived_conversations_panel.chats_list = arch_chats
            self.archived_conversations_panel.chat_names = arch_names

        if hasattr(self, "locked_conversations_panel"):
            if getattr(self, "_chat_lock_unlocked", False):
                self.locked_conversations_panel.set_all_chats(
                    locked_chats, locked_names
                )
            else:
                self.locked_conversations_panel.set_all_chats([], [])

        if self.IsShown():
            self.add_chats_to_ui()
        # Refresh title whenever chat list / unread counts change.
        # Tray tooltip is only refreshed while the window is hidden — when
        # visible the title already shows unread counts, and RemoveIcon/SetIcon
        # disrupts NVDA focus (see tray_manager.py update_tooltip docstring).
        self._update_title()

    def _apply_chat_lists_if_current(self, generation: int, *args):
        """Apply a chat-list rebuild only if no newer one has been kicked off
        since. See ``_chat_list_generation`` for why this matters — without
        it, two rebuilds racing (e.g. a message arriving right as sync
        finishes) could apply in the wrong order and leave the UI showing the
        older/stale one."""
        if generation != self._chat_list_generation:
            logging.debug(
                "[chat_lists] discarding stale rebuild (generation=%d, current=%d)",
                generation, self._chat_list_generation,
            )
            return
        self._apply_chat_lists(*args)

    def set_chats(self):
        # NOTE: _build_lid_to_phone_cache() is intentionally NOT called here.
        # It scans every message in every chat (O(chats × messages)) and is
        # too expensive to run on the wx main thread. The cache is built once
        # at startup (in init_chats) and then maintained incrementally by
        # _extract_lid_mapping() on each new message.
        self._chat_list_generation += 1
        generation = self._chat_list_generation
        def _bg():
            try:
                result = self._compute_chat_lists()
                wx.CallAfter(self._apply_chat_lists_if_current, generation, *result)
            except Exception:
                logging.exception("[set_chats] Unhandled error during bg set_chats")
        threading.Thread(target=_bg, daemon=True).start()

    # How often the watchdog pings the wx main loop, and how long a ping may
    # go unanswered before the UI counts as stalled. One second between pings
    # costs a single no-op CallAfter per second while everything is healthy.
    _UI_WATCHDOG_INTERVAL = 1.0
    _UI_WATCHDOG_STALL_SECONDS = 2.0
    #: Longest gap between two reports of the *same* unchanging stack. The
    #: sampling rate does not change, only how often an identical sample is
    #: written, so a genuine freeze still leaves periodic evidence while an
    #: open modal dialog costs one line a minute instead of thirty.
    _UI_WATCHDOG_MAX_REPORT_GAP = 60.0

    def start_ui_watchdog(self):
        """Detect a frozen wx main loop and log *where* it is frozen.

        Every UI-freeze report so far has been diagnosed by inference, because
        the main thread writes nothing to the log while it is blocked — the
        freeze window simply contains worker-thread lines and a hole. Two
        successive theories built that way (a refresh storm, then an oversized
        pagination window) each explained the symptom and each turned out not
        to be the cause, which is a good sign the guessing should stop.

        This pings the main loop once a second with a no-op CallAfter. When a
        ping goes unanswered past _UI_WATCHDOG_STALL_SECONDS the main thread is
        by definition not draining its event queue, and sys._current_frames()
        gives its stack from *outside* it — so the log gets the exact call the
        UI is stuck in, repeated every couple of seconds for as long as the
        stall lasts, followed by how long it took to recover.

        Costs nothing while the UI is healthy and never touches UI state, so it
        stays on permanently rather than being a debug-only switch.
        """
        main_id = threading.main_thread().ident

        def _loop():
            import sys as _sys
            import traceback as _traceback
            while not getattr(self, "_shutting_down", False):
                pong = threading.Event()
                t0 = time.monotonic()
                try:
                    wx.CallAfter(pong.set)
                except Exception:
                    return          # app is going away
                stalled = False
                last_stack = None
                next_report = 0.0
                report_gap = self._UI_WATCHDOG_STALL_SECONDS
                while not pong.wait(self._UI_WATCHDOG_STALL_SECONDS):
                    if getattr(self, "_shutting_down", False):
                        return
                    stalled = True
                    frame = _sys._current_frames().get(main_id)
                    stack = ("".join(_traceback.format_stack(frame)) if frame
                             else "<main thread frame unavailable>")
                    elapsed = time.monotonic() - t0
                    # A stack that keeps changing is the interesting case, so
                    # it is always logged. An unchanging one is reported on a
                    # doubling backoff instead of every couple of seconds,
                    # because the longest "stall" this ever sees is not a bug
                    # at all: a modal dialog runs its own event loop and never
                    # answers the ping, so a re-pairing prompt left on screen
                    # produced 1,400 lines of identical stack in four minutes
                    # and buried the session failure that had opened it. The
                    # log is truncated every launch and is the only record of
                    # that failure; drowning it costs the diagnosis.
                    if stack != last_stack or elapsed >= next_report:
                        logging.warning(
                            "[ui-watchdog] UI thread unresponsive for %.1fs — main thread stack:\n%s",
                            elapsed, stack)
                        last_stack = stack
                        next_report = elapsed + report_gap
                        report_gap = min(report_gap * 2,
                                         self._UI_WATCHDOG_MAX_REPORT_GAP)
                if stalled:
                    logging.warning(
                        "[ui-watchdog] UI thread responsive again after %.1fs.",
                        time.monotonic() - t0)
                time.sleep(self._UI_WATCHDOG_INTERVAL)

        threading.Thread(target=_loop, daemon=True, name="ui-watchdog").start()
        logging.info("[ui-watchdog] started (ping every %.0fs, stall threshold %.0fs).",
                     self._UI_WATCHDOG_INTERVAL, self._UI_WATCHDOG_STALL_SECONDS)

    # Longer than the 300 ms used for content refreshes: this one only ever
    # repaints *names* that background resolution has just filled in, and a
    # name settling a second later is invisible to the user — while the
    # resolution loops that trigger it run in batches for minutes on end.
    _ACTIVE_REFRESH_DEBOUNCE_MS = 1000

    def _schedule_refresh_active_messages(self, jids=None):
        """Debounce refresh_active_conversation_messages().

        That method re-renders every row of the open conversation
        (SetItemText + _render_message_line per message), and three separate
        background loops — [Contact Resolution], [Mentions Scan] and
        [LID Resolution] — used to wx.CallAfter it once per resolved batch.
        On an account with thousands of unresolved @lid senders those batches
        arrive continuously for minutes, so the wx event queue filled with
        full re-renders faster than the main loop could drain them.

        Confirmed, not inferred: the UI watchdog caught three stalls of 9.4 s,
        19.8 s and 40.1 s, and all 33 stack samples taken during them landed on
        the same line — conversations.py's `SetItemText(i,
        self._render_message_line(msg))`. Two earlier theories about this
        freeze (a refresh storm on the history path, then an oversized
        pagination window) were wrong; this is what the stacks actually show.

        *jids* are the JIDs the caller has just resolved, accumulated across
        every batch that lands inside one debounce window. The resolution
        loops know exactly whose name changed, and since the message window no
        longer has a ceiling (it keeps whatever history the user loaded with
        Home) the difference is repainting a handful of rows instead of
        thousands. Omitting it — or passing an empty collection — still means
        "I don't know what changed, repaint everything", which stays the safe
        default for any caller that can't tell: a row that should have been
        repainted and wasn't keeps announcing raw @lid/phone digits, which is
        worse than the slowness this avoids.

        **UI thread only.** The accumulator below is a plain set with no lock,
        and every caller reaches it through wx.CallAfter — including
        register_jid_mapping(), which is itself a multi-threaded writer (the
        sync thread and the Socket.IO thread both call it, under
        _lid_mapping_lock). Dropping that wx.CallAfter because it looks
        redundant corrupts this set silently, with no exception to point at it.
        """
        if jids:
            pending = getattr(self, "_refresh_active_jids", set())
            # None is the sticky "repaint everything" request — a later batch
            # that does know its JIDs must not narrow it back down.
            if pending is not None:
                pending = set(pending)
                pending.update(j for j in jids if isinstance(j, str) and j)
                self._refresh_active_jids = pending
        else:
            self._refresh_active_jids = None
        if getattr(self, "_refresh_active_pending", False):
            return
        self._refresh_active_pending = True
        wx.CallLater(self._ACTIVE_REFRESH_DEBOUNCE_MS,
                     self._do_scheduled_refresh_active_messages)

    def _do_scheduled_refresh_active_messages(self):
        """Run the coalesced name repaint. Flag is cleared first so a failure
        cannot wedge every later batch."""
        self._refresh_active_pending = False
        # Drain before rendering, not after: a batch arriving while the
        # repaint runs then finds an empty accumulator and a cleared pending
        # flag, so it schedules its own round instead of being swallowed by
        # this one (or repainted twice by it).
        jids = getattr(self, "_refresh_active_jids", None)
        self._refresh_active_jids = set()
        # An empty set means every JID handed in was filtered out, i.e. we no
        # longer know what changed — same contract as None.
        if not jids:
            jids = None
        cp = getattr(self, "conversations_panel", None)
        if cp is None:
            # The drained JIDs are dropped here, unlike on the failure path
            # below, and that is fine rather than overlooked: with no panel
            # there is no open conversation to leave stale, and whenever one is
            # opened it renders from scratch.
            return
        # Medido, não estimado: a janela de paginação deixou de ser limitada a
        # messages_page_size quando o usuário carrega histórico, e a suspeita de
        # que uma janela grande custa caro aqui já foi levantada duas vezes sem
        # nenhum número. Uma linha por repaint responde se 4000 linhas custam
        # 80 ms ou 900 ms — e é o mesmo laço que os stacks do watchdog apontam.
        started = time.monotonic()
        try:
            painted = cp.refresh_active_conversation_messages(jids=jids)
        except Exception:
            logging.exception("[_do_scheduled_refresh_active_messages] repaint failed")
            # The batch was drained before the call, so those JIDs are gone and
            # the next one would be scoped to its own — the rows this round was
            # meant to fix would never be repainted again. _render_message_line()
            # has raised in production, which is why the try exists at all, so
            # degrade to a full repaint and reschedule it.
            #
            # Exactly once, though. A malformed record makes the render raise
            # every time, and rescheduling unconditionally turns that into a
            # permanent 1 Hz Freeze/SetItemText/Thaw cycle on messages_list —
            # the accessibility-event flood the Freeze() exists to prevent, now
            # forever — plus log.log growing without bound on one traceback, in
            # the file that is this project's primary diagnostic tool. One
            # retry keeps the whole point (a lost scoped round comes back as a
            # full one) and gives up when the failure is deterministic.
            if getattr(self, "_refresh_active_failed_once", False):
                logging.error(
                    "[_do_scheduled_refresh_active_messages] repaint failed twice "
                    "in a row — not rescheduling; the open conversation keeps "
                    "whatever text it currently shows until something else "
                    "rebuilds it.")
            else:
                self._refresh_active_failed_once = True
                self._refresh_active_jids = None
                wx.CallAfter(self._schedule_refresh_active_messages)
        else:
            self._refresh_active_failed_once = False
            if logging.getLogger().isEnabledFor(logging.INFO):
                # The panel widens a scoped request back to a full pass when it
                # can't address every affected row, so "requested" is all this
                # side can honestly claim.
                scope = "full" if jids is None else f"{len(jids)} resolved JID(s) requested"
                logging.info(
                    "[_do_scheduled_refresh_active_messages] repainted %d of %d row(s) "
                    "in %.0f ms (%s).",
                    painted,
                    len(getattr(cp, "_sorted_messages", []) or []),
                    (time.monotonic() - started) * 1000.0,
                    scope,
                )

    def _schedule_refresh_messages(self):
        """Debounce the open conversation's message-list rebuild.

        on_historical_message() fires once per backfilled message, and for the
        conversation currently on screen every one of them used to schedule its
        own refresh_messages_if_changed() on the wx main thread. That call is
        not cheap: _messages_signature() walks *every stored record* of the chat
        — ~1400 on the group this was reported against, not just the 200 on
        screen — calling _get_message_content() on each, and whenever the
        signature differs, which it does on every genuinely new message,
        populate_messages() rebuilds the native list with DeleteAllItems() plus
        one Append() per visible row.

        During a history backfill that is hundreds of full rebuilds queued
        back-to-back on the UI thread, and wx does not coalesce CallAfter — so
        opening a large, actively-backfilling group locked the interface up for
        as long as its history kept landing. Reported live against a group
        the logs could not show it directly because
        nothing on the UI thread writes to them, but the freeze window held 95
        worker-thread lines and not one from the UI module.

        Same shape as _schedule_set_chats() below, and called from the same
        place, so a burst of N messages costs one rebuild instead of N.
        """
        if getattr(self, "_refresh_messages_pending", False):
            return
        self._refresh_messages_pending = True
        wx.CallLater(300, self._do_scheduled_refresh_messages)

    def _do_scheduled_refresh_messages(self):
        """Run the coalesced rebuild. Re-checks the panel still has a
        conversation open — the user may have closed it during the window."""
        self._refresh_messages_pending = False
        cp = getattr(self, "conversations_panel", None)
        if cp is None or getattr(cp, "conversation", None) is None:
            return
        try:
            cp.refresh_messages_if_changed()
        except Exception:
            logging.exception("[_do_scheduled_refresh_messages] refresh failed")

    def _schedule_set_chats(self):
        """Debounce set_chats() so rapid message bursts trigger only one rebuild.
        Safe to call from any thread; scheduling happens on the wx main thread."""
        if getattr(self, "_set_chats_pending", False):
            return
        self._set_chats_pending = True
        wx.CallLater(300, self._do_scheduled_set_chats)

    def _do_scheduled_set_chats(self):
        """Run heavy computation in background; apply UI changes on main thread."""
        self._set_chats_pending = False
        self._chat_list_generation += 1
        generation = self._chat_list_generation
        def _bg():
            try:
                # _build_lid_to_phone_cache() is intentionally NOT called here.
                # It scans every message in every chat (O(total_messages)) and is
                # too expensive to run on every WebSocket event.  The cache is
                # maintained incrementally by _extract_lid_mapping() on each new
                # message, and rebuilt in full only at startup (set_chats calls).
                result = self._compute_chat_lists()
                wx.CallAfter(self._apply_chat_lists_if_current, generation, *result)
            except Exception:
                logging.exception("[_do_scheduled_set_chats] Unhandled error during scheduled set_chats")
        threading.Thread(target=_bg, daemon=True).start()

    def _preview_sender_from_jid(self, jid: str) -> str:
        """
        Resolve a participant JID to a display name for chat list previews.
        Tries contacts dict (with @lid bridging), then falls back to
        format_number on the phone-number JID. Never returns a bare @lid string.
        """
        if not jid:
            return ""
        ppm = getattr(self, "_presence_pushname_map", {})
        phone_jid = ""
        contact = self._get_contact_tolerant(jid)
        if not contact and jid.endswith("@lid"):
            phone_jid = getattr(self, "_lid_to_phone", {}).get(jid, "")
            if phone_jid:
                contact = self._get_contact_tolerant(phone_jid)
        if contact:
            name = (contact.get("name") or contact.get("pushName") or "").strip()
            if name and not is_phone_like(name):
                return name
        # Fallback: presence-learned pushName map
        for lookup_jid in ([jid, phone_jid] if phone_jid else [jid]):
            pname = (ppm.get(lookup_jid) or "").strip()
            if pname and not pname.isdigit() and not is_phone_like(pname):
                return pname
        if jid.endswith("@lid"):
            if not phone_jid:
                phone_jid = getattr(self, "_lid_to_phone", {}).get(jid, "")
            return format_number(phone_jid) if phone_jid else self.i18n.t("unnamed_participant")
        if jid.endswith("@g.us"):
            return self.i18n.t("unknown_group")
        return format_number(jid)

    # Message types that count as "the conversation's last message" — the ones
    # a user would recognise as activity. Deliberately excludes the silent
    # bookkeeping WhatsApp stores alongside real messages: groupNotification
    # ("X entrou no grupo"), notification_template, and every unknown type.
    #
    # reactionMessage is ALSO deliberately excluded, even though a
    # reactionMessage record legitimately sits in a chat's `records` (see
    # on_historical_message() — needed there so ConversationsPanel can
    # rebuild the in-conversation reaction display on reopen). A reaction
    # has its own dedicated preview channel — chat["_last_reaction"], set by
    # _track_last_reaction() and consumed directly by _last_msg_preview()
    # before it ever reaches this allowlist — so letting a reactionMessage
    # record ALSO count here served no purpose and was actively harmful:
    # since _last_msg_preview() has no rendering case for messageType
    # "reactionMessage", picking one as `last` (which happened whenever
    # _last_reaction hadn't been (re)populated for it, e.g. right after an
    # app restart or an F5 resync, since _last_reaction is in-memory only)
    # rendered as literally "Mensagem incompatível". It also let a bare
    # reaction's timestamp float a chat to the top of the list via
    # _chat_last_ts(), independent of any real message ever being sent.
    _PREVIEW_MESSAGE_TYPES = frozenset({
        "conversation", "extendedTextMessage", "imageMessage", "videoMessage",
        "audioMessage", "documentMessage", "stickerMessage", "contactMessage",
        "locationMessage", "liveLocationMessage",
        "pollCreationMessage", "pollCreationMessageV2", "pollCreationMessageV3",
        "pollUpdateMessage",
        "buttonsMessage", "listMessage", "templateMessage", "interactiveMessage",
        "buttonsResponseMessage", "listResponseMessage", "protocolMessage",
        CALL_LOG_MESSAGE_TYPE, LEGACY_CALL_LOG_TYPE,
    })

    @classmethod
    def _counts_as_last_message(cls, m) -> bool:
        """True when a record should decide a chat's preview and its position.

        Both the preview and the sort key have to agree on this, or the list
        contradicts itself: a group where someone merely joined jumps to the top
        while still showing a week-old preview, because the join was counted for
        ordering but skipped for display. Observed live — a group whose newest
        stored record was a groupNotification at 09:52 sat above conversations
        from minutes earlier while displaying its real last message from five
        days before.
        """
        if not isinstance(m, dict):
            return False
        # A message that permanently failed to send (retries exhausted,
        # _mark_message_failed()) never reached WhatsApp — it must not be
        # treated as the conversation's real last message. Without this the
        # chat-list preview kept showing a message the recipient never got,
        # with no visible cue anything was wrong, until the user happened to
        # reopen the conversation (which rebuilds this from the same
        # records — so it "fixed itself" there for an unrelated reason, not
        # because this case was actually handled).
        if m.get("_send_failed"):
            return False
        # Same reasoning for a message the user deleted while it was still
        # sending: its record is kept for a moment longer only so the WebSocket
        # echo has something of the right type to bind to (see
        # ConversationsPanel._cancel_pending_message()). The row is already gone
        # from the conversation, so showing it as the chat's last message would
        # put a message the user just deleted back in the list.
        if m.get("_cancelled_awaiting_id"):
            return False
        m_type = m.get("messageType", "")
        if m_type not in cls._PREVIEW_MESSAGE_TYPES:
            return False
        if m_type == "protocolMessage":
            protocol = (m.get("message") or {}).get("protocolMessage") or {}
            return protocol.get("type") in (3, "REVOKE", "revoke")
        return True

    def _recompute_chat_last_message(self, jid: str):
        """Recompute chat["lastMessage"]/["t"] from the records still present
        after one or more messages were removed (local delete, revoke-for-
        everyone, remote-mirrored deletion). Both the preview and the sort
        key (_chat_last_ts) fall back to these fields whenever they are newer
        than anything left in records, so without this a deleted message kept
        the chat pinned at its old position with its old preview text forever.

        Also drops chat["_last_reaction"] when the message it points to is
        one of the ones just removed (issue #72). A reaction is deliberately
        never added to `records` itself (see _track_last_reaction()), so
        deleting its target message left nothing here for the ordinary
        "did the last message disappear" check to catch — _last_msg_preview()
        kept showing "you reacted with X to <deleted message>" as the chat's
        latest activity forever, since the only guard it has is a timestamp
        comparison against whatever real message remains, and a reaction
        newer than every remaining message passes that unconditionally.
        """
        chat = self.chats.get(jid)
        if not chat:
            return

        def _ts(m):
            val = int(m.get("messageTimestamp") or m.get("timestamp") or m.get("t") or 0)
            return val // 1000 if val > 1_000_000_000_000 else val

        records_wrapper = chat.get("messages") or {}
        records = []
        if isinstance(records_wrapper, dict):
            inner_wrapper = records_wrapper.get("messages") or {}
            if isinstance(inner_wrapper, dict):
                records = inner_wrapper.get("records") or []

        candidates = [m for m in records if self._counts_as_last_message(m)]
        if candidates:
            last = max(candidates, key=_ts)
            chat["lastMessage"] = last
            chat["t"] = _ts(last)
        else:
            chat["lastMessage"] = None
            chat["t"] = 0

        last_reaction = chat.get("_last_reaction")
        if last_reaction:
            target_id = last_reaction.get("target_id", "")
            still_present = any(
                isinstance(m, dict) and m.get("key", {}).get("id") == target_id
                for m in records
            )
            if not still_present:
                chat.pop("_last_reaction", None)

        if hasattr(self, "db") and self.db is not None:
            try:
                self.db.upsert_chat(jid, chat)
            except Exception as exc:
                logging.warning(
                    "[_recompute_chat_last_message] DB upsert failed for %s: %s", jid, exc
                )

    def _last_msg_preview(self, chat: dict) -> str:
        """
        Build a compact last-message description for the conversations list.
        Returns "" if no messages are found.
        Format: "[você: ]{content} {timestamp}"
        """
        records_wrapper = chat.get("messages") or {}
        records = []
        if isinstance(records_wrapper, dict):
            inner_wrapper = records_wrapper.get("messages") or {}
            if isinstance(inner_wrapper, dict):
                records = list(inner_wrapper.get("records") or [])

        # Shared with _chat_last_ts() so the preview and the ordering can never
        # disagree about which record is a chat's last message.
        is_displayable = self._counts_as_last_message

        def _get_ts(m):
            if not isinstance(m, dict):
                return 0
            val = int(m.get("timestamp", 0) or m.get("messageTimestamp", 0) or m.get("t", 0) or 0)
            return val // 1000 if val > 1_000_000_000_000 else val

        last = None
        if records:
            try:
                last = max(
                    (m for m in records if is_displayable(m)),
                    key=_get_ts,
                    default=None,
                )
            except Exception:
                last = None
        if last is None:
            candidate = chat.get("lastMessage")
            if isinstance(candidate, dict) and is_displayable(candidate):
                last = candidate

        i18n = self.i18n

        # A reaction is deliberately never added to `records` (on_new_message
        # returns early for messageType == "reactionMessage" so it can't
        # pollute the message list or unread counts) — it's tracked
        # separately in chat["_last_reaction"] instead (see
        # _track_last_reaction()). Show it here in place of the last real
        # message only when it is genuinely the most recent event in the
        # chat; a reaction to an older message must not resurrect itself as
        # the preview once newer messages have since arrived.
        last_reaction = chat.get("_last_reaction")
        if last_reaction and last_reaction.get("timestamp", 0) >= _get_ts(last):
            emoji = last_reaction.get("emoji", "")
            orig_text = ""
            target_id = last_reaction.get("target_id", "")
            if target_id:
                for m in records:
                    if isinstance(m, dict) and m.get("key", {}).get("id") == target_id:
                        orig_type = m.get("messageType", "")
                        orig_obj  = m.get("message") or {}
                        if orig_type == "conversation":
                            orig_text = (orig_obj.get("conversation") or "")
                        elif orig_type == "extendedTextMessage":
                            orig_text = ((orig_obj.get("extendedTextMessage") or {}).get("text") or "")
                        elif orig_type in ("audioMessage", "audio", "ptt"):
                            is_ptt = is_voice_message(m) or bool(isinstance(orig_obj, dict) and is_voice_message({"messageType": "audioMessage", "message": orig_obj}))
                            vm_mode = self.settings.get("user_interface", {}).get("voice_message_mode", "voice_message")
                            orig_text = i18n.t("message_type_voice_message") if (vm_mode == "voice_message" and is_ptt) else i18n.t("message_type_audio")
                        elif orig_type == "videoMessage":
                            orig_text = i18n.t("video")
                        elif orig_type == "imageMessage":
                            orig_text = i18n.t("photo")
                        elif orig_type == "documentMessage":
                            orig_text = i18n.t("document")
                        elif orig_type == "stickerMessage":
                            orig_text = i18n.t("sticker")
                        elif orig_type == "contactMessage":
                            orig_text = i18n.t("notif_contact")
                        elif orig_type == "locationMessage":
                            orig_text = i18n.t("notif_location")
                        else:
                            orig_text = i18n.t("notif_unsupported")
                        break
            ts_val = last_reaction.get("timestamp", 0)
            time_str = ""
            if ts_val:
                try:
                    from datetime import datetime as _dt
                    dt    = _dt.fromtimestamp(ts_val)
                    today = _dt.now().date()
                    if dt.date() == today:
                        time_str = dt.strftime(get_time_format(i18n.t("time_fmt")))
                    else:
                        time_str = dt.strftime(get_datetime_format(i18n.t("datetime_fmt")))
                except Exception:
                    pass
            if last_reaction.get("from_me"):
                label = i18n.t("reaction_preview_you").format(emoji=emoji)
            else:
                sender_jid = last_reaction.get("participant", "")
                push       = last_reaction.get("push_name", "")
                if sender_jid.endswith("@g.us") and push and push.isdigit():
                    sender_jid = f"{push}@s.whatsapp.net"
                sender_name = (
                    self._resolve_contact_name({"remoteJid": sender_jid})
                    or (push if push and not is_phone_like(push) else "")
                    or self._preview_sender_from_jid(sender_jid)
                )
                label = i18n.t("reaction_preview_them").format(name=sender_name, emoji=emoji)
            parts = [label]
            if orig_text:
                parts.append(orig_text)
            if time_str:
                parts.append(time_str)
            return " ".join(parts)

        if last is None:
            return ""

        from_me  = last.get("key", {}).get("fromMe", False)
        msg_type = last.get("messageType", "conversation")
        msg_obj  = last.get("message") or {}

        # Build compact content
        def _dur(secs):
            try:
                s = int(secs or 0)
            except Exception:
                return "0:00"
            h, m, sec = s // 3600, (s % 3600) // 60, s % 60
            return f"{h}:{m:02d}:{sec:02d}" if h > 0 else f"{m}:{sec:02d}"

        if is_call_log(last):
            # Same sentence the conversation row reads (core/call_log.py).
            panel = getattr(self, "conversations_panel", None)
            content = call_log_label(
                last, i18n,
                panel._format_duration if panel is not None else (lambda _s: ""))
        elif msg_type == "conversation":
            content = msg_obj.get("conversation") or ""
            if looks_like_binary_blob(content):
                # Some senders — the official WhatsApp updates account
                # ("0@s.whatsapp.net") observed live — deliver a message
                # whose "conversation" text field is itself a raw base64
                # image blob rather than real text. Without this guard the
                # chat-list preview showed "+0: /9j/4AAQSkZJRg..." verbatim.
                content = i18n.t("notif_unsupported")
        elif msg_type == "extendedTextMessage":
            content = (msg_obj.get("extendedTextMessage") or {}).get("text", "") or ""
            if looks_like_binary_blob(content):
                content = i18n.t("notif_unsupported")
            ext = msg_obj.get("extendedTextMessage") or {}
            mentioned = (
                (last.get("contextInfo") or {}).get("mentionedJid")
                or (msg_obj.get("contextInfo") or {}).get("mentionedJid")
                or ext.get("contextInfo", {}).get("mentionedJid")
                or []
            )
            if isinstance(mentioned, list) and mentioned:
                for jid in mentioned:
                    if not isinstance(jid, str):
                        continue
                    if self._is_self_jid(jid):
                        name = "eu"
                    else:
                        if hasattr(self, "conversations_panel"):
                            name = self.conversations_panel._get_participant_name(
                                jid, resolve_missing=False
                            )
                        else:
                            name = ""
                    
                    lid_local = jid.rsplit("@", 1)[0]
                    _lid_map = getattr(self, "_lid_to_phone", {})
                    phone_jid = _lid_map.get(jid, "") if jid.endswith("@lid") else ""
                    phone = phone_jid.split("@")[0] if phone_jid else jid.split("@")[0]
                    
                    placeholder = None
                    if f"@{lid_local}" in content:
                        placeholder = lid_local
                    elif phone and f"@{phone}" in content:
                        placeholder = phone
                        
                    if not placeholder:
                        continue
                        
                    if name and name != placeholder and name != jid:
                        content = content.replace(f"@{placeholder}", f"@{name}")
        elif msg_type in ("audioMessage", "audio", "ptt"):
            audio_inner = (msg_obj.get("audioMessage") or {}) if isinstance(msg_obj, dict) else {}
            dur = _dur(audio_inner.get("seconds"))
            is_ptt = is_voice_message(last) or bool(isinstance(msg_obj, dict) and is_voice_message({"messageType": "audioMessage", "message": msg_obj}))
            vm_mode = self.settings.get("user_interface", {}).get("voice_message_mode", "voice_message")
            lbl = i18n.t("message_type_voice_message") if (vm_mode == "voice_message" and is_ptt) else i18n.t("message_type_audio")
            content = f"{lbl} {dur}".strip()
        elif msg_type == "videoMessage":
            video = msg_obj.get("videoMessage") or {}
            dur   = _dur(video.get("seconds"))
            content = f"{i18n.t('video')} {dur}"
        elif msg_type == "imageMessage":
            img     = msg_obj.get("imageMessage") or {}
            caption = (img.get("caption") or "").strip()
            content = i18n.t("photo") + (f" {caption}" if caption else "")
        elif msg_type == "documentMessage":
            doc      = msg_obj.get("documentMessage") or {}
            filename = doc.get("fileName") or doc.get("title") or ""
            size_bytes = doc.get("fileLength")
            size_str = ""
            if size_bytes:
                try:
                    sz  = int(size_bytes)
                    sep = i18n.t("decimal_separator")
                    if sz < 1024:
                        size_str = f"{sz} b"
                    elif sz < 1024 ** 2:
                        size_str = f"{sz / 1024:.1f}".replace(".", sep) + " kb"
                    elif sz < 1024 ** 3:
                        size_str = f"{sz / 1024 ** 2:.1f}".replace(".", sep) + " mb"
                    else:
                        size_str = f"{sz / 1024 ** 3:.1f}".replace(".", sep) + " gb"
                except (ValueError, TypeError):
                    pass
            parts = [i18n.t("document")]
            if filename:
                parts.append(filename)
            if size_str:
                parts.append(size_str)
            content = ", ".join(parts)
        elif msg_type == "stickerMessage":
            content = i18n.t("sticker")
        elif msg_type == "contactMessage":
            contact = msg_obj.get("contactMessage") or {}
            name = contact.get("displayName") or ""
            vcard = contact.get("vcard") or ""

            if not name or "BEGIN:VCARD" in name:
                vcard_to_parse = name if "BEGIN:VCARD" in name else vcard
                parsed_name = ""
                for line in vcard_to_parse.splitlines():
                    if line.startswith("FN:"):
                        parsed_name = line[3:].strip()
                        break
                if parsed_name:
                    name = parsed_name
                else:
                    name = i18n.t("unknown_contact")

            content = i18n.t("contact_message").format(name=name)
        elif msg_type == "contactsArrayMessage":
            arr = msg_obj.get("contactsArrayMessage") or {}
            contacts = arr.get("contacts") or []
            content = i18n.t("contacts_count").format(count=len(contacts))
        elif msg_type in ("locationMessage", "liveLocationMessage"):
            content = i18n.t("notif_location")
        elif msg_type in ("pollCreationMessage", "pollCreationMessageV2", "pollCreationMessageV3", "pollUpdateMessage"):
            poll = msg_obj.get("pollCreationMessage") or msg_obj.get("pollCreationMessageV2") or msg_obj.get("pollCreationMessageV3") or {}
            name = poll.get("name") or ""
            content = i18n.t("notif_poll").format(name=name) if name else i18n.t("notif_poll_no_name")
        elif msg_type == "buttonsMessage":
            content = i18n.t("notif_button")
        elif msg_type == "listMessage":
            content = i18n.t("notif_list")
        elif msg_type == "templateMessage":
            content = i18n.t("notif_template")
        elif msg_type == "protocolMessage":
            protocol = msg_obj.get("protocolMessage") or {}
            p_type = protocol.get("type")
            if p_type in (3, "REVOKE", "revoke"):
                content = i18n.t("notif_deleted")
            else:
                content = i18n.t("notif_system_message")
        else:
            # Logged so a future report of a raw, untranslated messageType
            # showing up in the chat-list preview (e.g. "audioMessage" wraps
            # such as view-once/ephemeral audio, which arrive under a
            # DIFFERENT outer messageType than "audioMessage" itself and
            # aren't unwrapped here) can be traced straight to the exact
            # type instead of only reproducing "some preview looks wrong".
            logging.info("[_last_msg_preview] unhandled messageType=%r for chat=%r",
                         msg_type, chat.get("remoteJid", ""))
            content = i18n.t("notif_unsupported")

        # Build time string
        ts = last.get("messageTimestamp")
        time_str = ""
        if ts:
            try:
                from datetime import datetime as _dt
                ts_val = int(ts)
                if ts_val > 1_000_000_000_000:
                    ts_val //= 1000
                dt    = _dt.fromtimestamp(ts_val)
                today = _dt.now().date()
                if dt.date() == today:
                    time_str = dt.strftime(get_time_format(i18n.t("time_fmt")))
                else:
                    time_str = dt.strftime(get_datetime_format(i18n.t("datetime_fmt")))
            except Exception:
                pass

        # For group chats add sender name before content (e.g. "João: vídeo 0:30")
        jid      = chat.get("remoteJid", "")
        is_group = jid.endswith("@g.us")
        if from_me:
            sender_prefix = self.self_reference_label() + ": "
        elif is_group:
            p_key      = last.get("key", {})
            sender_jid = last.get("participant") or p_key.get("participant") or p_key.get("remoteJid", "")
            push       = last.get("pushName", "")
            if sender_jid.endswith("@g.us") and push and push.isdigit():
                sender_jid = f"{push}@s.whatsapp.net"
            sender_name = (
                self._resolve_contact_name({"remoteJid": sender_jid})
                or (push if push and not is_phone_like(push) else "")
                or self._preview_sender_from_jid(sender_jid)
            )
            sender_prefix = f"{sender_name}: " if sender_name else ""
        else:
            sender_prefix = ""
        parts = [f"{sender_prefix}{content}"]
        if time_str:
            parts.append(time_str)
        if getattr(self, "settings", {}).get("user_interface", {}).get(
            "show_delivery_status_in_chat_list", True
        ):
            panel = getattr(self, "conversations_panel", None)
            if panel is not None:
                try:
                    # The panel renders every row here, not just its own open
                    # conversation, so the chat has to be named explicitly —
                    # otherwise _map_status() falls back to whichever chat
                    # happens to be open and applies its "Me"-chat receipt
                    # rule to somebody else's preview line.
                    status_text = panel._map_status(last, chat.get("remoteJid", ""))
                except Exception:
                    status_text = ""
                if status_text:
                    parts.append(status_text)
        return " ".join(parts)

    @staticmethod
    def _filter_archived_chats(chats: list, names: list, conv_filter: str,
                               search: str, fold_mode: str) -> "tuple[list, list]":
        """Return (chats, names) after applying the archived panel's own
        filter tabs and its own search field.

        Extracted so this can be tested directly without instantiating
        ArchivedConversationsPanel or MainWindow (both require a running
        wx.App) — mirrors why _conversation_search_candidates() above is a
        staticmethod. *search* and *fold_mode* are already normalized by the
        caller (normalize_for_search()/self._search_normalization_mode()),
        same as add_chats_to_ui() does for the main list's own search field —
        this one never reaches outside the archived list it filters.
        """
        displayed_chats: list = []
        displayed_names: list = []
        for i, chat in enumerate(chats):
            chat_jid = chat.get("remoteJid", "")
            if conv_filter == 'unread' and effective_unread_count(chat) == 0:
                continue
            if conv_filter == 'groups' and not chat_jid.endswith("@g.us"):
                continue
            if conv_filter == 'individual' and chat_jid.endswith("@g.us"):
                continue
            name = names[i] if i < len(names) else ""
            if search and search not in normalize_for_search(name, fold_mode):
                continue
            displayed_chats.append(chat)
            displayed_names.append(name)
        return displayed_chats, displayed_names

    def _refresh_archived_chats_in_ui(self, arch_focused_jid: "str | None" = None):
        """Update the archived conversations list using SetItem when possible.

        Avoids DeleteAllItems() when JID order/count is unchanged so the
        archived panel's scroll position and focus are preserved.
        """
        if not hasattr(self, "archived_conversations_panel"):
            return
        panel = self.archived_conversations_panel
        arch_full_chats = list(getattr(panel, '_all_chats_list', panel.chats_list))
        arch_full_names = list(getattr(panel, '_all_chat_names', panel.chat_names))
        arch_lst = panel.conversations_list
        arch_filter = getattr(panel, '_conv_filter', 'all')
        _fold = self._search_normalization_mode()
        arch_search = normalize_for_search(
            panel.search_field.GetValue().strip() if hasattr(panel, "search_field") else "",
            _fold,
        )
        filtered_chats, filtered_names = self._filter_archived_chats(
            arch_full_chats, arch_full_names, arch_filter, arch_search, _fold
        )

        new_arch_chats: list = []
        new_arch_names: list = []
        new_arch_texts: list = []
        for chat, name in zip(filtered_chats, filtered_names):
            unread = effective_unread_count(chat)
            unread_str = (
                f" {unread} " + (self.i18n.t("unread_messages") if unread > 1 else self.i18n.t("unread_message"))
                if unread > 0 else ""
            )
            preview = self._last_msg_preview(chat)
            item_text = name + unread_str
            if item_text and preview:
                item_text += f" {preview}"
            new_arch_chats.append(chat)
            new_arch_names.append(name)
            new_arch_texts.append(item_text)

        new_arch_jids = [c.get("remoteJid", "") for c in new_arch_chats]
        _arch_displayed_jids = getattr(panel, '_displayed_jids', None)

        _arch_fast_path_ok = False
        if (
            _arch_displayed_jids is not None
            and _arch_displayed_jids == new_arch_jids
            and arch_lst.GetItemCount() == len(new_arch_jids)
        ):
            try:
                for idx, new_text in enumerate(new_arch_texts):
                    if arch_lst.GetItemText(idx, 0) != new_text:
                        arch_lst.SetItem(idx, 0, new_text)
                _arch_fast_path_ok = True
            except Exception:
                # See add_chats_to_ui(): don't retry SetItem on a stale index,
                # fall through to the full rebuild below instead.
                pass
        if _arch_fast_path_ok:
            panel.chats_list = new_arch_chats
            panel.chat_names = new_arch_names
            return

        arch_list_has_focus = (wx.Window.FindFocus() == arch_lst)
        arch_fi = arch_lst.GetFocusedItem()
        if arch_fi != -1:
            try:
                arch_lst.SetItemState(arch_fi, 0, wx.LIST_STATE_FOCUSED)
            except Exception:
                pass
        arch_lst.DeleteAllItems()
        for item_text in new_arch_texts:
            arch_lst.Append((item_text,))
        panel.chats_list = new_arch_chats
        panel.chat_names = new_arch_names
        panel._displayed_jids = new_arch_jids

        if new_arch_chats:
            target_idx = -1
            if arch_focused_jid:
                for i, chat in enumerate(new_arch_chats):
                    if chat.get("remoteJid") == arch_focused_jid:
                        target_idx = i
                        break
            if target_idx != -1:
                if arch_list_has_focus and arch_lst.GetFocusedItem() != target_idx:
                    arch_lst.Focus(target_idx)
                if not arch_lst.IsSelected(target_idx):
                    arch_lst.Select(target_idx)
                arch_lst.EnsureVisible(target_idx)
            elif not getattr(self, "_initial_sync_running", False):
                last_jid = getattr(panel, "_last_open_jid", "")
                target_idx = 0
                if last_jid:
                    for i, chat in enumerate(new_arch_chats):
                        if chat.get("remoteJid") == last_jid:
                            target_idx = i
                            break
                if arch_list_has_focus:
                    arch_lst.Focus(target_idx)
                arch_lst.Select(target_idx)
                arch_lst.EnsureVisible(target_idx)

    def _chat_last_ts(self, c: dict) -> int:
        """The timestamp the conversations list sorts a chat by.

        Only counts records the preview would show — a system event (group
        join/leave, settings change, revoke, ...) stored in this chat's records
        must never push it back to the top just because its timestamp is the
        newest one on file. chat["t"]/lastMessage are already never set from a
        non-countable message (see on_new_message()/on_historical_message()),
        but this also scans raw records directly, so it needs the same filter.

        Extracted from _compute_chat_lists() so a single chat's position can be
        worked out without sorting the whole list — see _chat_sort_key() and
        move_chat_row_to_top().
        """
        chat_ts = int(c.get("t", 0) or 0)
        if chat_ts > 1_000_000_000_000:
            chat_ts //= 1000
        ts = chat_ts

        lm = c.get("lastMessage")
        if isinstance(lm, dict) and is_countable_message(lm):
            lm_ts = int(lm.get("timestamp", 0) or lm.get("messageTimestamp", 0) or lm.get("t", 0) or 0)
            if lm_ts > 1_000_000_000_000:
                lm_ts //= 1000
            if lm_ts > ts:
                ts = lm_ts

        records_wrapper = c.get("messages") or {}
        if isinstance(records_wrapper, dict):
            inner_wrapper = records_wrapper.get("messages") or {}
            if isinstance(inner_wrapper, dict):
                for m in list(inner_wrapper.get("records") or []):
                    if not self._counts_as_last_message(m):
                        continue
                    t = int(m.get("timestamp", 0) or m.get("messageTimestamp", 0) or m.get("t", 0) or 0)
                    if t > 1_000_000_000_000:
                        t //= 1000
                    if t > ts:
                        ts = t

        return ts if ts else 1

    def _chat_sort_key(self, chat: dict, name: str, pinned=None):
        """The conversations list's ordering: pinned chats first, then
        most-recent message descending, then alphabetically.

        Single source of truth — _compute_chat_lists() sorts by this, and
        move_chat_row_to_top() compares against it to place one row without
        sorting anything.
        """
        if pinned is None:
            pinned = self._pinned_chats
        j = chat.get("remoteJid", "")
        pin = 0 if j in pinned else 1
        return (pin, -self._chat_last_ts(chat), (name or "").lower())

    def move_chat_row_to_top(self, chat_jid: str) -> bool:
        """Move one chat to the top of its group after a new message, updating
        only that row. Returns whether it worked; callers fall back to
        _schedule_set_chats() when it returns False.

        A new message (sent or received) is the other half of the problem
        refresh_chat_row_text() solved for acks: it changes the row's text AND
        its position, so the row can't simply be repainted in place. But it
        does not require re-sorting the list either — a chat that just received
        the newest message belongs at the top of its own group (pinned or
        unpinned), and that claim is checked here rather than assumed: the
        chat's sort key is compared against the key of whatever currently sits
        at the top of that group, and anything that doesn't come out on top
        falls back to the full recompute.

        Only two sort keys are computed (this chat's and the incumbent's),
        instead of _compute_chat_lists() re-resolving names and timestamps for
        every chat in the account.
        """
        panel = getattr(self, "conversations_panel", None)
        if panel is None:
            return False
        displayed = getattr(panel, "_displayed_jids", None)
        chats_list = getattr(panel, "chats_list", None)
        names = getattr(panel, "chat_names", None)
        if not displayed or chats_list is None or names is None:
            return False
        lst = panel.conversations_list
        if not (len(displayed) == len(chats_list) == len(names) == lst.GetItemCount()):
            return False

        # A search or filter means the displayed list isn't simply the sorted
        # chat list, so "top of its group" isn't a position this can reason
        # about. Let the full path handle it.
        try:
            if panel.search_field.GetValue().strip():
                logging.info("[move_chat_row_to_top] %s: search active — full path", chat_jid)
                return False
        except Exception:
            return False
        if getattr(panel, "_conv_filter", "all") != "all":
            logging.info("[move_chat_row_to_top] %s: filter %r active — full path",
                         chat_jid, getattr(panel, "_conv_filter", "all"))
            return False

        wanted = {chat_jid}
        stored = self.chats.get(chat_jid)
        if isinstance(stored, dict) and stored.get("remoteJid"):
            wanted.add(stored["remoteJid"])
        idx = next((i for i, j in enumerate(displayed) if j in wanted), None)
        if idx is None:
            logging.info("[move_chat_row_to_top] %s: not rendered — full path", chat_jid)
            return False        # not rendered yet — a brand-new chat needs the full path

        chat = chats_list[idx]
        name = names[idx]
        pinned = self._pinned_chats
        key = self._chat_sort_key(chat, name, pinned)

        # Where this chat's group starts: pinned chats occupy the head of the
        # list, so an unpinned chat can only rise to just below them.
        is_pinned = (chat.get("remoteJid", "") in pinned)
        if is_pinned:
            group_start = 0
        else:
            group_start = 0
            while group_start < len(chats_list) and (
                chats_list[group_start].get("remoteJid", "") in pinned
            ):
                group_start += 1

        if idx == group_start:
            # Already at the top of its group — only the text can have changed.
            logging.info("[move_chat_row_to_top] %s: already at row %s — text only",
                         chat_jid, idx)
            return self.refresh_chat_row_text(chat_jid)

        incumbent_key = self._chat_sort_key(chats_list[group_start], names[group_start], pinned)
        if key >= incumbent_key:
            # Doesn't actually belong at the top (an older message arriving
            # late, a clock skew, an alphabetical tie) — don't guess.
            logging.info(
                "[move_chat_row_to_top] %s: stays at row %s — key=%s not above "
                "row %s key=%s; full path", chat_jid, idx, key, group_start, incumbent_key,
            )
            return False

        new_texts = list(lst.GetItemText(i, 0) for i in range(lst.GetItemCount()))
        new_texts.insert(group_start, new_texts.pop(idx))
        new_texts[group_start] = self._build_chat_item_text(chat, name)

        new_jids = list(displayed)
        new_jids.insert(group_start, new_jids.pop(idx))
        focused_jid = None
        focused_idx = lst.GetFocusedItem()
        if 0 <= focused_idx < len(displayed):
            focused_jid = displayed[focused_idx]

        if not self._apply_chat_rows_incrementally(
            lst, displayed, new_jids, new_texts, focused_jid
        ):
            logging.info("[move_chat_row_to_top] %s: row surgery declined — full path", chat_jid)
            return False
        logging.info("[move_chat_row_to_top] %s: moved row %s -> %s of %s",
                     chat_jid, idx, group_start, len(new_jids))

        chats_list.insert(group_start, chats_list.pop(idx))
        names.insert(group_start, names.pop(idx))
        panel._displayed_jids = new_jids
        # _all_chats_list/_all_chat_names back the unfiltered view add_chats_to_ui()
        # rebuilds from; keep them in step so the next full pass doesn't undo this.
        for attr in ("_all_chats_list", "_all_chat_names"):
            seq = getattr(panel, attr, None)
            if isinstance(seq, list) and len(seq) == len(chats_list):
                seq[:] = chats_list if attr == "_all_chats_list" else names
        return True

    def _build_chat_item_text(self, chat: dict, name: str) -> str:
        """Render one conversations-list row: name, unread badge, preview,
        presence, and the pinned/muted/blocked/selected suffixes.

        Extracted from add_chats_to_ui() so a single row can be rebuilt on its
        own — see refresh_chat_row_text().
        """
        chat_jid = chat.get("remoteJid", "")
        unread = effective_unread_count(chat)
        unread_str = (
            f" {unread} " + (self.i18n.t("unread_messages") if unread > 1 else self.i18n.t("unread_message"))
            if unread > 0 else ""
        )
        preview = self._last_msg_preview(chat)
        text = name + unread_str
        if preview:
            text += f" {preview}"
        chat_jid_norm = self._normalize_jid(chat_jid) if chat_jid else ""
        if chat_jid_norm:
            presence_label = self._presence_label_for_chat(
                chat_jid_norm,
                chat_jid_norm.endswith("@g.us"),
                resolve_missing=False,
            )
            if presence_label:
                text += f" {presence_label}"
        # Archived chats never appear in this list except when a global
        # search merges them in (_conversation_search_candidates), so the row
        # is otherwise indistinguishable from an active conversation — read
        # aloud, "Ana" from Arquivadas sounded exactly like "Ana" from the
        # normal list. Keyed off the merged set rather than is_chat_archived()
        # so it costs one set lookup per row instead of a candidate walk, and
        # so the archived panel's own rows are never suffixed.
        if chat_jid and chat_jid in getattr(
            self.conversations_panel, "_search_archived_jids", ()
        ):
            text += f" ({self.i18n.t('archived_suffix')})"
        if chat_jid_norm and self.is_chat_pinned(chat_jid_norm):
            text += f" ({self.i18n.t('pinned_suffix')})"
        if chat_jid_norm and self.is_chat_muted(chat_jid_norm):
            text += f" ({self.i18n.t('muted')})"
        if chat_jid_norm and self.is_contact_blocked(chat_jid_norm):
            text += f" ({self.i18n.t('blocked')})"
        is_selected = bool(chat_jid) and chat_jid in getattr(self.conversations_panel, "selected_chats", ())
        position = self.settings.get("user_interface", {}).get(
            "selected_announcement_position", "end"
        )
        return append_selected_marker(text, self.i18n.t("selected_suffix"), position, is_selected)

    def refresh_chat_row_text(self, chat_jid: str) -> bool:
        """Repaint ONE conversation's row in place. Returns whether it worked.

        For a change that alters a chat's row text but not its position in the
        list — a delivery ack walking "Pendente" to "Enviada" is the archetype,
        since it leaves the message's timestamp alone — going through
        set_chats() is enormously disproportionate: _compute_chat_lists()
        re-resolves the display name of EVERY chat (contact lookups, group name
        resolution, JID formatting) and add_chats_to_ui() then rebuilds the row
        text of every chat (each one walking that chat's records for a preview,
        plus presence/pinned/muted/blocked lookups) — all to change one row.
        On an account with several hundred chats that is seconds of work, which
        is exactly the delay reported between a message being sent and its
        preview leaving "Pendente".

        This touches only the row that changed. Callers must fall back to
        _schedule_set_chats() when it returns False — the row may be filtered
        out of the current view, the list may be mid-rebuild, or the chat may
        not be rendered at all.
        """
        panel = getattr(self, "conversations_panel", None)
        if panel is None:
            return False
        displayed = getattr(panel, "_displayed_jids", None)
        chats_list = getattr(panel, "chats_list", None)
        names = getattr(panel, "chat_names", None)
        if not displayed or chats_list is None or names is None:
            return False
        # The backing arrays must line up with what's on screen; if they don't,
        # a targeted SetItem would write the right text into the wrong row.
        lst = panel.conversations_list
        if not (len(displayed) == len(chats_list) == len(names) == lst.GetItemCount()):
            return False

        # Rows are identified by chat["remoteJid"], which is not always the key
        # the chat is stored under in self.chats (see _compute_chat_lists).
        wanted = {chat_jid}
        stored = self.chats.get(chat_jid)
        if isinstance(stored, dict) and stored.get("remoteJid"):
            wanted.add(stored["remoteJid"])
        idx = next((i for i, j in enumerate(displayed) if j in wanted), None)
        if idx is None:
            return False

        try:
            new_text = self._build_chat_item_text(chats_list[idx], names[idx])
            if lst.GetItemText(idx, 0) != new_text:
                lst.SetItem(idx, 0, new_text)
        except Exception:
            logging.exception("[refresh_chat_row_text] failed for %s; falling back", chat_jid)
            return False
        return True

    def _apply_chat_rows_incrementally(self, lst, old_jids: list, new_jids: list,
                                       new_item_texts: list, focused_jid) -> bool:
        """Move/insert/delete individual rows of *lst* instead of rebuilding it.

        Returns True when the control now matches *new_jids* exactly; False
        when the change was too large to be worth it (or a native call
        refused), in which case the caller falls back to the full rebuild.

        Keyboard focus follows the chat it was on, not the row index — the
        whole point of the incremental path is that a chat jumping to the top
        must not drag the user's cursor with it, and must not reset focus to
        row 0 the way DeleteAllItems() does.
        """
        ops = plan_row_updates(old_jids, new_jids)
        if not ops:
            # None  -> too churny, rebuild.  []  -> nothing to do, but the
            # caller only reaches here when the JID lists actually differ, so
            # an empty plan means the two disagreed about identity; rebuild.
            return False

        had_focus = (wx.Window.FindFocus() is lst)
        lst.Freeze()
        try:
            for kind, idx in ops:
                if kind == "delete":
                    lst.DeleteItem(idx)
                else:
                    lst.InsertItem(idx, new_item_texts[idx])
            # Rows that merely shifted keep their old text (a preview, unread
            # badge or presence label may have changed on any of them), so
            # resync every row the same way the SetItem path does.
            for idx, new_text in enumerate(new_item_texts):
                if lst.GetItemText(idx, 0) != new_text:
                    lst.SetItem(idx, 0, new_text)
        except Exception:
            # Same defence as the SetItem path: a native call rejected an
            # index we believed valid. The control is now in an unknown state,
            # so let the caller rebuild it from scratch rather than leaving it
            # half-updated.
            logging.exception("[add_chats_to_ui] incremental row update failed; rebuilding")
            return False
        finally:
            lst.Thaw()

        if lst.GetItemCount() != len(new_jids):
            logging.warning(
                "[add_chats_to_ui] incremental update left %s rows for %s chats; rebuilding",
                lst.GetItemCount(), len(new_jids),
            )
            return False

        if focused_jid and focused_jid in new_jids:
            target_idx = new_jids.index(focused_jid)
            try:
                if lst.GetFocusedItem() != target_idx:
                    lst.Focus(target_idx)
                if not lst.IsSelected(target_idx):
                    lst.Select(target_idx)
                if had_focus:
                    lst.EnsureVisible(target_idx)
            except Exception:
                logging.exception("[add_chats_to_ui] restoring focus after incremental update failed")
        return True

    @staticmethod
    def _conversation_search_candidates(
        main_chats, main_names, archived_chats, archived_names, include_archived
    ):
        """Return aligned chat/name lists used by the global conversation search.

        The ordinary conversations list deliberately excludes archived chats,
        but a non-empty global search must query both lists, matching WhatsApp.
        Keep the normal list unchanged when search is empty.

        The third return value is the set of JIDs contributed by the archived
        panel — _build_chat_item_text() suffixes exactly those rows, since a
        merged archived result is otherwise indistinguishable from an active
        one to a screen reader.

        The de-duplication compares raw ``remoteJid``, which means it does NOT
        recognise an @lid/@s.whatsapp.net pair as the same chat. It does not
        have to: _apply_chat_lists() partitions self.chats into the two panels,
        so the same dict never sits in both, and the guard is only cheap
        insurance against a caller passing overlapping lists.
        """
        chats = list(main_chats)
        names = list(main_names)
        if not include_archived:
            return chats, names, set()

        seen_jids = {
            chat.get("remoteJid", "")
            for chat in chats
            if isinstance(chat, dict) and chat.get("remoteJid")
        }
        merged_archived = set()
        for index, chat in enumerate(archived_chats):
            if not isinstance(chat, dict):
                continue
            jid = chat.get("remoteJid", "")
            # Skipped for the same reason a non-dict entry is: the suffix is
            # carried by the JID set, so a row with no JID would be merged in
            # and then read aloud as an ordinary active conversation — and it
            # cannot be opened from the list either.
            if not jid or jid in seen_jids:
                continue
            seen_jids.add(jid)
            merged_archived.add(jid)
            chats.append(chat)
            names.append(archived_names[index] if index < len(archived_names) else "")
        return chats, names, merged_archived

    def add_chats_to_ui(self):
        """Rebuild the conversations list from the current chats data.

        Applies active search and conversation filter to both the wx.ListCtrl
        and the backing chats_list/chat_names arrays so that list indices are
        always consistent.  Without this sync the user would open the wrong
        conversation when a search was active.
        """
        _fold        = self._search_normalization_mode()
        search       = normalize_for_search(
            self.conversations_panel.search_field.GetValue().strip(), _fold
        )
        conv_filter  = getattr(self.conversations_panel, '_conv_filter', 'all')

        # Used below to tell "the same filtered view just lost an item" (where
        # reusing the old row position to keep focus nearby makes sense) apart
        # from "the active filter/search changed" (where the old row position
        # belongs to a different, unrelated list and must not be reused).
        _filter_or_search_changed = (
            getattr(self, "_last_conv_filter_key", None) != (conv_filter, search)
        )
        self._last_conv_filter_key = (conv_filter, search)

        # Always start from the full sorted lists saved by set_chats() so
        # that restoring the window or clearing a search shows all chats.
        full_chats = list(getattr(self.conversations_panel, '_all_chats_list',
                                  self.conversations_panel.chats_list))
        full_names = list(getattr(self.conversations_panel, '_all_chat_names',
                                  self.conversations_panel.chat_names))
        archived_panel = getattr(self, "archived_conversations_panel", None)
        merged_archived_jids = set()
        if archived_panel is not None:
            archived_chats = list(getattr(
                archived_panel, '_all_chats_list', archived_panel.chats_list
            ))
            archived_names = list(getattr(
                archived_panel, '_all_chat_names', archived_panel.chat_names
            ))
            full_chats, full_names, merged_archived_jids = (
                self._conversation_search_candidates(
                    full_chats,
                    full_names,
                    archived_chats,
                    archived_names,
                    include_archived=bool(search),
                )
            )
        # Read back by _build_chat_item_text() below and by
        # refresh_chat_row_text(), so a single-row repaint during a search
        # keeps the suffix the full rebuild gave the row.
        self.conversations_panel._search_archived_jids = merged_archived_jids

        lst = self.conversations_panel.conversations_list

        # Save focused JID before any potential modification (used by both paths).
        focused_idx = lst.GetFocusedItem()
        focused_jid = getattr(self.conversations_panel, '_preserved_focused_jid', None)
        self.conversations_panel._preserved_focused_jid = None  # consume
        if focused_jid is None and focused_idx != -1 and 0 <= focused_idx < len(self.conversations_panel.chats_list):
            focused_jid = self.conversations_panel.chats_list[focused_idx].get("remoteJid")

        # Save currently focused archived chat JID if archived panel is present
        arch_focused_jid = None
        if hasattr(self, "archived_conversations_panel"):
            arch_lst = self.archived_conversations_panel.conversations_list
            arch_focused_idx = arch_lst.GetFocusedItem()
            arch_focused_jid = getattr(self.archived_conversations_panel, '_preserved_focused_jid', None)
            self.archived_conversations_panel._preserved_focused_jid = None  # consume
            if arch_focused_jid is None and arch_focused_idx != -1 and 0 <= arch_focused_idx < len(self.archived_conversations_panel.chats_list):
                arch_focused_jid = self.archived_conversations_panel.chats_list[arch_focused_idx].get("remoteJid")

        # Pre-compute the new display list (filtering + item text) so we can
        # choose between a lightweight SetItem path and a full rebuild.
        _build_item_text = self._build_chat_item_text

        displayed_chats: list = []
        displayed_names: list = []
        new_item_texts: list = []
        for i, chat in enumerate(full_chats):
            name     = full_names[i]
            chat_jid = chat.get("remoteJid", "")
            if conv_filter == 'unread' and effective_unread_count(chat) == 0:
                continue
            if conv_filter == 'groups' and not chat_jid.endswith("@g.us"):
                continue
            if conv_filter == 'individual' and chat_jid.endswith("@g.us"):
                continue
            if search and search not in normalize_for_search(name, _fold):
                continue
            displayed_chats.append(chat)
            displayed_names.append(name)
            new_item_texts.append(_build_item_text(chat, name))

        # ── SetItem path: same JIDs in same order — only text may have changed ──
        # Avoids DeleteAllItems() entirely, keeping scroll position and focus intact.
        # NOTE: chats_list was already overwritten by _apply_chat_lists before this
        # function runs, so we track what's truly rendered via _displayed_jids.
        new_jids = [c.get("remoteJid", "") for c in displayed_chats]

        # Content fingerprint of exactly what this call would render. Used only
        # to skip the per-row GetItemText comparison loop below when nothing
        # changed — it deliberately does NOT short-circuit the whole method.
        #
        # It used to: an equal fingerprint returned from add_chats_to_ui()
        # immediately. But _apply_chat_lists() overwrites panel.chats_list with
        # the *unfiltered* sorted list right before calling this, and the
        # archived panel's rows were never part of the fingerprint at all — so
        # that early return left the backing chats_list and the rows actually
        # on screen describing two different lists, and skipped
        # _refresh_archived_chats_in_ui() entirely. Every lookup that maps a row
        # index back to a chat (activation, context menu, focus restore) then
        # read the wrong entry: reported live as archived groups showing up
        # twice and opening a different group than the one announced.
        _fp = (conv_filter, search, tuple(new_jids), tuple(new_item_texts))
        _fp_unchanged = (_fp == getattr(self, "_chats_ui_fp", None))
        self._chats_ui_fp = _fp

        _displayed_jids = getattr(self.conversations_panel, '_displayed_jids', None)
        _fast_path_ok = False
        if (
            _displayed_jids is not None
            and _displayed_jids == new_jids
            and lst.GetItemCount() == len(new_jids)
        ):
            if _fp_unchanged:
                _fast_path_ok = True  # rows already hold exactly this text
            else:
                try:
                    for idx, new_text in enumerate(new_item_texts):
                        if lst.GetItemText(idx, 0) != new_text:
                            lst.SetItem(idx, 0, new_text)
                    _fast_path_ok = True
                except Exception:
                    # The underlying Win32 list control rejected an index that
                    # GetItemCount() claimed was valid (observed as "Couldn't
                    # retrieve information about list control item N"). Don't
                    # retry SetItem blindly — fall through to the full rebuild
                    # below, which resyncs the control from scratch.
                    pass
        if _fast_path_ok:
            self.conversations_panel.chats_list = displayed_chats
            self.conversations_panel.chat_names = displayed_names
            # _displayed_jids stays the same (JIDs didn't change)
            # Refresh archived panel via the same SetItem logic
            if hasattr(self, "archived_conversations_panel"):
                self._refresh_archived_chats_in_ui(arch_focused_jid)
            return

        # ── Incremental path: the same list with a few rows moved/added/gone ──
        # A new message (or a reaction, or a read receipt) reorders the list —
        # almost always by moving one chat up — which misses the SetItem path
        # above and used to fall straight through to the full rebuild below.
        # That rebuild is a DeleteAllItems() plus one Append() per row, so the
        # cost of showing a new preview scaled with how many chats the account
        # has, and screen readers were handed an entirely new list each time.
        # plan_row_updates() turns "one chat moved" into two native calls
        # regardless of list length; anything it considers too churny returns
        # None and rebuilds as before.
        if (
            _displayed_jids is not None
            and lst.GetItemCount() == len(_displayed_jids)
            and self._apply_chat_rows_incrementally(
                lst, _displayed_jids, new_jids, new_item_texts, focused_jid
            )
        ):
            self.conversations_panel.chats_list = displayed_chats
            self.conversations_panel.chat_names = displayed_names
            self.conversations_panel._displayed_jids = new_jids
            logging.info("[add_chats_to_ui] incremental path applied (%s rows)", len(new_jids))
            if hasattr(self, "archived_conversations_panel"):
                self._refresh_archived_chats_in_ui(arch_focused_jid)
            return

        # ── Full rebuild path: JID order or count changed ────────────────────
        logging.info(
            "[add_chats_to_ui] full rebuild (%s rows; displayed_jids=%s, count=%s)",
            len(new_jids),
            "none" if _displayed_jids is None else len(_displayed_jids),
            lst.GetItemCount(),
        )
        focus_allowed = self._allow_ui_focus_changes()
        _lst_had_focus = (wx.Window.FindFocus() is lst)
        if _lst_had_focus:
            # Set focus to parent panel temporarily to prevent OS from auto-focusing
            # item 0 during DeleteAllItems/Append when the control has focus.
            self.conversations_panel.SetFocus()
        if focused_idx != -1:
            try:
                # Clear focus state before DeleteAllItems to prevent NVDA COMError/freeze
                lst.SetItemState(focused_idx, 0, wx.LIST_STATE_FOCUSED)
            except Exception:
                pass
        if hasattr(self, "archived_conversations_panel"):
            if arch_focused_idx != -1:
                try:
                    self.archived_conversations_panel.conversations_list.SetItemState(
                        arch_focused_idx, 0, wx.LIST_STATE_FOCUSED
                    )
                except Exception:
                    pass
        lst.Freeze()
        try:
            lst.DeleteAllItems()
            for item_text in new_item_texts:
                lst.Append((item_text,))
        finally:
            lst.Thaw()

        # Keep backing lists in sync with exactly what is displayed.
        self.conversations_panel.chats_list = displayed_chats
        self.conversations_panel.chat_names = displayed_names
        self.conversations_panel._displayed_jids = new_jids

        # Restore selection / focus after DeleteAllItems() clears everything.
        # Prefer the previously focused item if it is still in the list to prevent jumping.
        panel = self.conversations_panel
        target_idx = -1
        if focused_jid:
            for i, chat in enumerate(displayed_chats):
                if chat.get("remoteJid") == focused_jid:
                    target_idx = i
                    break

        if target_idx != -1:
            if panel.conversations_list.GetFocusedItem() != target_idx:
                panel.conversations_list.Focus(target_idx)
            if _lst_had_focus:
                if not panel.conversations_list.IsSelected(target_idx):
                    panel.conversations_list.Select(target_idx)
                panel.conversations_list.EnsureVisible(target_idx)
                panel.conversations_list.SetFocus()
            elif panel.conversation is not None:
                if not panel.conversations_list.IsSelected(target_idx):
                    panel.conversations_list.Select(target_idx)
        elif (_lst_had_focus and focused_jid and displayed_chats
              and focus_allowed):
            # The previously focused chat is gone (e.g. it was just cleared and
            # filtered out). Keep keyboard focus in the list by landing on
            # whatever now occupies its slot instead of dropping focus entirely.
            # But only reuse that raw position when this is still the same
            # filtered/searched view — if the filter or search just changed,
            # the old index refers to an unrelated list and landing on row 0
            # is the only position that means anything in the new one.
            if _filter_or_search_changed:
                neighbor_idx = 0
            else:
                neighbor_idx = min(focused_idx, len(displayed_chats) - 1)
            if neighbor_idx < 0:
                neighbor_idx = 0
            panel.conversations_list.Focus(neighbor_idx)
            panel.conversations_list.Select(neighbor_idx)
            panel.conversations_list.EnsureVisible(neighbor_idx)
            panel.conversations_list.SetFocus()
        elif getattr(self, "_initial_sync_running", False):
            # Skip selection/focus restoration during active initial background sync to prevent screen readers loop
            pass
        elif panel.conversation is None and displayed_chats:
            last_jid    = getattr(panel, "_last_open_jid", "")
            target_idx  = 0
            if last_jid:
                for i, chat in enumerate(displayed_chats):
                    if chat.get("remoteJid") == last_jid:
                        target_idx = i
                        break
            if focus_allowed:
                if panel.conversations_list.GetFocusedItem() != target_idx:
                    panel.conversations_list.Focus(target_idx)
                if not panel.conversations_list.IsSelected(target_idx):
                    panel.conversations_list.Select(target_idx)
                panel.conversations_list.EnsureVisible(target_idx)
                # Restore keyboard focus to the list when no conversation is open.
                search = getattr(panel, "search_field", None)
                focused_now = wx.Window.FindFocus()
                if _lst_had_focus or focused_now is None or focused_now is lst:
                    if focused_now is not search:
                        wx.CallAfter(lst.SetFocus)
        elif panel.conversation is not None:
            open_jid = panel.conversation.get("remoteJid", "")
            target_idx = -1
            for i, chat in enumerate(displayed_chats):
                if chat.get("remoteJid") == open_jid:
                    target_idx = i
                    break
            if target_idx != -1:
                if _lst_had_focus:
                    if panel.conversations_list.GetFocusedItem() != target_idx:
                        panel.conversations_list.Focus(target_idx)
                if not panel.conversations_list.IsSelected(target_idx):
                    panel.conversations_list.Select(target_idx)
                panel.conversations_list.EnsureVisible(target_idx)

            if focus_allowed:
                focus_ctrl = getattr(panel, "message_field", None)
                if focus_ctrl and focus_ctrl.IsShownOnScreen():
                    if wx.Window.FindFocus() is None and self.IsActive():
                        wx.CallAfter(focus_ctrl.SetFocus)

        # Also refresh the archived panel if present
        if hasattr(self, "archived_conversations_panel"):
            self._refresh_archived_chats_in_ui(arch_focused_jid)
