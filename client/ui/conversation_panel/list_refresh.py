"""ListRefreshMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import logging
import time
import wx

from ui.conversation_panel.typing_row import (
    append_message_row,
    dismiss_typing_row_for_message,
    message_row_count,
    sync_typing_row,
)


class ListRefreshMixin:
    """Keeping the message list in sync with the data: status repaints, incoming
    messages, signature-based repaint and populate_messages.
    """

    # Delivery receipts do not arrive one at a time. When the other side opens
    # the chat, WhatsApp sends a READ receipt for every message we ever sent in
    # it at once — ten in a single second, measured in a real session. Each one
    # used to rewrite its row immediately, and on Windows every SetItemText
    # raises a name-change event on that ListView item: a screen reader reading
    # a message gets interrupted, ten times in a row, for rows the user is not
    # even on. That is the "a fala e cortada e algum item da lista muda"
    # report, and it is the exact flood CLAUDE.md's Freeze/Thaw rule exists to
    # prevent. So the burst is coalesced into one pass on a short timer.
    _STATUS_REPAINT_COALESCE_MS = 120

    def refresh_message_status(self, msg_id: str, status: str):
        """Queue a status-icon repaint for one sent message.

        Never repaints synchronously and never rebuilds the list — see
        _flush_status_repaints() for why the delay exists.
        """
        pending = getattr(self, "_pending_status_repaints", None)
        if pending is None:
            pending = self._pending_status_repaints = set()
        pending.add(msg_id)

        timer = getattr(self, "_status_repaint_timer", None)
        if timer is not None and timer.IsRunning():
            # Still inside the burst — let the running timer pick this up too.
            return
        self._status_repaint_timer = wx.CallLater(
            self._STATUS_REPAINT_COALESCE_MS, self._flush_status_repaints
        )

    def _flush_status_repaints(self):
        """Repaint every row queued since the last flush, as one batch.

        Rows whose rendered text did not actually change are skipped entirely.
        That is not just an optimisation: a no-op SetItemText still raises the
        accessibility event, so writing text identical to what is already there
        interrupts a screen reader for literally no reason. A receipt for a
        message whose row is off the current page, or whose mark did not move,
        is exactly that case.
        """
        self._status_repaint_timer = None
        pending = getattr(self, "_pending_status_repaints", None)
        if not pending:
            return

        ids = set(pending)
        pending.clear()
        # A row parked earlier gets another look on every pass: it may no
        # longer be the one with focus.
        guard = getattr(self, "_chain_repaint_guard", None)
        if guard is not None:
            ids |= guard.take_parked()

        rows = []
        for i, msg in enumerate(self._sorted_messages):
            if self._is_separator(msg):
                continue
            if msg.get("key", {}).get("id") in ids:
                rows.append((i, msg))

        # While a voice-note sequence runs, the row that has list focus must
        # not be written, and neither must the one the sequence has just moved
        # focus off — see _release_chain_held_repaints() for the whole
        # reasoning. Only those are parked (core/chain_repaint_guard.py); every
        # other row of the conversation is written as usual.
        if guard is not None and guard.armed and rows:
            focused = self.messages_list.GetFocusedItem()
            writable = []
            for i, msg in rows:
                msg_id = msg.get("key", {}).get("id")
                if guard.must_park(msg_id, has_focus=(i == focused)):
                    guard.parked.add(msg_id)
                else:
                    writable.append((i, msg))
            rows = writable
        if not rows:
            return

        # NOTE: MessageUpdate was already appended by on_message_status_update
        # in main.py before this method is called. Do NOT append again here or
        # the status history grows with duplicates on every update.
        self.messages_list.Freeze()
        try:
            for i, msg in rows:
                line = self._render_message_line(msg)
                try:
                    if self.messages_list.GetItemText(i) == line:
                        continue
                except Exception:
                    pass
                self.messages_list.SetItemText(i, line)
                # RefreshItem ensures the list control repaints this row.
                # Without it, SetItemText updates the internal data but Windows
                # may defer the visual update until the next full paint cycle —
                # making the status icon appear frozen until the user leaves and
                # re-enters the conversation.
                try:
                    self.messages_list.RefreshItem(i)
                except Exception:
                    pass
        finally:
            self.messages_list.Thaw()

    def _hold_status_repaints_until_chain_ends(self):
        """Arm the hold. Called the moment we know the chain is about to move
        list focus off the row we are about to mark as played."""
        guard = getattr(self, "_chain_repaint_guard", None)
        if guard is not None:
            guard.arm()

    def _chain_moving_focus_off(self, msg_id: str):
        """The chain is about to move list focus off this row: it stays parked
        until the sequence is over, focused or not. Call it BEFORE the Focus()
        call, so no repaint can slip in between the two."""
        guard = getattr(self, "_chain_repaint_guard", None)
        if guard is not None:
            guard.focus_moved_off(msg_id)

    def _recheck_parked_chain_repaints(self):
        """List focus moved: a row parked only because it had focus is owed.

        Queued behind the usual coalescing timer rather than written here, so
        the write does not land on top of the focus move itself; a row that
        still has focus, or that the chain moved focus off, is parked again by
        that pass."""
        guard = getattr(self, "_chain_repaint_guard", None)
        if guard is None:
            return
        for msg_id in guard.take_parked():
            self.refresh_message_status(msg_id, "")

    def _release_chain_held_repaints(self):
        """Write out every row repaint held back while the audio chain ran.

        Only the row with list focus and the rows the chain moved focus off
        are held (see core/chain_repaint_guard.py): a receipt for any other
        message of the conversation is written when it arrives, sequence or
        not.

        Why the hold exists at all — the rule comes straight from NVDA's own
        source. ``NVDAObject.event_nameChange`` is::

            def event_nameChange(self):
                if self is api.getFocusObject():
                    speech.speakObjectProperties(self, name=True, reason=CHANGE)

        A wx.ListCtrl row is one MSAA object whose *name* is the whole rendered
        line, so rewriting a row to add "reproduzido" raises a name change, and
        NVDA speaks the **entire row** — but only when that row is the object it
        currently believes has focus. So the fix is not to time the write, it is
        to never write the row NVDA is looking at while focus is moving away
        from it.

        The previous protection tried to order the two events instead: move
        focus, then fire the "played" refresh from the same callback,
        documented as "this can't lose the race because both actions run in the
        same callback, in this order". It could, for two measured reasons:

        * ``refresh_message_status()`` does not write anything — it queues the
          row and starts a 120 ms coalescing timer. Measured on the real code
          path, the write landed 142 ms *after* the focus move. That is a
          margin, not a guarantee, and users on several machines with current
          NVDA reported it losing.
        * Worse, that margin can go negative. ``mark_audio_message_played()``
          also POSTs a played receipt to WhatsApp, which echoes the same status
          back over Socket.IO onto ``on_message_status_update()`` with
          ``skip_panel_refresh=False`` — bypassing the chain protection
          entirely. Measured on the real code path: the row was written 95 ms
          *before* the focus move, so NVDA read the whole finished row out and
          only then announced the newly focused one. That is exactly the
          reported symptom, and it is a hole the ordering approach cannot cover
          because the second write does not come through the chain at all.

        Holding removes both: during the chain the row it leaves is not
        written, so there is no event to lose a race with. At release time
        focus sits on the last voice note of the sequence while every row the
        chain left is an earlier one, so each name change lands on a
        non-focused object and NVDA stays silent by its own rule — no timing
        assumption anywhere. (A row parked because the user was on it is read
        when it is written at release, if they still are: the sequence is over
        or paused by then, so it is the ordinary feedback of a status change
        on the focused row, not speech over the audio.)

        Idempotent: the guard is disarmed first, so the several places that
        can end a sequence (the last voice note, the user stopping or pausing
        playback, leaving the conversation) may all call this without writing
        the rows twice. It does NOT check whether the chain is still running —
        call it only where no focus move is coming: once the sequence is
        genuinely over, which is why most call sites sit next to where
        _is_in_audio_chain is cleared, or when playback is paused (the next
        voice note to end arms the hold again).
        """
        guard = getattr(self, "_chain_repaint_guard", None)
        if guard is None:
            return
        held = guard.release()
        if not held:
            return
        pending = getattr(self, "_pending_status_repaints", None)
        if pending is None:
            pending = self._pending_status_repaints = set()
        pending.update(held)
        # Straight to the write rather than through refresh_message_status():
        # the chain is over, there is no focus move left to stay clear of, and
        # another 120 ms of coalescing would only leave the rows stale for
        # longer. A timer already in flight is cancelled so it cannot fire a
        # second, empty flush.
        timer = getattr(self, "_status_repaint_timer", None)
        if timer is not None:
            try:
                timer.Stop()
            except Exception:
                pass
            self._status_repaint_timer = None
        self._flush_status_repaints()

    def refresh_active_conversation_messages(self, jids=None) -> int:
        """Re-render messages in the active message list (useful after
        background name/LID resolution). Returns how many rows it repainted.

        *jids* narrows the work to the rows whose text can depend on those
        JIDs. The message window has had no ceiling since it started
        preserving the history the user loads with Home, so a full pass is
        thousands of _render_message_line() calls once per resolved batch —
        while a batch typically renames one person. None (the default) keeps
        the original behaviour of re-rendering everything, and every path that
        cannot say with certainty which rows changed falls back to it.
        """
        if not self.conversation or not hasattr(self, "messages_list"):
            return 0
        target_ids = self._message_ids_touching_jids(jids) if jids else None
        # Mesma guarda de _repaint_message_rows(): _set_message_row_texts()
        # escreve por índice, então uma lista fora de passo com o controle põe
        # o texto certo na linha errada — e um descompasso só de prefixo não
        # levanta exceção nenhuma, o leitor de tela simplesmente passa a ler a
        # mensagem trocada. Degrada para o passe completo, que percorre as duas
        # em paralelo e no máximo pinta linhas a mais.
        if (target_ids is not None
                and message_row_count(self) != len(self._sorted_messages)):
            logging.info(
                "[refresh_active_conversation_messages] list out of step with rows "
                "— full path")
            target_ids = None
        # Um SetItemText por linha é um evento de acessibilidade por linha, e
        # os lotes de resolução de nomes/LID chamam isto repetidamente sobre a
        # lista inteira — sem congelar, o leitor de tela recebe a enxurrada e a
        # janela trava por segundos (ver as notas do watchdog em main.py).
        self.messages_list.Freeze()
        try:
            if target_ids is not None:
                # Reuses the same SetItemText loop the selection-marker
                # refresh goes through; it already renders with an explicit
                # index/total.
                return len(self._set_message_row_texts(target_ids))
            painted = 0
            failed = 0
            total = len(self._sorted_messages)
            for i, msg in enumerate(self._sorted_messages):
                if not self._is_separator(msg):
                    # Per row, not around the loop: a single malformed record
                    # used to abort the whole pass, so every row after it kept
                    # its old text — one bad message turning into a whole
                    # conversation that stops being repainted. Only the first
                    # traceback is logged, since this runs on a timer and a
                    # permanently bad record would otherwise fill log.log.
                    try:
                        # index/total explícitos como em _set_message_row_texts():
                        # sem eles o modo listbox com contagem de itens cai no
                        # fallback self._sorted_messages.index(msg), uma varredura
                        # linear com comparação profunda de dicts por linha — e duas
                        # mensagens de mesmo conteúdo anunciam a posição errada.
                        self.messages_list.SetItemText(
                            i, self._render_message_line(msg, index=i, total=total)
                        )
                    except Exception:
                        if not failed:
                            logging.exception(
                                "[refresh_active_conversation_messages] row %d "
                                "failed to render; skipping it and continuing.", i)
                        failed += 1
                    else:
                        painted += 1
            if failed > 1:
                logging.warning(
                    "[refresh_active_conversation_messages] %d of %d rows failed "
                    "to render.", failed, total)
            return painted
        finally:
            self.messages_list.Thaw()

    # ── Real-time incoming message ────────────────────────────────────────────

    def _matches_open_conversation(self, remote_jid: str) -> bool:
        """True when remote_jid addresses the conversation currently open.

        Tolerates the @lid/phone duality in both directions: a live event may
        arrive under either form regardless of which one the open conversation
        was loaded under. Also tolerates Brazilian 9th-digit variations and
        unnormalized JIDs.
        """
        if self.conversation is None or not remote_jid:
            return False
        conv_jid = self.conversation.get("remoteJid", "")
        if not conv_jid:
            return False
        if conv_jid == remote_jid:
            return True

        mw = getattr(self, "main_window", None)
        norm_conv = mw._normalize_jid(conv_jid) if mw and hasattr(mw, "_normalize_jid") else conv_jid
        norm_remote = mw._normalize_jid(remote_jid) if mw and hasattr(mw, "_normalize_jid") else remote_jid
        if norm_conv == norm_remote:
            return True

        c_digits, _, c_dom = norm_conv.partition("@")
        r_digits, _, r_dom = norm_remote.partition("@")
        if (
            mw
            and hasattr(mw, "_phone_digits_equivalent")
            and c_dom
            and c_dom == r_dom
            and c_dom in ("s.whatsapp.net", "c.us")
            and mw._phone_digits_equivalent(c_digits, r_digits)
        ):
            return True

        phone_to_lid = getattr(mw, "_phone_to_lid", {}) if mw else {}
        lid_to_phone = getattr(mw, "_lid_to_phone", {}) if mw else {}

        candidates = {
            conv_jid,
            norm_conv,
            phone_to_lid.get(conv_jid, ""),
            phone_to_lid.get(norm_conv, ""),
            lid_to_phone.get(conv_jid, ""),
            lid_to_phone.get(norm_conv, ""),
        }
        targets = {
            remote_jid,
            norm_remote,
            phone_to_lid.get(remote_jid, ""),
            phone_to_lid.get(norm_remote, ""),
            lid_to_phone.get(remote_jid, ""),
            lid_to_phone.get(norm_remote, ""),
        }
        candidates.discard("")
        targets.discard("")
        if candidates & targets:
            return True

        if mw and hasattr(mw, "_phone_digits_equivalent"):
            for c in candidates:
                for t in targets:
                    cd, _, cdom = c.partition("@")
                    td, _, tdom = t.partition("@")
                    if cdom and cdom == tdom and cdom in ("s.whatsapp.net", "c.us"):
                        if mw._phone_digits_equivalent(cd, td):
                            return True

        return False

    def on_incoming_message(self, remote_jid: str, msg: dict):
        """
        Called (on the main thread) when a new message arrives via WebSocket.
        If the conversation matching remote_jid is currently open, appends the
        message to the list; otherwise does nothing (the unread badge in the
        conversations list is updated separately via set_chats).
        """
        # Reactions are handled BEFORE the "is this conversation open?" guard
        # below — unlike a normal message, a reaction still has to be recorded
        # for a chat the user is not currently looking at. See
        # apply_incoming_reaction().
        if msg.get("messageType") == "reactionMessage":
            self.apply_incoming_reaction(remote_jid, msg)
            return  # Don't add reaction as a separate row

        if self.conversation is None:
            return

        if not self._matches_open_conversation(remote_jid):
            return

        # Get the top visible item before inserting the message
        top_msg_id = None
        top_idx = -1
        if getattr(self.main_window, "_allow_ui_focus_changes", lambda: False)():
            if hasattr(self.messages_list, "GetTopItem"):
                top_idx = self.messages_list.GetTopItem()
            else:
                try:
                    import ctypes
                    hwnd = self.messages_list.GetHandle()
                    top_idx = ctypes.windll.user32.SendMessageW(hwnd, 0x018E, 0, 0)
                except Exception:
                    pass
            if top_idx != -1 and 0 <= top_idx < len(self._sorted_messages):
                m = self._sorted_messages[top_idx]
                if not self._is_separator(m):
                    top_msg_id = m.get("key", {}).get("id", "")
        # Avoid duplicates
        msg_id = msg.get("key", {}).get("id", "")
        if msg_id:
            for existing in self._sorted_messages:
                if self._is_separator(existing):
                    continue
                if existing.get("key", {}).get("id", "") == msg_id:
                    return

        # A delayed/replayed event is not necessarily a new tail row. The
        # backend already stored it; let the existing debounced rebuild put it
        # at its timestamp without adding a new-unread separator or scrolling
        # to an old message. Bursts still cost one focus-preserving rebuild.
        tail = next((m for m in reversed(self._sorted_messages)
                     if not self._is_separator(m)), None)
        # A row this client sent carries the PC clock's stamp, not the
        # server's; with the clock ahead, a reply right after it would look
        # late. Compare against the last server-stamped row instead.
        stamped_tail = next((m for m in reversed(self._sorted_messages)
                             if not self._is_separator(m)
                             and not m.get("_local_id")), None)
        if (stamped_tail is not None and (self._extract_timestamp(msg) or 0)
                < (self._extract_timestamp(stamped_tail) or 0)):
            self.main_window._schedule_refresh_messages()
            return

        # Batch all list operations so the screen reader receives a single
        # accessibility event rather than one per insertion/update.
        from_me = bool(msg.get("key", {}).get("fromMe"))
        # Capture alignment before the unread separator can become the last
        # visible row. The pagination list holds messages, not separators.
        all_sorted = getattr(self, "_all_sorted_messages", [])
        pagination_in_step = bool(
            all_sorted and all_sorted is not self._sorted_messages
            and all_sorted[-1] is tail
        )
        self.messages_list.Freeze()
        try:
            # Manage unread separator — never for our OWN messages. This
            # branch also runs for the WebSocket echo of a message we just
            # sent (when it isn't matched to its optimistic pending row by
            # main.py's by-type matching, e.g. sent from another linked
            # device) — an own message is never "unread", the same
            # principle first_unread_index() already applies when placing
            # the separator on conversation open. Without this guard, that
            # echo could insert/relocate a separator directly above the
            # user's own just-sent message, which is what made Alt+2 ("jump
            # to last message") land on a stale earlier separator/row
            # instead of the message the user actually just sent.
            if not from_me and self._counts_toward_unread_separator(msg):
                self._update_unread_separator_for_incoming(msg)

            # Append the real message (focus must NOT move)
            self._clear_empty_placeholder()
            # The local pagination offset reads _all_sorted_messages. Keep its
            # tail in step too, or scrolling up can hit an already-loaded DB
            # page and ask the server before local history is exhausted.
            if pagination_in_step:
                all_sorted.append(msg)
            self._sorted_messages.append(msg)
            append_message_row(self, self._render_message_line(msg))
            # What the sender was typing is here now: take them off the
            # typing row (which stays below, for anyone else still typing).
            if not from_me:
                dismiss_typing_row_for_message(self, msg)
        finally:
            self.messages_list.Thaw()

        # Only scroll while WinZapp is already active; incoming notifications
        # must never move focus or alter the user's current foreground context.
        if getattr(self.main_window, "_allow_ui_focus_changes", lambda: False)():
            scrolled = False
            if top_msg_id:
                is_near_bottom = False
                last_idx_before = len(self._sorted_messages) - 2
                if last_idx_before - top_idx < 15:
                    is_near_bottom = True
                
                if not is_near_bottom:
                    for idx, msg in enumerate(self._sorted_messages):
                        if isinstance(msg, dict) and msg.get("key", {}).get("id") == top_msg_id:
                            self.messages_list.EnsureVisible(idx)
                            scrolled = True
                            break
            
            if not scrolled:
                # The very bottom on purpose, typing row included: when it is
                # still there someone else is typing, and it sits right below
                # the message that just arrived.
                last = self.messages_list.GetItemCount() - 1
                if last >= 0:
                    self.messages_list.EnsureVisible(last)

    def navigate_to_jid(self, jid: str) -> bool:
        """Select and open the conversation matching jid, clearing any search.

        Returns whether a row for *jid* was found; the caller decides what to
        do about a person with no conversation yet."""
        # Clear search so all chats are visible
        if self.search_field.GetValue():
            self.search_field.SetValue("")
            self.main_window.add_chats_to_ui()

        # Find the chat index and activate it
        for i, chat in enumerate(self.chats_list):
            if chat.get("remoteJid", "") == jid:
                self.conversations_list.Focus(i)
                self.conversations_list.Select(i)
                self.conversations_list.EnsureVisible(i)
                self.navigate_to_conversation(chat)
                return True
        return False

    # ── Populate ─────────────────────────────────────────────────────────────

    def _clear_populating_messages_flag(self):
        self._populating_messages = False

    def _messages_signature(self):
        """Cheap fingerprint of everything ``populate_messages()`` would render.

        Deliberately built from the raw records rather than from rendered rows:
        it has to be cheap enough to run on every background refresh, and every
        field that can change a row's text is covered here (body, status,
        star/edit markers, reactions arrive as their own records, and the
        separator position is pinned by ``_first_unread_msg_id``).
        """
        conv = self.conversation or {}
        records = []
        container = conv.get("messages")
        if isinstance(container, dict):
            inner = container.get("messages")
            if isinstance(inner, dict) and isinstance(inner.get("records"), list):
                records = inner["records"]
        sig = []
        for m in records:
            if not isinstance(m, dict):
                continue
            key = m.get("key") or {}
            sig.append((
                key.get("id", ""),
                m.get("messageType", ""),
                m.get("status", ""),
                bool(m.get("starred")),
                bool(m.get("pinInChat")),
                bool(m.get("_edited")),
                bool(m.get("_local_pending")),
                self._extract_timestamp(m) or 0,
                self._get_message_content(m) or "",
            ))
        return (
            conv.get("remoteJid", ""),
            self._first_unread_msg_id,
            self._pending_open_unread,
            tuple(sig),
        )

    @staticmethod
    def _signature_changed_ids(old, new):
        """Which message ids differ between two _messages_signature() snapshots.

        Returns None when the difference isn't expressible as "these rows
        changed": a different conversation, a moved unread separator, or an id
        that is empty/repeated in either snapshot (which makes the per-id
        comparison below ambiguous). Those change which row sits where, so no
        per-row repaint can stand in for a rebuild.
        """
        if not (isinstance(old, tuple) and isinstance(new, tuple)):
            return None
        if len(old) != 4 or len(new) != 4 or old[:3] != new[:3]:
            return None
        old_rows = {r[0]: r for r in old[3]}
        new_rows = {r[0]: r for r in new[3]}
        if len(old_rows) != len(old[3]) or len(new_rows) != len(new[3]):
            return None
        if "" in old_rows or "" in new_rows:
            return None
        return {
            mid for mid in set(old_rows) | set(new_rows)
            if old_rows.get(mid) != new_rows.get(mid)
        }

    def _adopt_signature_after_repaint(self, msg_ids: set) -> None:
        """Move refresh_messages_if_changed()'s fingerprint forward after rows
        were repainted in place.

        populate_messages() snapshots the fingerprint on its way out, so a
        local change used to land in the cache as a side effect of rebuilding.
        Repainting instead leaves the cache describing the state *before* the
        change, and the next background refresh would find a mismatch and
        rebuild the whole list — moving the user's focus for something already
        correct on screen. Adopting the new fingerprint unconditionally would
        be worse: anything else that changed in `records` since the last
        rebuild would be swallowed and never rendered. So it's adopted only
        when the rows that differ are the ones just repainted.
        """
        try:
            new_sig = self._messages_signature()
        except Exception:
            logging.exception("[_adopt_signature_after_repaint] signature failed")
            self._messages_signature_cache = None
            return
        changed = self._signature_changed_ids(
            getattr(self, "_messages_signature_cache", None), new_sig
        )
        if changed is not None and changed <= set(msg_ids):
            self._messages_signature_cache = new_sig

    def _repaint_message_rows(self, msg_ids) -> bool:
        """Repaint the rows of *msg_ids* in place instead of rebuilding the
        list. Returns whether every requested row was found and repainted;
        callers fall back to a full rebuild when it returns False.

        Starring, pinning and a remote "delete for everyone" each change the
        text of rows already on screen and nothing else: the list is sorted by
        timestamp, which none of them touch, so no row moves, appears or
        disappears (a revoked message keeps its row — see
        _is_displayable_message()). populate_messages() nevertheless re-sorts
        and de-duplicates every record in the conversation, rebuilds the
        reaction map, recomputes the unread separator and the pagination
        window, then DeleteAllItems() + Append()s every row — and hands the
        screen reader a whole new list in the process (CLAUDE.md's
        Freeze()/Thaw() note). Starring a selection of messages in a long
        conversation is the visible case. Same idea as main.py's
        refresh_chat_row_text() for the conversations list.
        """
        ids = {i for i in (msg_ids or ()) if i}
        if not ids or not self._sorted_messages:
            return False
        # Backing list out of step with the control means a targeted
        # SetItemText would write the right text into the wrong row.
        if message_row_count(self) != len(self._sorted_messages):
            logging.info("[_repaint_message_rows] list out of step with rows — full path")
            return False
        try:
            found = self._set_message_row_texts(ids)
        except Exception:
            logging.exception("[_repaint_message_rows] failed — full path")
            return False
        if found != ids:
            # Something asked for isn't rendered: paginated out of the current
            # window, or replaced by a resync while a server call was in
            # flight. The rebuild is the only thing that can show it.
            logging.info("[_repaint_message_rows] %d of %d rows not rendered — full path",
                         len(ids - found), len(ids))
            return False
        self._adopt_signature_after_repaint(ids)
        return True

    def _sorted_deduped_records(self, messages: list) -> list:
        """The record list ``populate_messages()`` renders from: sorted by
        timestamp, then de-duplicated by key.id keeping the LAST occurrence.

        Extracted from that method verbatim so the in-place repaint path below
        can derive the same reaction map the rebuild would, rather than a second
        opinion about it. Records accumulate duplicates when the same message
        arrives via both the initial sync and messages.upsert; the latest
        version of the message wins. A record with no id is never a duplicate of
        anything and is always kept.
        """
        try:
            messages_sorted = sorted(
                messages, key=lambda m: self._extract_timestamp(m) or 0
            )
        except Exception:
            messages_sorted = messages
        _seen_ids: dict = {}
        for i, m in enumerate(messages_sorted):
            if not isinstance(m, dict):
                continue
            mid = m.get("key", {}).get("id", "")
            if mid:
                _seen_ids[mid] = i
        _kept = set(_seen_ids.values())
        return [
            m for i, m in enumerate(messages_sorted)
            if isinstance(m, dict) and (
                not m.get("key", {}).get("id", "") or i in _kept
            )
        ]

    def _reaction_map_from_sorted(self, messages_sorted: list) -> dict:
        """Build the reaction map from an already sorted+deduped record list.

        Each sender can only have ONE active reaction on a message at a time —
        later records for the same (message, sender) pair replace the earlier
        one instead of accumulating a count, and an empty emoji means that
        sender removed their reaction. Order therefore matters, which is why
        this takes the sorted list rather than raw records.
        """
        reaction_map: dict = {}
        for m in messages_sorted:
            if isinstance(m, dict) and m.get("messageType") == "reactionMessage":
                reaction   = (m.get("message") or {}).get("reactionMessage") or {}
                emoji      = reaction.get("text", "")
                orig_id    = (reaction.get("key") or {}).get("id", "")
                sender_key = self._reactor_key_from_msg(m)
                if orig_id and sender_key:
                    per_msg = reaction_map.setdefault(orig_id, {})
                    if emoji:
                        per_msg[sender_key] = emoji
                    else:
                        per_msg.pop(sender_key, None)
        return reaction_map

    @staticmethod
    def _reaction_target_id(msg: dict) -> str:
        """The id of the message a reaction record decorates, or ""."""
        if not isinstance(msg, dict) or msg.get("messageType") != "reactionMessage":
            return ""
        reaction = (msg.get("message") or {}).get("reactionMessage") or {}
        return (reaction.get("key") or {}).get("id", "") or ""

    def _repaint_changed_rows_in_place(self, old_sig, new_sig) -> bool:
        """Rewrite only the rows whose text actually changed, instead of
        rebuilding the whole list. Returns whether the entire difference between
        the two signatures was covered.

        This is the rung that was missing between _append_new_tail_rows() (rows
        added at the END) and the full rebuild, and its absence is what the 60s
        poll was landing on. Two very ordinary things change a row that is
        already on screen without adding or removing any row:

        * a delivery/read receipt moving a message's ``status``;
        * a reaction, which is a record that never becomes a row of its own and
          instead changes the text of ANOTHER row.

        Neither is expressible as a tail append, so both fell through to
        ``populate_messages(preserve_focus=True)`` — DeleteAllItems() plus one
        Append() per row, followed by re-Focus()/re-Select()ing the row the user
        was already on. That last part is the damage: a native ListView row is a
        single MSAA object, so re-focusing it fires EVT_LIST_ITEM_FOCUSED and
        the screen reader re-announces a row the user never moved off, once a
        minute, mid-read. It cannot be fixed by making the rebuild quieter —
        the focus event is unavoidable once the control has been cleared — so
        the rebuild has to not happen.

        Refuses anything that moves, adds or removes a ROW, because only the
        rebuild knows where a row goes:

        * a changed record whose timestamp moved (the list is sorted by
          timestamp, so it may belong somewhere else now);
        * a displayable record added or removed (that is a row appearing or
          disappearing — _append_new_tail_rows() owns the tail case);
        * a reaction whose target is not currently rendered (paginated out, or
          in another conversation) — there is no row to repaint;
        * everything _signature_changed_ids() already refuses on its own: a
          different conversation, a moved unread separator, an empty or
          repeated id;
        * a list out of step with the control, or the placeholder list, on the
          same reasoning as _repaint_message_rows().
        """
        if self.conversation is None or not self._sorted_messages:
            return False
        changed = self._signature_changed_ids(old_sig, new_sig)
        if not changed:
            # None (not comparable) and the empty set (nothing to do, which
            # refresh_messages_if_changed() would not have called us for) both
            # belong to the caller's slower path.
            return False
        old_rows = {r[0]: r for r in old_sig[3]}
        new_rows = {r[0]: r for r in new_sig[3]}

        records = []
        container = self.conversation.get("messages")
        if isinstance(container, dict):
            inner = container.get("messages")
            if isinstance(inner, dict) and isinstance(inner.get("records"), list):
                records = inner["records"]
        by_id = {}
        for m in records:
            if isinstance(m, dict):
                mid = (m.get("key") or {}).get("id", "")
                if mid:
                    by_id[mid] = m

        rendered = {}
        for idx, m in enumerate(self._sorted_messages):
            if isinstance(m, dict) and not self._is_separator(m):
                mid = (m.get("key") or {}).get("id", "")
                if mid:
                    rendered[mid] = idx

        targets = set()
        for mid in changed:
            before, after = old_rows.get(mid), new_rows.get(mid)
            if before is not None and after is not None:
                # Index 7 of the signature tuple is the timestamp; a row whose
                # sort key moved may not belong where it currently sits.
                if before[7] != after[7]:
                    return False
                if mid in rendered:
                    targets.add(mid)
                    continue
                # Not a row of its own: only a reaction may legitimately be
                # invisible, and only if what it decorates is on screen.
                target = self._reaction_target_id(by_id.get(mid) or {})
                if target and target in rendered:
                    targets.add(target)
                    continue
                return False
            # Added or removed outright.
            record = by_id.get(mid)
            if record is None:
                # Gone from `records`: either a row disappeared, or a reaction
                # was withdrawn. Both need the rebuild — a withdrawn reaction
                # changed some other row's text and there is nothing left to
                # read the target off, so it cannot be repainted either.
                return False
            if self._is_displayable_message(record):
                return False        # a row appears — not ours to place
            target = self._reaction_target_id(record)
            if not (target and target in rendered):
                return False
            targets.add(target)

        if not targets:
            return False
        if message_row_count(self) != len(self._sorted_messages):
            logging.info("[_repaint_changed_rows_in_place] list out of step with rows — full path")
            return False
        first_row = self._sorted_messages[0]
        if isinstance(first_row, dict) and first_row.get("_type") == "empty_placeholder":
            return False

        # The reaction map has to move first: _render_message_line() reads it,
        # so repainting before rebuilding it would write the OLD reaction back
        # into the row that just changed.
        try:
            self._reaction_map = self._reaction_map_from_sorted(
                self._sorted_deduped_records(records)
            )
            found = self._set_message_row_texts(targets)
        except Exception:
            logging.exception("[_repaint_changed_rows_in_place] failed — full path")
            return False
        if found != targets:
            logging.info("[_repaint_changed_rows_in_place] %d of %d rows not rendered — full path",
                         len(targets - found), len(targets))
            return False
        self._messages_signature_cache = new_sig
        logging.info(
            "[_repaint_changed_rows_in_place] %d changed record(s) -> %d row(s) "
            "repainted, %d row(s) total — no rebuild.",
            len(changed), len(targets), len(self._sorted_messages),
        )
        return True

    def _repaint_or_repopulate(self, msg_ids) -> None:
        """Repaint just the rows of *msg_ids*, rebuilding the list only if
        that isn't possible. The shape every local flag change uses."""
        if not self._repaint_message_rows(msg_ids):
            self.populate_messages(preserve_focus=True)

    def _row_position_suffix_active(self) -> bool:
        """Se cada linha carrega o sufixo ", N de M" (modo listbox com a
        contagem de itens ligada).

        Importa para quem acrescenta linha em vez de reconstruir: acrescentar
        muda o M de TODAS as linhas já renderizadas, e só o rebuild re-renderiza
        todas. Com o sufixo ligado, o caminho incremental deixaria a lista
        inteira anunciando um total velho ao leitor de tela — pior que a
        lentidão que ele evita.
        """
        if getattr(self, "_message_list_mode", "classic") != "listbox":
            return False
        mw = getattr(self, "main_window", None)
        settings = getattr(mw, "settings", None)
        if not isinstance(settings, dict):
            return False
        return bool(settings.get("user_interface", {}).get("show_listbox_item_count", False))

    def _append_new_tail_rows(self, old_sig, new_sig) -> bool:
        """Renderiza mensagens que só chegaram no FIM da conversa acrescentando
        as linhas delas, em vez de reconstruir a lista. Devolve se a diferença
        inteira entre as duas assinaturas foi coberta assim; quem chama
        reconstrói quando não foi.

        É o caso mais comum que sobrou passando pelo rebuild: mensagem nova. O
        painel já a acrescenta ao vivo em on_incoming_message(), mas o refresh
        de fundo que vem segundos depois (sync_chat_messages() ->
        _refresh_open_conversation_after_sync(), o backfill de histórico, a
        rodada de 60s) só sabia comparar a assinatura e chamar
        populate_messages(): DeleteAllItems() mais um Append() por linha, para
        pintar de novo o que já estava na tela — numa janela que agora pode ter
        milhares de linhas. Mesma ideia de _repaint_message_rows(), que já faz
        isso para estrela/fixar/apagar, e de refresh_chat_row_text() na lista de
        conversas.

        Na prática o caminho normal acrescenta ZERO linha: a mensagem já foi
        pintada ao vivo, e o que este método faz é reconhecer isso e adotar a
        assinatura, transformando o rebuild seguinte em nada. Acrescentar de
        fato é o caminho de quem chegou pelo sync sem passar pelo live.

        Recusa tudo que não seja "linhas novas no fim, nada mais mudou", porque
        aí o rebuild é a única coisa que sabe onde a linha vai:

        - qualquer linha existente que mudou de texto ou sumiu (a comparação
          por id de _signature_changed_ids(), que já devolve None sozinha para
          conversa trocada, separador movido ou id vazio/repetido);
        - registro novo que não vira linha — a reação é o caso comum: ela muda
          o texto de OUTRA linha, e a assinatura não diz de qual;
        - registro novo mais antigo que a última linha, que entraria no meio da
          lista ordenada por timestamp, não no fim;
        - qualquer reordenação dos registros antigos, que a comparação por id
          não veria e o sort estável do rebuild veria;
        - lista fora de passo com o controle, lista vazia (acrescentar na lista
          vazia com foco reproduz o pulo de foco para a linha 0 que o Freeze()
          de populate_messages() documenta) e a lista de placeholder;
        - o sufixo ", N de M" ligado (ver _row_position_suffix_active()).

        Uma exceção à recusa por registro não exibível: reação a um status
        NOSSO é linha de verdade (_is_displayable_message() ->
        reaction_targets_status()), então ela é acrescentada como qualquer
        mensagem. O rebuild também a poria em _reaction_map, e o atalho não —
        sem divergência visível, porque essa entrada é chaveada por um id de
        status@broadcast, que não tem linha nesta conversa para decorar.

        Consequência deliberada, não efeito colateral: o separador de não
        lidas que on_incoming_message() insere ao vivo sobrevive a este
        refresh, e é seguro porque só se acrescenta na cauda: _unread_sep_idx
        continua apontando para a mesma linha e _dismiss_unread_separator()
        continua funcionando. A assimetria que existia aqui — o separador ao
        vivo era removido pelo primeiro rebuild que qualquer OUTRA mudança
        provocasse, porque este caminho não gravava _first_unread_msg_id — foi
        fechada do outro lado: _update_unread_separator_for_incoming() grava a
        âncora e a contagem, e _place_unread_separator_for_rebuild() as lê de
        volta.
        """
        if self.conversation is None or not self._sorted_messages:
            return False
        changed = self._signature_changed_ids(old_sig, new_sig)
        if changed is None:
            return False
        old_rows, new_rows = old_sig[3], new_sig[3]
        # Crescimento no fim, e nada mais: os registros antigos têm de continuar
        # lá, iguais e na mesma ordem. Comparar só os conjuntos de id deixaria
        # passar uma reordenação pura, e ela não é inócua — populate_messages()
        # ordena por timestamp com sort estável, então duas mensagens de mesmo
        # timestamp trocam de lugar no rebuild e a lista na tela deixaria de ser
        # a que o rebuild produziria.
        if len(new_rows) < len(old_rows) or new_rows[:len(old_rows)] != old_rows:
            return False
        added = {row[0] for row in new_rows[len(old_rows):]}
        # Implicado pelo prefixo acima; custa uma comparação de conjuntos e
        # prende a conclusão em vez de depender do raciocínio.
        if changed != added:
            return False
        if self._row_position_suffix_active():
            return False
        # Lista de fora de passo com o controle: um Append() aqui desalinharia
        # texto e registro para sempre. Mesma guarda de _repaint_message_rows().
        if message_row_count(self) != len(self._sorted_messages):
            logging.info("[_append_new_tail_rows] list out of step with rows — full path")
            return False
        first_row = self._sorted_messages[0]
        if isinstance(first_row, dict) and first_row.get("_type") == "empty_placeholder":
            return False

        records = []
        container = self.conversation.get("messages")
        if isinstance(container, dict):
            inner = container.get("messages")
            if isinstance(inner, dict) and isinstance(inner.get("records"), list):
                records = inner["records"]
        newly = {}
        for m in records:
            if not isinstance(m, dict):
                continue
            mid = (m.get("key") or {}).get("id", "")
            # `mid not in newly` é inalcançável — _signature_changed_ids() já
            # devolveu None para id repetido — e fica pelo mesmo motivo que a
            # comparação `changed != added` acima: o mapa por id só é seguro se
            # o id for único, e isso passa a estar dito aqui também.
            if mid in added and mid not in newly:
                newly[mid] = m
        if len(newly) != len(added):
            return False
        if any(not self._is_displayable_message(m) for m in newly.values()):
            return False

        rendered = {
            (m.get("key") or {}).get("id", "")
            for m in self._sorted_messages if not self._is_separator(m)
        }
        newcomers = [m for mid, m in newly.items() if mid not in rendered]
        newcomers.sort(key=lambda m: self._extract_timestamp(m) or 0)
        tail_ts = None
        for m in reversed(self._sorted_messages):
            if not self._is_separator(m):
                tail_ts = self._extract_timestamp(m) or 0
                break
        if tail_ts is None:
            # Só sentinela na tela e nenhuma mensagem: não há cauda contra a
            # qual comparar, e um piso 0 aqui aprovaria qualquer timestamp.
            return False
        if any((self._extract_timestamp(m) or 0) < tail_ts for m in newcomers):
            return False

        if newcomers:
            # Keep the pagination list in step only while both tails are the
            # same object, as on_incoming_message() does. An unverified offset
            # can fetch already-loaded DB rows and fall through to the server
            # before exhausting local history.
            # O `is not` não é paranoia: _sorted_messages tem de ser um SUFIXO
            # de _all_sorted_messages, nunca o mesmo objeto de lista. Hoje todo
            # produtor fatia ou concatena, então são sempre listas distintas;
            # se alguma passar a aliasar, os dois append() abaixo virariam dois
            # na mesma lista e ela desalinharia do controle.
            in_step = (
                self._all_sorted_messages
                and self._all_sorted_messages is not self._sorted_messages
                and self._all_sorted_messages[-1] is self._sorted_messages[-1]
            )
            self.messages_list.Freeze()
            try:
                for m in newcomers:
                    if in_step:
                        self._all_sorted_messages.append(m)
                    self._sorted_messages.append(m)
                    append_message_row(self, self._render_message_line(m))
                    # Same as the live path: a message that came in through
                    # sync takes its sender off the typing row too (own
                    # messages are ignored inside).
                    dismiss_typing_row_for_message(self, m)
            finally:
                self.messages_list.Thaw()
            self._remember_expanded_window()
        self._messages_signature_cache = new_sig
        logging.info(
            "[_append_new_tail_rows] %d new record(s), %d row(s) appended, %d row(s) total "
            "— no rebuild.",
            len(added), len(newcomers), len(self._sorted_messages),
        )
        return True

    def refresh_messages_if_changed(self):
        """Repopulate the messages list only when its content actually changed.

        Every unattended refresh must come through here rather than calling
        ``populate_messages(preserve_focus=True)`` directly. That rebuild
        re-derives every row and re-writes the native list (per row, through
        _sync_message_rows() — never cleared), and even with preserve_focus it
        can only put focus back on the *message* it saved — the moment that message is no longer in the paginated window (or
        the list was showing the unread separator, or the saved id came back
        empty) focus lands somewhere else entirely. With a 60s poll calling it
        unconditionally, the user was thrown to a random message in the middle
        of the conversation roughly once a minute, mid-read.

        Nothing periodic needs a rebuild when nothing changed, so compare first
        and skip. When the only difference is messages at the END of the
        conversation — a new message, which is the overwhelmingly common case —
        _append_new_tail_rows() covers it by appending those rows (usually
        none: the live path already painted them) and the rebuild is skipped
        too. Anything else still rebuilds in full, preserving focus as best it
        can.
        """
        if self.conversation is None:
            return
        try:
            sig = self._messages_signature()
        except Exception:
            # Never let a fingerprinting hiccup swallow a real refresh.
            logging.exception("[refresh_messages_if_changed] signature failed")
            self.populate_messages(preserve_focus=True)
            return
        if sig == getattr(self, "_messages_signature_cache", None):
            return
        cached = getattr(self, "_messages_signature_cache", None)
        try:
            if self._append_new_tail_rows(cached, sig):
                return
        except Exception:
            # O rebuild abaixo repinta a conversa inteira de qualquer forma, e
            # é ele que estava aqui antes: uma falha no atalho não pode custar
            # a atualização.
            logging.exception("[refresh_messages_if_changed] tail append failed — full path")
        try:
            if self._repaint_changed_rows_in_place(cached, sig):
                return
        except Exception:
            logging.exception("[refresh_messages_if_changed] in-place repaint failed — full path")
        # Neither shortcut covered it, so the list really is being rebuilt and
        # the user really will be re-announced their own row. Say what forced
        # it: without this the only evidence in a log is a populate_messages
        # line every 60s, and working out which record moved took a session of
        # inference. Cheap — it runs only on the path that is about to spend
        # tens of milliseconds rebuilding.
        try:
            changed = self._signature_changed_ids(cached, sig)
            if changed is None:
                logging.info("[refresh_messages_if_changed] rebuild: signatures not "
                             "comparable (conversation, unread separator, or an "
                             "empty/repeated message id changed)")
            else:
                logging.info("[refresh_messages_if_changed] rebuild: %d record(s) "
                             "changed, ids=%s", len(changed), sorted(changed)[:8])
        except Exception:
            pass
        self._messages_signature_cache = sig
        self.populate_messages(preserve_focus=True)

    def populate_messages(self, preserve_focus: bool = False):
        """Rebuild the messages list from self.conversation.

        preserve_focus=True keeps whatever message is currently focused
        instead of resetting to the unread separator / last message — used
        by background refreshes (e.g. the on-demand sync kicked off by
        navigate_to_conversation) so they don't silently yank focus away
        from the user a few seconds after a conversation was opened.
        """
        # Guards the lazy-load-on-focus-0 hook in _on_message_focused: this
        # method's own Focus(0) calls below (a short conversation whose last
        # message or unread separator sits at index 0) fire EVT_LIST_ITEM_FOCUSED
        # synchronously, and re-entering _load_older_messages()/
        # _load_more_messages() — which themselves insert rows into this same
        # list — while this rebuild is still in progress would corrupt the
        # list. Cleared via CallAfter so it stays set for every
        # nested/synchronous focus event this call produces, and only turns
        # off once control actually returns to the event loop.
        self.refresh_poll_votes()  # throttled; repaints its rows when votes change
        self._apply_pinned_message_flags()
        self._populating_messages = True
        wx.CallAfter(self._clear_populating_messages_flag)

        _preserved_msg_id = self._focused_msg_id() if preserve_focus else None
        _had_focus = (wx.Window.FindFocus() is self.messages_list)
        # _focused_msg_id() returns "" both when nothing is focused AND when
        # the focused row is the unread-separator sentinel (it has no
        # message id). Without telling those two apart, a background
        # refresh (preserve_focus=True) that fires while the user happens to
        # be sitting right on the separator row falls through to the same
        # "jump to separator/last message" default used for a freshly opened
        # conversation — see the preserve_focus fallback below.
        _preserved_was_separator = False
        if preserve_focus and not _preserved_msg_id:
            _fi = self.messages_list.GetFocusedItem()
            if 0 <= _fi < len(self._sorted_messages) and self._is_separator(self._sorted_messages[_fi]):
                _preserved_was_separator = True

        top_msg_id = None
        if preserve_focus:
            top_idx = -1
            if hasattr(self.messages_list, "GetTopItem"):
                top_idx = self.messages_list.GetTopItem()
            else:
                try:
                    import ctypes
                    hwnd = self.messages_list.GetHandle()
                    top_idx = ctypes.windll.user32.SendMessageW(hwnd, 0x018E, 0, 0)
                except Exception:
                    pass
            if top_idx != -1 and 0 <= top_idx < len(self._sorted_messages):
                m = self._sorted_messages[top_idx]
                if not self._is_separator(m):
                    top_msg_id = m.get("key", {}).get("id", "")

        # Frozen for the whole rebuild-and-refocus sequence below. Without
        # this, DeleteAllItems() followed by re-Append()ing every row made
        # the native SysListView32 control (still holding keyboard focus the
        # entire time, since this fires from a background wx.CallAfter while
        # the conversation stays open) briefly auto-assign LVIS_FOCUSED to
        # row 0 the moment the first item was appended back into what was,
        # for an instant, an empty focused list — a real Win32 ListView
        # quirk, independent of any Focus()/Select() call this method makes
        # itself. That transient focus (on whatever message pagination
        # happens to put at row 0) fired its own accessibility event, which
        # NVDA could announce — reported live as focus "randomly" jumping to
        # a fixed message (always row 0 of the current pagination window)
        # every time a background refresh (e.g. history backfill delivering
        # an already-seen message for the open conversation) repopulated the
        # list, without ever changing the visible selection. Freeze()
        # suppresses native repaint/accessibility notifications until Thaw()
        # runs in the finally block below, by which point only the final,
        # correct Focus()/Select() call (or lack thereof) is ever observed.
        #
        # Medido, não estimado, pelo mesmo motivo do repaint de nomes em
        # main.py: este rebuild percorre a janela inteira (renderiza cada linha
        # e compara com a tela), roda a cada mensagem nova, e a janela deixou de
        # ser limitada ao messages_page_size. Uma linha por rebuild diz quanto
        # custa a janela no tamanho a que ela chegou. A lista em si não é
        # esvaziada: ver _sync_message_rows().
        _rebuild_started = time.monotonic()
        # Antes de DeleteAllItems(): é _sorted_messages de agora, o que o leitor
        # de tela está lendo, que vira o piso deste rebuild.
        try:
            self._refresh_expanded_window_before_rebuild()
        except Exception:
            logging.exception("[populate_messages] failed to record the window before rebuilding")
        # What the control shows now. The rows are written below by
        # _sync_message_rows(), which only touches the ones that differ — the
        # list is never cleared (see message_rows.py for why).
        _old_rows = list(self._sorted_messages)
        self.messages_list.Freeze()
        try:
            self._unread_sep_idx = -1
            self._reaction_map = {}
            messages_container = (
                self.conversation.get("messages", {}) if self.conversation else {}
            )
            messages: list = []
            if isinstance(messages_container, dict):
                inner = messages_container.get("messages")
                if isinstance(inner, dict) and isinstance(inner.get("records"), list):
                    messages = inner["records"]
            messages_sorted = self._sorted_deduped_records(messages)
            self._reaction_map = self._reaction_map_from_sorted(messages_sorted)

            # Exclude reaction messages — they must not affect index mapping
            displayable = [
                m for m in messages_sorted if self._is_displayable_message(m)
            ]

            # Insert unread separator before the first unread message, either
            # derived from the snapshot taken before mark_conversation_as_read()
            # zeros the dict, or restored from the state the live path left
            # behind (see _place_unread_separator_for_rebuild()).
            displayable = self._place_unread_separator_for_rebuild(displayable)

            # ── Pagination: show only last N messages ────────────────────────────
            self._all_sorted_messages = displayable
            limit = int(
                self.main_window.settings.get("user_interface", {}).get("messages_page_size", 200)
            )
            self._messages_offset, self._unread_sep_idx = (
                self._history_window_for_rebuild(displayable, limit)
            )
            paginated = displayable[self._messages_offset:]

            # A chat with no displayable history (e.g. WhatsApp Web's own store
            # never loaded this conversation's messages, so all WinZapp captured
            # was a non-displayable system record) previously left messages_list
            # with zero rows and nothing was ever focused — for a screen-reader
            # user that reads as total silence, indistinguishable from the app
            # being broken. Show one non-actionable placeholder row instead.
            if not paginated:
                paginated = [{"_type": "empty_placeholder"}]

            self._sorted_messages = paginated

            self._sync_message_rows(_old_rows, paginated)

            # Restore scroll position if preserve_focus is True and we tracked a top visible message
            scrolled = False
            if preserve_focus and top_msg_id:
                for idx, msg in enumerate(self._sorted_messages):
                    if isinstance(msg, dict) and msg.get("key", {}).get("id") == top_msg_id:
                        self.messages_list.EnsureVisible(idx)
                        scrolled = True
                        break

            # A background refresh (preserve_focus=True) should keep the user's
            # current position instead of jumping back to the separator/last
            # message — only fall back to the default placement below if the
            # previously-focused message is no longer present (e.g. it was
            # cleared or paginated out).
            if _preserved_msg_id:
                for idx, msg in enumerate(self._sorted_messages):
                    if isinstance(msg, dict) and msg.get("key", {}).get("id") == _preserved_msg_id:
                        # The row the user is on survived untouched, so the
                        # control still has focus and selection on it. Putting
                        # them back anyway fires a focus event for a row that
                        # never moved, and the screen reader reads it again.
                        _still_there = (
                            self.messages_list.GetFocusedItem() == idx
                            and self.messages_list.GetFirstSelected() == idx
                        )
                        if not _still_there:
                            if _had_focus and wx.Window.FindFocus() is not self.messages_list:
                                self.messages_list.SetFocus()
                            self.messages_list.Focus(idx)
                            self.messages_list.Select(idx)
                            if not scrolled:
                                self.messages_list.EnsureVisible(idx)
                        return

            if preserve_focus:
                if _preserved_was_separator and self._unread_sep_idx >= 0:
                    _sep = self._unread_sep_idx
                    if not (self.messages_list.GetFocusedItem() == _sep
                            and self.messages_list.GetFirstSelected() == _sep):
                        if _had_focus and wx.Window.FindFocus() is not self.messages_list:
                            self.messages_list.SetFocus()
                        self.messages_list.Focus(_sep)
                        self.messages_list.Select(_sep)
                        if not scrolled:
                            self.messages_list.EnsureVisible(_sep)
                return

            # Make the unread separator visible, or select and focus the last (newest) message by default
            if not scrolled:
                if self._unread_sep_idx >= 0:
                    last = message_row_count(self) - 1
                    target_visible = min(self._unread_sep_idx + 3, last)
                    if target_visible >= 0:
                        self.messages_list.EnsureVisible(target_visible)
                    self.messages_list.EnsureVisible(self._unread_sep_idx)
                    self.messages_list.Focus(self._unread_sep_idx)
                    self.messages_list.Select(self._unread_sep_idx)
                else:
                    # The last MESSAGE: a typing row below it is not where a
                    # freshly opened conversation lands.
                    last = message_row_count(self) - 1
                    if last >= 0:
                        self.messages_list.EnsureVisible(last)
                        self.messages_list.Focus(last)
                        self.messages_list.Select(last)
                        logging.info(
                            "[populate_messages] default-select tail: last=%d "
                            "GetFocusedItem()=%d GetFirstSelected()=%d ItemCount=%d",
                            last, self.messages_list.GetFocusedItem(),
                            self.messages_list.GetFirstSelected(),
                            self.messages_list.GetItemCount(),
                        )
                    else:
                        logging.info("[populate_messages] default-select tail: list is empty (last=-1)")
        finally:
            # The typing row is never written by the rebuild above (it is not
            # a record); re-decided here, still frozen, because the
            # conversation may have changed under it.
            try:
                sync_typing_row(self)
            except Exception:
                logging.exception("[populate_messages] failed to update the typing row")
            self.messages_list.Thaw()
            # A janela que acabou de ser pintada é o piso da próxima. Aqui, no
            # finally, pelo mesmo motivo da assinatura abaixo: o corpo retorna
            # de vários pontos, e um rebuild que não registrasse a janela
            # deixaria o seguinte livre para cortá-la. Ver
            # _remember_expanded_window().
            try:
                self._remember_expanded_window()
            except Exception:
                logging.exception("[populate_messages] failed to record the rendered window")
            # Snapshot what is now on screen so the next background refresh can
            # tell "nothing changed" apart from "needs a rebuild" — see
            # refresh_messages_if_changed(). Taken here, in the finally, because
            # the body above returns from several places.
            try:
                self._messages_signature_cache = self._messages_signature()
            except Exception:
                self._messages_signature_cache = None
            # The focused row may now be another message (another conversation
            # opened, or a call record whose outcome just settled) without a
            # focus event of its own, so "Retornar ligação" is re-decided here.
            try:
                self._update_return_call_button(self.messages_list.GetFocusedItem())
            except Exception:
                logging.exception("[populate_messages] failed to update the return-call button")
            _rebuild_ms = (time.monotonic() - _rebuild_started) * 1000.0
            # Acima do limiar sobe para WARNING. Em INFO o número só aparece
            # para quem já foi procurar por ele, e este é o laço que a janela
            # sem teto alarga: DeleteAllItems() + um Append() por linha, a cada
            # mensagem nova. 250 ms é uma ordem de grandeza abaixo dos stalls
            # que o watchdog mediu no laço vizinho (9,4 s / 19,8 s / 40,1 s —
            # ver _schedule_refresh_active_messages() em main.py), então o
            # aviso chega no log antes de o usuário sentir travamento.
            if _rebuild_ms > 250.0:
                logging.warning(
                    "[populate_messages] rebuilt %d row(s) in %.0f ms (offset=%d).",
                    len(self._sorted_messages), _rebuild_ms, self._messages_offset,
                )
            elif logging.getLogger().isEnabledFor(logging.INFO):
                logging.info(
                    "[populate_messages] rebuilt %d row(s) in %.0f ms (offset=%d).",
                    len(self._sorted_messages), _rebuild_ms, self._messages_offset,
                )
