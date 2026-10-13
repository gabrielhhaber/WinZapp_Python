"""ReactionsMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import logging
import threading
import time
import wx
from ui.dialogs.emoji_picker import choose_reaction_emoji, skin_tone_step
from core.reaction_shortcuts import (
    quick_reactions,
    reaction_to_send,
    remember_reaction,
)


class ReactionsMixin:
    """Sending, applying, persisting and backfilling reactions.
    """

    def _on_menu_react(self, msg: dict):
        """Open the emoji picker dialog to react to a message."""
        if self._reject_system_event_action(msg):
            return
        # Keep the target before the modal picker or worker can outlive this
        # conversation. Message keys may also be updated by a history sync.
        jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        msg_key = dict(msg.get("key") or {})
        if not jid or not msg_key.get("id"):
            return
        i18n = self.main_window.i18n
        msg_id = msg_key["id"]
        # issue #67: show which reaction (if any) I already sent to this
        # message, and let activating it again remove it — there was
        # previously no way to remove a reaction from the UI at all.
        current_emoji = (self._reaction_map.get(msg_id) or {}).get(self._SELF_REACTOR_KEY, "")
        settings = self.main_window.settings
        reactions = settings.get("reactions", {})
        # A list of its own: Left/Right swap a row for another skin tone.
        emojis = list(quick_reactions(
            settings.get("reaction_recent_emojis"), current_emoji,
            fixed_slots=(reactions.get("quick_reaction_slots", [])
                         if reactions.get("fixed_quick_reactions", False) else None),
        ))

        dlg = wx.Dialog(
            self.main_window,
            title=i18n.t("react_dialog_title"),
            style=wx.DEFAULT_DIALOG_STYLE,
            size=(300, 420),
        )
        panel = wx.Panel(dlg)
        sizer = wx.BoxSizer(wx.VERTICAL)

        hint_label = wx.StaticText(
            panel,
            label=(i18n.t("react_dialog_hint_remove") if current_emoji else i18n.t("react_dialog_hint"))
            + " " + i18n.t("react_dialog_hint_tones"),
        )
        hint_label.Wrap(270)
        sizer.Add(hint_label, 0, wx.ALL, 8)

        emoji_list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        emoji_list.InsertColumn(0, i18n.t("react_dialog_title"), width=240)
        emoji_list.EnableCheckBoxes(True)
        current_idx = -1
        emoji_list.Freeze()
        try:
            for idx, emoji in enumerate(emojis):
                emoji_list.Append((emoji,))
                if emoji == current_emoji:
                    emoji_list.CheckItem(idx, True)
                    current_idx = idx
            emoji_list.Append((i18n.t("react_dialog_more"),))
        finally:
            emoji_list.Thaw()
        sizer.Add(emoji_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        cancel_btn = wx.Button(panel, wx.ID_CANCEL, label=i18n.t("cancel"))
        sizer.Add(cancel_btn, 0, wx.ALIGN_RIGHT | wx.ALL, 8)

        panel.SetSizer(sizer)
        dlg_sizer = wx.BoxSizer(wx.VERTICAL)
        dlg_sizer.Add(panel, 1, wx.EXPAND)
        dlg.SetSizer(dlg_sizer)

        selected_emoji = [None]

        def _on_emoji_activated(event):
            idx = event.GetIndex()
            if idx == len(emojis):
                dlg.EndModal(wx.ID_MORE)
            elif 0 <= idx < len(emojis):
                # Activating the reaction already checked (i.e. the one I
                # already sent) removes it instead of resending the same
                # emoji — the only way to clear a reaction previously.
                selected_emoji[0] = reaction_to_send(emojis[idx], current_emoji)
                dlg.EndModal(wx.ID_OK)

        def _on_list_key(event):
            # As in the emoji picker: the horizontal arrows walk the skin
            # tones of the highlighted emoji, when it has them.
            if event.GetKeyCode() in (wx.WXK_LEFT, wx.WXK_RIGHT):
                idx = emoji_list.GetFirstSelected()
                if 0 <= idx < len(emojis):
                    toned = skin_tone_step(emojis[idx], 1 if event.GetKeyCode() == wx.WXK_RIGHT else -1)
                    if toned is not None:
                        emojis[idx] = toned
                        emoji_list.SetItem(idx, 0, toned)
                        # The screen reader names the emoji, tone included.
                        self.main_window.output(toned, interrupt=True)
                        return
            event.Skip()

        def _on_emoji_selected(event):
            # Single click: just move selection, don't send yet
            pass

        def _on_emoji_checked(event):
            # "Add more reactions" is an action, not a choice: Space would
            # otherwise leave it announced as "checked" with nothing chosen.
            if event.GetIndex() == len(emojis):
                emoji_list.CheckItem(len(emojis), False)

        emoji_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, _on_emoji_activated)
        emoji_list.Bind(wx.EVT_KEY_DOWN, _on_list_key)
        emoji_list.Bind(wx.EVT_LIST_ITEM_CHECKED, _on_emoji_checked)
        cancel_btn.Bind(wx.EVT_BUTTON, lambda e: dlg.EndModal(wx.ID_CANCEL))
        dlg.Bind(wx.EVT_CHAR_HOOK, lambda e: dlg.EndModal(wx.ID_CANCEL) if e.GetKeyCode() == wx.WXK_ESCAPE else e.Skip())

        # A pre-populated list must never leave focus/selection pointing at
        # nothing — mirrors the conversation list's own convention. Land on
        # the currently-sent reaction when there is one, same reasoning as
        # every other "open on the relevant row" dialog in this app.
        if emoji_list.GetItemCount() > 0:
            start = current_idx if current_idx >= 0 else 0
            emoji_list.Focus(start)
            emoji_list.Select(start)
        emoji_list.SetFocus()
        dlg.CentreOnParent()
        result = dlg.ShowModal()
        dlg.Destroy()

        if result == wx.ID_MORE:
            choice = choose_reaction_emoji(self, i18n)
            if choice is not None:
                selected_emoji[0] = "" if choice == current_emoji else choice
                result = wx.ID_OK

        if result == wx.ID_OK and selected_emoji[0] is not None:
            emoji = selected_emoji[0]
            threading.Thread(
                target=self._do_send_reaction,
                args=(jid, msg_key, emoji),
                daemon=True,
            ).start()

    _SELF_REACTOR_KEY = "_me_"

    def _reactor_key_from_msg(self, msg: dict) -> str:
        """Identity of whoever sent this reactionMessage — used so each sender
        only ever holds one active reaction per message in _reaction_map."""
        key = msg.get("key", {}) or {}
        if key.get("fromMe"):
            return self._SELF_REACTOR_KEY
        return key.get("participant") or key.get("remoteJid") or ""

    def _reaction_counts(self, msg_id: str) -> dict:
        """Aggregate {sender: emoji} into {emoji: count} for display."""
        per_msg = self._reaction_map.get(msg_id) or {}
        counts: dict = {}
        for emoji in per_msg.values():
            counts[emoji] = counts.get(emoji, 0) + 1
        return counts

    def _send_reaction(self, msg: dict, emoji: str):
        """Send reaction directly (called from most-used submenu)."""
        jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        msg_key = dict(msg.get("key") or {})
        if not jid or not msg_key.get("id"):
            return
        threading.Thread(
            target=self._do_send_reaction,
            args=(jid, msg_key, emoji),
            daemon=True,
        ).start()

    def _do_send_reaction(self, jid: str, msg_key: dict, emoji: str):
        """Background: send to the target captured by the UI, never the live chat."""
        ok = self.main_window.send_reaction(jid, msg_key, emoji)
        if ok:
            # Apply optimistically — the WebSocket echo for own reactions is
            # suppressed in on_messages_upsert to avoid double-counting.
            wx.CallAfter(self._on_own_reaction_sent, jid, msg_key, emoji)

    def apply_incoming_reaction(self, remote_jid: str, msg: dict):
        """Apply a reactionMessage that just arrived over the WebSocket.

        Persisting is unconditional; only the live redraw is conditional on
        the reacted-to chat being the one currently open. main.py's
        on_new_message() deliberately never appends a reactionMessage to a
        chat's `records` itself, so this is the ONLY thing that files a live
        reaction anywhere — and it used to run behind
        on_incoming_message()'s "is this conversation open?" guard, which
        meant a reaction to a chat the user was not looking at (the normal
        case: the toast for it only ever fires while the window is in the
        background) was applied nowhere at all. It showed up as a
        notification and then simply did not exist: opening the conversation
        afterwards rebuilds _reaction_map by scanning `records`, which never
        received it.

        _reaction_map, by contrast, only ever describes the conversation
        currently rendered in messages_list — populate_messages() rebuilds it
        from scratch per conversation — so it is only touched when this
        reaction really belongs to that conversation.
        """
        reaction   = (msg.get("message") or {}).get("reactionMessage") or {}
        emoji      = reaction.get("text", "")
        orig_id    = (reaction.get("key") or {}).get("id", "")
        sender_key = self._reactor_key_from_msg(msg)
        if not orig_id or not sender_key:
            return

        if self._matches_open_conversation(remote_jid):
            per_msg = self._reaction_map.setdefault(orig_id, {})
            if emoji:
                # A new/changed reaction from this sender replaces theirs —
                # it never accumulates into a bogus higher count.
                per_msg[sender_key] = emoji
            else:
                # Empty emoji = this sender removed their reaction.
                per_msg.pop(sender_key, None)
            # Re-render the original message in the list
            for i, m in enumerate(self._sorted_messages):
                if not self._is_separator(m) and m.get("key", {}).get("id") == orig_id:
                    self.messages_list.SetItemText(i, self._render_message_line(m))
                    # The Reactions button only ever refreshes on focus
                    # change (_update_reactions_button() is called from the
                    # list's EVT_LIST_ITEM_FOCUSED handler) — a reaction
                    # landing on the message the user already has focused
                    # left the button in whatever state it was in before,
                    # requiring the user to move focus away and back just to
                    # make it appear. Refresh it here too when this is the
                    # row currently focused.
                    if i == self.messages_list.GetFocusedItem():
                        self._update_reactions_button(i)
                    break

        # Persist so populate_messages()/refresh_active_conversation_messages()
        # (which rebuild _reaction_map purely from `records`) can recover this
        # reaction whenever the message list is (re)built — see
        # _persist_reaction_record()'s own docstring. Not _track_last_reaction()
        # or _schedule_set_chats() here — main.py's on_new_message() already
        # calls both for every reaction from someone else.
        own_key = msg.get("key", {}) or {}
        self._persist_reaction_record(
            remote_jid, orig_id, reaction.get("key") or {}, sender_key,
            bool(own_key.get("fromMe")), emoji,
            participant=own_key.get("participant", ""),
        )

    def _persist_reaction_record(self, jid: str, orig_id: str, msg_key: dict,
                                  sender_key: str, from_me: bool, emoji: str,
                                  participant: str = "") -> "dict | None":
        """Persist a reaction (ours or someone else's) into the chat's own
        records, so populate_messages()/refresh_active_conversation_messages()
        — which rebuild _reaction_map purely by scanning `records` — can
        recover it after anything repopulates the message list: a
        conversation close/reopen, an app restart, or any background
        full-list refresh in between (e.g. a history backfill delivering an
        already-seen message). A reaction from someone else used to update
        only the in-memory _reaction_map and nothing else, so it silently
        vanished — both the inline marker on the message row and the
        Reactions button — the next time anything rebuilt the list, with no
        real relation to focus movement despite how it was reported
        ("reação some ao voltar o foco pra mensagem"). Own reactions already
        persisted this way; this is the same thing for the received case.

        The synthetic record id is namespaced per (message, sender) — not
        just per message — since more than one person can react to the same
        message with different emojis at once; only the mover's own key
        keeps the original bare `_rxn_{orig_id}` form used before per-sender
        namespacing existed, so an already-persisted self-reaction record on
        an existing install is found and updated in place rather than
        duplicated under a new id.

        Returns the record dict (the caller may still need it, e.g. for
        _track_last_reaction()), or None if there was nothing to persist.
        """
        chat = self.main_window.get_chat(jid)
        if not chat or not orig_id:
            return None
        # get_chat() already resolves the @lid/phone duality when looking the
        # chat up, but the record itself still has to be filed under the
        # chat's own canonical JID: a live reaction arrives under whichever
        # form the event happened to use, and persisting it under the other
        # one wrote it into a DB bucket the conversation never reads back.
        jid = chat.get("remoteJid") or jid
        rxn_id = (
            f"_rxn_{orig_id}" if sender_key == self._SELF_REACTOR_KEY
            else f"_rxn_{orig_id}_{sender_key}"
        )
        key = {"remoteJid": jid, "fromMe": from_me, "id": rxn_id}
        if participant:
            key["participant"] = participant
        reaction_record = {
            "messageType": "reactionMessage",
            "message": {
                "reactionMessage": {
                    "key":  msg_key,
                    "text": emoji,
                }
            },
            "key": key,
            "messageTimestamp": int(time.time()),
        }
        records = (
            chat.setdefault("messages", {})
                .setdefault("messages", {})
                .setdefault("records", [])
        )
        # Update the existing reaction record for this (message, sender)
        # pair in place (changing the emoji) instead of silently no-op'ing —
        # previously a changed reaction only updated the in-memory map, so
        # the old emoji came back after reopening the conversation.
        existing = next((r for r in records if r.get("key", {}).get("id") == rxn_id), None)
        if existing:
            existing["message"] = reaction_record["message"]
            existing["messageTimestamp"] = reaction_record["messageTimestamp"]
        else:
            records.append(reaction_record)
        try:
            self.main_window.db.insert_message(jid, reaction_record)
        except Exception:
            logging.exception("[conversations] insert reaction failed")
        return reaction_record

    # Bounded, not exhaustive: there is no cheap way to know in advance which
    # of a chat's messages actually have a reaction to find, so this is one
    # request per message checked. Scoped to what a user opening a chat can
    # actually see without scrolling — asking for an entire multi-year
    # history would be thousands of requests for a handful of hits.
    _REACTION_BACKFILL_LIMIT = 40

    # How long a chat that was just backfilled is left alone before it is
    # walked again. One open costs up to _REACTION_BACKFILL_LIMIT sequential
    # requests, and alternating between two conversations — Alt+Tab-grade
    # ordinary usage — otherwise pays that in full every single time, for a
    # window in which essentially nothing can have changed that the live
    # WebSocket would not have delivered anyway. The gap this closes is a
    # disconnection, measured in minutes at least, so a few minutes of
    # staleness costs nothing.
    _REACTION_BACKFILL_COOLDOWN_SECONDS = 300

    def _reaction_backfill_is_due(self, jid: str) -> bool:
        """Whether `jid` is outside its backfill cooldown (and mark it done).

        Not a cache of the reactions themselves — it only decides whether to
        ask again. Keyed by the same canonical JID the rest of the reaction
        path uses, so the @lid and phone forms of one chat share a cooldown
        instead of each getting their own.
        """
        seen = getattr(self, "_reaction_backfill_last", None)
        if seen is None:
            # getattr-guarded like the other lazily-present attributes on
            # this path: the test stubs carry only what the method touches.
            seen = self._reaction_backfill_last = {}
        key = self._canonical_reactor_key(jid)
        now = time.time()
        if now - seen.get(key, 0) < self._REACTION_BACKFILL_COOLDOWN_SECONDS:
            return False
        seen[key] = now
        return True

    def _backfill_reactions_for_open_conversation(self):
        """Fetch reactions for the most recent messages of the conversation
        just opened, so one that happened while WinZapp was disconnected —
        never delivered live, and not something a normal resync re-fetches
        either, see MainWindow.fetch_message_reactions()'s docstring — is
        picked up the moment the chat is opened, instead of never at all.

        Runs in the background; _apply_backfilled_reactions() (back on the
        UI thread) is what actually touches _reaction_map/records, guarded
        by the generation counter so a fetch for a conversation the user has
        since navigated away from cannot land on whatever is open by then.
        """
        if self.conversation is None:
            return
        jid = self.conversation.get("remoteJid", "")
        if not jid:
            return
        # Before the cooldown is stamped, not after. Every request this pass
        # would make bails on the same flag inside
        # MainWindow.fetch_message_reactions(), so arming it while offline
        # spends the whole 5 minutes on a pass that cannot fetch anything —
        # and the pass that most often runs disconnected is the one right
        # after a reconnection, which is the case this backfill exists for:
        # the health poll can take ~30 s to confirm the connection, and a
        # chat opened inside that window would then stay stale until the user
        # left it and came back more than five minutes later.
        if not getattr(self.main_window, "_wa_connected", False):
            return
        if not self._reaction_backfill_is_due(jid):
            return
        records = (
            self.conversation.get("messages", {})
                .get("messages", {})
                .get("records", [])
        )
        candidates = [
            r for r in records
            if isinstance(r, dict)
            and r.get("messageType") != "reactionMessage"
            and r.get("key", {}).get("id")
        ]
        try:
            candidates.sort(key=lambda m: self._extract_timestamp(m) or 0, reverse=True)
        except Exception:
            pass
        msg_ids = [
            m.get("key", {}).get("id") for m in candidates[: self._REACTION_BACKFILL_LIMIT]
        ]
        if not msg_ids:
            return
        self._reaction_backfill_generation += 1
        generation = self._reaction_backfill_generation
        threading.Thread(
            target=self._do_backfill_reactions,
            args=(jid, msg_ids, generation),
            daemon=True,
        ).start()

    def _do_backfill_reactions(self, jid: str, msg_ids: list, generation: int):
        """Background: one GET per candidate message. A single message's
        request failing (offline blip, timeout) must not lose the ones
        already fetched, so each is caught and skipped individually rather
        than aborting the whole batch."""
        results = []
        for msg_id in msg_ids:
            if generation != self._reaction_backfill_generation:
                return  # superseded — the user already opened something else
            try:
                payload = self.main_window.fetch_message_reactions(msg_id)
            except Exception:
                logging.exception(
                    "[conversations] reaction backfill failed for %s", msg_id)
                continue
            if payload:
                results.append((msg_id, payload))
        if results:
            wx.CallAfter(self._apply_backfilled_reactions, jid, results, generation)

    def _apply_backfilled_reactions(self, jid: str, results: list, generation: int):
        if generation != self._reaction_backfill_generation:
            return
        if self.conversation is None or self.conversation.get("remoteJid") != jid:
            return
        changed = False
        for orig_id, payload in results:
            if self._merge_fetched_reactions(jid, orig_id, payload):
                changed = True
        if changed:
            self.populate_messages(preserve_focus=True)

    def _canonical_reactor_key(self, jid: str) -> str:
        """One JID reduced to the single form both sides of the reaction merge
        can be compared in: device suffix stripped and @c.us folded to
        @s.whatsapp.net (main.py's _normalize_jid), then @lid bridged to the
        phone JID it maps to whenever the cache knows it (_lid_to_phone).

        Both sources feed reactions in under whichever form they happened to
        use — a live event through the WebSocketClient, a backfill straight
        out of WhatsApp Web's own Store — and the same person under two
        forms is two people as far as the one-reaction-per-person rule is
        concerned. Unresolvable input is returned unchanged rather than
        emptied: an @lid nobody has mapped yet is still a stable identity,
        just not the canonical one.
        """
        mw = self.main_window
        try:
            jid = mw._normalize_jid(jid)
            return getattr(mw, "_lid_to_phone", {}).get(jid, jid)
        except Exception:
            return jid

    def _reactor_key_from_api(self, sender_user_jid) -> str:
        """The identity _reactor_key_from_msg() would give the same person,
        built from a /reactions/{id} sender instead of a stored record.

        The two were compared raw, and they are not the same thing.
        `senderUserJid` is built by wa-js as createWid(...) — a Wid, which
        reaches Python either as its serialized string or as the object it
        serializes to, and always in WhatsApp Web's own JID forms (@c.us,
        @lid), never the @s.whatsapp.net the WebSocketClient normalizes a
        live reaction's participant to. So every reaction already on file
        looked like a *new* one under a key of its own (persisted a second
        time — the visible symptom being an inflated count) and, in the same
        pass, like a *removed* one, because its stored key was absent from
        the fetched set. On every conversation open.

        Returns "" for anything unusable; the caller skips those.
        """
        if isinstance(sender_user_jid, dict):
            # A Wid that survived serialization as an object rather than as
            # its string. Only _serialized is the whole JID — server/user
            # are its halves.
            sender_user_jid = sender_user_jid.get("_serialized") or ""
        jid = str(sender_user_jid or "").strip()
        if not jid or "@" not in jid:
            return ""
        return self._canonical_reactor_key(jid)

    def _is_self_reactor(self, canonical_jid: str) -> bool:
        """Whether a canonical reactor key is this account.

        Deliberately not read off the response's `reactionByMe`, which is
        absent whenever we hold no reaction on the message — and "no
        reaction known here yet" is exactly the state a reaction made from
        the phone while WinZapp was offline starts from, i.e. the one case
        this whole backfill exists to find. Comparing against our own JID
        answers it without depending on already knowing the answer.
        """
        if not canonical_jid:
            return False
        mw = self.main_window
        for own in (getattr(mw, "my_jid", ""), getattr(mw, "my_lid", "")):
            if own and self._canonical_reactor_key(own) == canonical_jid:
                return True
        return False

    def _merge_fetched_reactions(self, jid: str, orig_id: str, payload: dict) -> bool:
        """Reconcile one message's /reactions/{id} response against whatever
        is already persisted for it, via the same _persist_reaction_record()
        every other reaction source uses — so a reaction already known from
        a live event, or from a previous backfill, can never be duplicated
        under a different id, only updated in place.

        Handles removal too, but asymmetrically, and that asymmetry is the
        point: see below.
        """
        reactions = payload.get("reactions")
        if not isinstance(reactions, list):
            return False

        fetched: dict = {}  # canonical key -> (emoji, from_me, participant)
        for group in reactions:
            if not isinstance(group, dict):
                continue
            for sender in (group.get("senders") or []):
                if not isinstance(sender, dict):
                    continue
                emoji = (sender.get("reactionText") or "").strip()
                sender_jid = self._reactor_key_from_api(sender.get("senderUserJid"))
                if not sender_jid or not emoji:
                    continue
                from_me = self._is_self_reactor(sender_jid)
                sender_key = self._SELF_REACTOR_KEY if from_me else sender_jid
                fetched[sender_key] = (emoji, from_me, sender_jid)

        # Two maps, not one: `existing` answers "has this person's reaction
        # changed?" in the canonical key space, while `filed_under` remembers
        # the key their record is actually stored under. _persist_reaction_record()
        # dedups by "_rxn_{orig_id}_{sender_key}", so writing an update under
        # a newly-canonicalized key would append a SECOND record for someone
        # already on file — the very duplication this pass exists to avoid.
        existing: dict = {}      # canonical key -> emoji currently stored
        filed_under: dict = {}   # canonical key -> key that record uses
        for r in self._chat_records_for(jid):
            if not (isinstance(r, dict) and r.get("messageType") == "reactionMessage"):
                continue
            reaction = (r.get("message") or {}).get("reactionMessage") or {}
            if (reaction.get("key") or {}).get("id") != orig_id:
                continue
            stored_key = self._reactor_key_from_msg(r)
            if not stored_key:
                continue
            key = (stored_key if stored_key == self._SELF_REACTOR_KEY
                   else self._canonical_reactor_key(stored_key))
            existing[key] = (reaction.get("text") or "").strip()
            filed_under[key] = stored_key

        changed = False
        for sender_key, (emoji, from_me, sender_jid) in fetched.items():
            if existing.get(sender_key) == emoji:
                continue  # already known — nothing to persist or redraw for
            if self._persist_reaction_record(
                jid, orig_id, {"id": orig_id},
                filed_under.get(sender_key, sender_key), from_me, emoji,
                participant="" if from_me else sender_jid,
            ) is not None:
                changed = True

        if not fetched:
            # An empty-but-successful response is NOT "nobody reacted".
            # /reactions/{id} reads WhatsApp Web's live Store, not a history:
            # a message the Store does not currently hold — routine for the
            # older end of the 40 this backfill walks — answers 200 with
            # nothing at all. Treating that as confirmed removal wiped every
            # reaction the app already knew about, which is worse than the
            # gap this whole method exists to close. Removal therefore needs
            # positive evidence: somebody else's reaction present in the same
            # response, proving it was actually read.
            return changed
        # Whoever was known locally but is absent from a response that did
        # carry reactions removed theirs while WinZapp could not see it.
        for sender_key in set(existing) - set(fetched):
            if not existing.get(sender_key):
                continue  # already empty/removed locally
            from_me = sender_key == self._SELF_REACTOR_KEY
            if self._persist_reaction_record(
                jid, orig_id, {"id": orig_id},
                filed_under.get(sender_key, sender_key), from_me, "",
                participant="" if from_me else sender_key,
            ) is not None:
                changed = True
        return changed

    def _chat_records_for(self, jid: str) -> list:
        """The stored records list for `jid`, or [] if there is none —
        shared by _merge_fetched_reactions() so it does not have to
        duplicate get_chat()'s @lid/phone resolution."""
        chat = self.main_window.get_chat(jid)
        if not chat:
            return []
        records = chat.get("messages", {}).get("messages", {}).get("records", [])
        return records if isinstance(records, list) else []

    def _on_own_reaction_sent(self, jid: str, msg_key: dict, emoji: str):
        """Update reaction_map, re-render the original message, and refresh the list."""
        orig_id = msg_key.get("id", "")
        if not orig_id:
            return

        # The HTTP result can arrive after navigation or closing the chat.
        # Persist it below regardless, but this map belongs only to the open
        # conversation. A bare message id alone cannot identify its chat.
        if self._matches_open_conversation(jid):
            if emoji:
                self._reaction_map.setdefault(orig_id, {})[self._SELF_REACTOR_KEY] = emoji
            else:
                self._reaction_map.get(orig_id, {}).pop(self._SELF_REACTOR_KEY, None)

            for i, m in enumerate(self._sorted_messages):
                if not self._is_separator(m) and m.get("key", {}).get("id") == orig_id:
                    self.messages_list.Freeze()
                    try:
                        self.messages_list.SetItemText(i, self._render_message_line(m))
                    finally:
                        self.messages_list.Thaw()
                    if i == self.messages_list.GetFocusedItem():
                        self._update_reactions_button(i)
                    break

        # Persist reaction in chat records so _last_msg_preview and populate_messages
        # can reflect it after a conversation close/reopen.
        reaction_record = self._persist_reaction_record(
            jid, orig_id, msg_key, self._SELF_REACTOR_KEY, True, emoji,
        )
        if reaction_record is not None:
            # reaction_record stays in `records` only so populate_messages()
            # can rebuild the reaction map (and thus redraw the reacted-to
            # message's inline reaction marker) after a conversation
            # close/reopen or app restart — it must NOT also become
            # eligible as the chat-list preview's "last message" the way a
            # real message would. Received reactions already go through
            # _track_last_reaction() (see on_new_message), which keeps the
            # "você reagiu com X" / "Fulano reagiu com X" preview in a
            # separate chat["_last_reaction"] field instead of `records` —
            # own reactions previously skipped that call entirely (the
            # WebSocket echo for them is suppressed), so the chat list fell
            # back to formatting this raw reactionMessage record as if it
            # were a normal message, which _last_msg_preview() has no case
            # for and rendered as "mensagem incompatível" for the
            # conversation the user had just reacted in.
            self.main_window._track_last_reaction(jid, reaction_record)

        self.main_window._schedule_set_chats()
        if emoji and isinstance(getattr(self.main_window, "settings", None), dict):
            settings = self.main_window.settings
            settings["reaction_recent_emojis"] = remember_reaction(
                settings.get("reaction_recent_emojis"), emoji,
            )
            self.main_window._schedule_save_settings()
