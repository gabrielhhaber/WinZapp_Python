"""MentionsMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import logging
import re
import threading
import time
import wx
import wx.adv


class MentionsMixin:
    """@mentions: extracting them, the mentions panel and the composer's mention
    suggestions.
    """

    # ── @mention helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _raw_mentioned_jids(msg: dict) -> list:
        """The message's mentionedJid list, from wherever contextInfo landed."""
        msg_obj = msg.get("message") or {}
        ext = (msg_obj.get("extendedTextMessage") or {}) if isinstance(msg_obj, dict) else {}
        return (
            (msg.get("contextInfo") or {}).get("mentionedJid")
            or (msg_obj.get("contextInfo") or {}).get("mentionedJid")
            or (ext.get("contextInfo") or {}).get("mentionedJid")
            or []
        )

    def _mention_identity(self, jid: str) -> str:
        """One canonical string per person, whichever JID form they arrive as.

        @lid and @s.whatsapp.net are two addresses for the same participant, and
        which one a mention list carries depends entirely on who sent the
        message (see _extract_mentions). Resolve @lid through the phone cache —
        and fall back to the bare digits when the cache doesn't know it yet, so
        two forms of an unmapped participant at least still collide rather than
        counting as two different people.
        """
        if not jid:
            return ""
        mw = self.main_window
        jid = mw._normalize_jid(jid)
        if jid.endswith("@lid"):
            jid = getattr(mw, "_lid_to_phone", {}).get(jid, jid)
        return jid.rsplit("@", 1)[0].split(":")[0]

    @staticmethod
    def _mention_text(msg: dict) -> str:
        """The text a mention is written in: the body, or a media caption."""
        msg_obj = msg.get("message") or {}
        if not isinstance(msg_obj, dict):
            return ""
        ext = msg_obj.get("extendedTextMessage") or {}
        candidates = [msg_obj.get("conversation"), ext.get("text") if isinstance(ext, dict) else None]
        for value in msg_obj.values():
            if isinstance(value, dict):
                candidates.append(value.get("caption"))
        return next((c for c in candidates if isinstance(c, str) and c), "")

    def _individually_named_mentions(self, msg: dict, mentioned: list) -> list:
        """(display_name, jid) of the people the text names with their own
        ``@<number>``, out of a mention list that covers the whole group.

        An @todos expands to every participant in ``mentionedJid`` and leaves
        no marker, so a person also mentioned by name is in that list either
        way. What tells them apart is the text: a send swaps ``@Name`` for
        ``@<phone>`` (_build_mention_payload), while @todos stays a word.
        """
        tokens = set(re.findall(r"@(\d+)", self._mention_text(msg)))
        if not tokens:
            return []
        mw = self.main_window
        lid_to_phone = getattr(mw, "_lid_to_phone", {})
        phone_to_lid = getattr(mw, "_phone_to_lid", {})
        out, seen = [], set()
        for jid in mentioned:
            if not jid or jid in seen:
                continue
            norm = mw._normalize_jid(jid)
            forms = {jid, norm, lid_to_phone.get(norm, ""), phone_to_lid.get(norm, "")}
            digits = {self._mention_identity(form) for form in forms if form}
            digits |= {form.rsplit("@", 1)[0].split(":")[0] for form in forms if form}
            if digits & tokens:
                seen.add(jid)
                out.append((self._get_participant_name(jid), jid))
        return out

    def _extract_mentions(self, msg: dict) -> list:
        """Return list of (display_name, jid) for @mentioned JIDs in msg.

        Returns [] when the mentioned set covers essentially every
        participant in the group — WhatsApp expands an @Todos/@everyone
        mention into the full participant JID list with no marker
        distinguishing it from mentioning each person individually, and a
        hyperlink per participant (routinely dozens in a large group) is
        just UI noise for something that always means "everyone", not
        something a per-person link helps with. Individual mentions of
        specific people still show their links as before, and so do people
        named on their own next to an @todos.
        """
        mentioned = self._raw_mentioned_jids(msg)
        if not mentioned:
            return []

        participants = getattr(self, "_group_participants_cache", None)
        if participants:
            # _normalize_jid() alone is NOT enough to compare these two sets:
            # it only rewrites @c.us → @s.whatsapp.net and leaves @lid untouched.
            # The participants cache is populated with whatever form the group
            # metadata uses (@lid on LID-addressed accounts), while a message
            # WinZapp itself sent carries the phone form — _canonical_mention_jids()
            # converts every mention through _lid_to_phone before handing it to
            # the send endpoint. So for our OWN @todos messages the two sets
            # never intersected at all, the "this is really @everyone" test
            # always failed, and every participant got their own hyperlink
            # (dozens of them) — while the identical message received from
            # someone else, whose mentions arrive still keyed by @lid, was
            # correctly collapsed. Bridge both sides to one identity first.
            _ident = self._mention_identity
            participant_jids = {_ident(jid) for _, jid in participants}
            participant_jids.discard("")
            # Strictly more than 2: with exactly 2 participants the threshold
            # below is len - 1 == 1, so mentioning one person is arithmetically
            # indistinguishable from mentioning everyone, and every individual
            # mention in a 2-person group gets silently swallowed. Relaxing
            # this to >= 2 is what broke
            # test_a_tiny_group_is_not_treated_as_mention_all.
            if len(participant_jids) > 2:
                mentioned_norm = {_ident(jid) for jid in mentioned if jid}
                mentioned_norm.discard("")
                # Primary check: JID intersection (works for received messages).
                # Fallback: count-based check — if mentioned count covers almost
                # all participants, it's @todos regardless of JID format mismatch
                # (happens for messages WinZapp itself sent, where phone-form JIDs
                # don't intersect the @lid-keyed participant cache).
                intersect_size = len(mentioned_norm & participant_jids)
                threshold = len(participant_jids) - 1
                # count_match requires >= 2 mentions AND near-full coverage to
                # avoid suppressing individual mentions in small groups.
                count_match = (
                    len(mentioned_norm) >= 2
                    and len(mentioned_norm) >= threshold
                )
                if intersect_size >= threshold or count_match:
                    # An @todos, which says nothing about WHO: but anyone also
                    # named on their own in the text ("@todos ... @Ana ...")
                    # still gets a link; only the "everyone" part is dropped.
                    return self._individually_named_mentions(msg, mentioned)

        out = []
        seen = set()
        for jid in mentioned:
            if not jid or jid in seen:
                continue
            seen.add(jid)
            name = self._get_participant_name(jid)
            out.append((name, jid))
        return out

    def _update_mentions_panel(self, mentions: list):
        """Rebuild the @mention buttons below the messages list."""
        for child in list(self._mentions_panel.GetChildren()):
            if child is not self._mentions_label:
                child.Destroy()
        while self._mentions_sizer.GetItemCount() > 1:
            self._mentions_sizer.Remove(1)

        if not mentions:
            self._mentions_panel.Hide()
            self._current_mentions = []
            if self.conversation_panel.IsShown():
                self.conversation_panel.Layout()
            return

        self._current_mentions = mentions

        for display_name, jid in mentions:
            ctrl = wx.adv.HyperlinkCtrl(
                self._mentions_panel,
                id=wx.ID_ANY,
                label=f"@{display_name}",
                url=f"mention://{jid}",
                style=wx.adv.HL_DEFAULT_STYLE,
            )
            ctrl.Bind(
                wx.adv.EVT_HYPERLINK,
                lambda e, j=jid: self._on_mention_hyperlink(e, j),
            )
            ctrl.Bind(wx.EVT_KEY_DOWN, self._on_mention_display_key_down)
            self._mentions_sizer.Add(ctrl, 0, wx.LEFT | wx.BOTTOM, 3)

        self._mentions_panel.Show()
        self._mentions_panel.Layout()
        if self.conversation_panel.IsShown():
            self.conversation_panel.Layout()

    def _on_mention_open(self, jid: str):
        """Navigate to the conversation for the mentioned contact."""
        mw = self.main_window
        target_jid = jid
        # Resolve @lid <=> @s.whatsapp.net alternative JID if it was deduplicated
        alt_jid = getattr(mw, "_lid_to_phone", {}).get(jid) or getattr(mw, "_phone_to_lid", {}).get(jid)
        if alt_jid and alt_jid in mw.chats:
            target_jid = alt_jid
        
        chat = mw.get_chat(target_jid)
        if chat is None:
            name = self._get_participant_name(target_jid)
            chat = {"remoteJid": target_jid, "pushName": name}
        self.navigate_to_conversation(chat)

    def _on_mention_hyperlink(self, event, jid: str):
        """Intercept EVT_HYPERLINK on a mention display link to navigate instead of open URL."""
        event.Skip(False)
        self._on_mention_open(jid)

    def _on_mention_display_key_down(self, event):
        """Space/Enter on a mention HyperlinkCtrl activates it (like click)."""
        kc = event.GetKeyCode()
        if kc in (wx.WXK_RETURN, wx.WXK_SPACE, wx.WXK_NUMPAD_ENTER):
            ctrl = event.GetEventObject()
            jid = ctrl.GetURL().replace("mention://", "")
            self._on_mention_open(jid)
        else:
            event.Skip()

    # ── @mention input system ────────────────────────────────────────────────

    def _get_mention_query(self):
        """Return (start_pos, query) when cursor is inside @word, else (None, None)."""
        text = self.message_field.GetValue()
        pos  = self.message_field.GetInsertionPoint()
        i = min(pos - 1, len(text) - 1)
        while i >= 0:
            ch = text[i]
            if ch == "@":
                return (i, text[i + 1:pos])
            if ch in (" ", "\n", "\t"):
                break
            i -= 1
        return (None, None)

    def _hide_mention_suggestions(self):
        """Hide the mention suggestion list without announcing anything."""
        self._mention_active = False
        if hasattr(self, "_mention_panel") and self._mention_panel.IsShown():
            self._mention_panel.Hide()
            if self.conversation_panel.IsShown():
                self.conversation_panel.Layout()

    def _update_mention_suggestions(self, query: str):
        """Rebuild the suggestion list for the given query and show/hide the panel."""
        i18n = self.main_window.i18n
        q = query.lower()

        # Collect JIDs of everyone who has sent at least one message in the current conversation
        participants_who_sent_message = set()
        for msg in getattr(self, "_sorted_messages", []):
            if not isinstance(msg, dict):
                continue
            key = msg.get("key") or {}
            p_jid = key.get("participant") or msg.get("participant")
            if not p_jid and not msg.get("isGroupMsg", False):
                p_jid = key.get("remoteJid")
            if p_jid:
                p_jid = self.main_window._normalize_jid(p_jid)
                participants_who_sent_message.add(p_jid)
                # Map alternate formats
                phone_jid = getattr(self.main_window, "_lid_to_phone", {}).get(p_jid, "")
                if phone_jid:
                    participants_who_sent_message.add(phone_jid)
                lid_jid = getattr(self.main_window, "_phone_to_lid", {}).get(p_jid, "")
                if lid_jid:
                    participants_who_sent_message.add(lid_jid)

        def is_saved(jid):
            local = jid.rsplit("@", 1)[0]
            candidates = [jid]
            if jid.endswith("@lid"):
                phone = getattr(self.main_window, "_lid_to_phone", {}).get(jid, "")
                if phone:
                    candidates.append(phone)
                    candidates.append(phone.rsplit("@", 1)[0] + "@c.us")
            elif jid.endswith("@s.whatsapp.net"):
                candidates.append(local + "@c.us")
                lid = getattr(self.main_window, "_phone_to_lid", {}).get(jid, "")
                if lid:
                    candidates.append(lid)
            elif jid.endswith("@c.us"):
                candidates.append(local + "@s.whatsapp.net")

            for cjid in candidates:
                c = self.main_window.contacts.get(cjid)
                if c:
                    if c.get("isMyContact") or c.get("isSaved") or c.get("syncToAddressbook"):
                        return True
                    name = (c.get("name") or "").strip()
                    # main_window._is_bad_contact_name() instead of a third,
                    # independently-maintained copy of the same "sem nome"/
                    # "unknown" placeholder check (see _sender_label()'s
                    # _contact_name() for the other one and why they drift).
                    if name and not self.main_window._is_bad_contact_name(name):
                        return True
            return False

        # Individual participant matches filtered by rules:
        # Show if contact is saved in contacts OR has sent a message in the group
        matches = []
        for name, jid in self._group_participants_cache:
            norm_jid = self.main_window._normalize_jid(jid)
            if not q or q in name.lower() or q in norm_jid:
                matches.append((name, jid))

        logging.info(f"[mention] _update_mention_suggestions: query='{query}', cache_size={len(self._group_participants_cache)}, matches_size={len(matches)}")

        # Sort: names that start with the query come first, then those that
        # contain it but don't start with it — both groups sorted alphabetically.
        if q:
            matches.sort(key=lambda x: (0 if x[0].lower().startswith(q) else 1, x[0].lower()))

        # @all/@todos special entry — always at the top when query is empty or matches
        all_kw = i18n.t("mention_all_keyword")  # "todos" or "all"
        if not q or q in all_kw or q in "all" or q in "todos":
            matches = [("__ALL__", "@all")] + matches

        self._mention_suggestions = matches

        if not matches:
            was_visible = self._mention_panel.IsShown()
            self._hide_mention_suggestions()
            if was_visible:
                self.main_window.output(i18n.t("mention_no_suggestions"), interrupt=True)
            return

        self._mention_list.Clear()
        all_label = i18n.t("mention_all_label")
        for name, jid in matches:
            if jid == "@all":
                self._mention_list.Append(all_label)
            else:
                self._mention_list.Append(f"@{name}")

        self._mention_panel.Show()
        self._mention_panel.Layout()
        if self.conversation_panel.IsShown():
            self.conversation_panel.Layout()
        self._mention_list.SetSelection(0)
        self.main_window.output(i18n.t("mention_suggestions_available"), interrupt=False)

    def _on_text_changed_mention_check(self):
        """Called from on_change_message_field to detect and update @mention suggestions."""
        if self.conversation is None:
            return
        jid = self.conversation.get("remoteJid", "")
        if not jid.endswith("@g.us"):
            if self._mention_panel.IsShown():
                self._hide_mention_suggestions()
            return
        start, query = self._get_mention_query()
        if start is None:
            if self._mention_panel.IsShown():
                self._hide_mention_suggestions()
                self._mention_active = False
            return

        # Fail-safe: if cache is empty, fetch participants now in background
        if not getattr(self, "_group_participants_cache", None):
            logging.info(f"[mention] Cache empty on mention check. Triggering lazy fetch for group {jid}...")
            threading.Thread(
                target=self._fetch_group_participants,
                args=(jid,),
                daemon=True,
            ).start()

        self._mention_active = True
        self._mention_start_pos = start
        self._mention_query = query
        self._update_mention_suggestions(query)

    def _insert_mention(self, display_name: str, jid: str):
        """Replace the current @query in the field with @display_name and track the JID."""
        # Use cached start/query so this works even when message_field doesn't
        # have focus (e.g. when called from the mention list via EVT_CHAR_HOOK).
        start = self._mention_start_pos
        query = self._mention_query
        if start < 0:
            return
        i18n = self.main_window.i18n

        if jid == "@all":
            # @todos/@all: use the localized keyword as the inserted text and add
            # every group participant JID to the pending mentions list.
            all_kw = i18n.t("mention_all_keyword")   # "todos" or "all"
            replacement = f"@{all_kw} "
            for p_name, p_jid in self._group_participants_cache:
                if p_jid not in self._pending_mentions:
                    self._pending_mentions.append(p_jid)
                # Always store the display name so pill buttons show names, not LIDs.
                if p_jid not in self._pending_mention_display_names:
                    self._pending_mention_display_names[p_jid] = p_name or p_jid.rsplit("@", 1)[0]
        else:
            replacement = f"@{display_name} "
            if jid not in self._pending_mentions:
                self._pending_mentions.append(jid)
            self._pending_mention_display_names[jid] = display_name

        text = self.message_field.GetValue()
        new_text = text[:start] + replacement + text[start + 1 + len(query):]
        # ChangeValue does NOT fire EVT_TEXT, preventing a mention-check loop.
        self.message_field.ChangeValue(new_text)
        self.message_field.SetInsertionPoint(start + len(replacement))
        self._hide_mention_suggestions()
        self._mention_active = False
        self._rebuild_mention_pills()
        self.message_field.SetFocus()

    def _rebuild_mention_pills(self):
        """Rebuild the pending-mention pill buttons panel (one row per @mention)."""
        i18n = self.main_window.i18n
        panel = self._pending_mentions_panel
        sizer = self._pending_mentions_sizer

        # Destroy existing pill widgets.
        for child in list(panel.GetChildren()):
            child.Destroy()
        sizer.Clear(delete_windows=False)

        if not self._pending_mentions:
            panel.Hide()
            if self.conversation_panel.IsShown():
                self.conversation_panel.Layout()
            return

        for jid in list(self._pending_mentions):
            display = self._pending_mention_display_names.get(jid) or jid.rsplit("@", 1)[0]
            row = wx.BoxSizer(wx.HORIZONTAL)
            lbl = wx.StaticText(panel, label=f"@{display}")
            btn_label = i18n.t("remove_mention").format(name=display)
            btn = wx.Button(panel, label=btn_label)
            # Capture jid/display in closure.
            def _make_handler(j, d):
                def _handler(evt):
                    self._on_remove_mention(j, d)
                return _handler
            btn.Bind(wx.EVT_BUTTON, _make_handler(jid, display))
            row.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
            row.Add(btn, 0, wx.ALIGN_CENTER_VERTICAL)
            sizer.Add(row, 0, wx.LEFT | wx.BOTTOM, 3)

        panel.Show()
        panel.Layout()
        if self.conversation_panel.IsShown():
            self.conversation_panel.Layout()

    def _on_remove_mention(self, jid: str, display: str):
        """Remove a pending @mention pill and its text from the message field."""
        # Remove @display from message text if present.
        text = self.message_field.GetValue()
        # Try removing "@display " (with trailing space) first, then "@display" alone.
        if f"@{display} " in text:
            new_text = text.replace(f"@{display} ", "", 1)
        elif f"@{display}" in text:
            new_text = text.replace(f"@{display}", "", 1)
        else:
            new_text = text
        if new_text != text:
            self.message_field.ChangeValue(new_text)

        # Remove from pending state.
        if jid in self._pending_mentions:
            self._pending_mentions.remove(jid)
        self._pending_mention_display_names.pop(jid, None)

        self._rebuild_mention_pills()
        self.message_field.SetFocus()

    def _fetch_group_participants(self, jid: str):
        """Background: fetch participants for the group and populate the cache with retries if session is loading."""
        max_retries = 3
        delay = 3
        for attempt in range(max_retries):
            # Check if this chat is still the active conversation before retrying
            if not self.conversation or self.conversation.get("remoteJid") != jid:
                logging.info(f"[mention] Active conversation changed. Aborting fetch for {jid}.")
                return
            try:
                data = self.main_window.get_group_info_recent(jid)
                participants = data.get("participants", [])
                logging.info(f"[mention] get_group_info({jid}) attempt {attempt+1}/{max_retries} → {len(participants)} participants")
                if participants:
                    my_jid = getattr(self.main_window, "my_jid", "") or ""
                    # Build initial cache first so UI is populated instantly
                    cache = []
                    for p in participants:
                        if not isinstance(p, dict):
                            continue
                        p_jid = p.get("id", "")
                        if not p_jid:
                            continue
                        if my_jid and p_jid.split("@")[0] == my_jid.split("@")[0]:
                            continue  # skip self
                        name = self._get_participant_name(p_jid, p)
                        cache.append((name, p_jid))
                    cache.sort(key=lambda x: x[0].lower())
                    logging.info(f"[mention] cache built: {[n for n,_ in cache]}")
                    wx.CallAfter(self._set_group_participants_cache, cache)
                    return
            except Exception as e:
                logging.error(f"[mention] _fetch_group_participants error on attempt {attempt+1}: {e}", exc_info=True)
            
            if attempt < max_retries - 1:
                logging.info(f"[mention] Empty participants response, retrying in {delay}s...")
                time.sleep(delay)

    def _set_group_participants_cache(self, cache: list):
        """Main-thread callback: store cache and refresh suggestions if active."""
        self._group_participants_cache = cache
        if self._mention_active:
            self._update_mention_suggestions(self._mention_query)
