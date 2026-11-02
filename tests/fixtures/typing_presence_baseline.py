# Historical WinZapp source fixture, GPL-3.0-or-later (same license as this repository).
# Source: gabrielhhaber/WinZapp_Python, 4118f4f4e2f98cc722c91c8452c1722c7b94c664
# Original: client/main_window/chat_events.py, ChatEventsMixin.on_presence_update
# Test reference only. Do not update this historical behavior with the feature.

def on_presence_update(self, jid: str, presences: dict, *, refresh_offline=True):
        """Update the presence cache and composing indicators. Speaks changes
        via AO2 when the active conversation has a new composing event, and refreshes
        the data-button note for the open conversation.

        presences: {jid_str: {"lastKnownPresence": str, "lastSeen": int|None}, ...}
        """
        if (getattr(self, "_shutting_down", False) or not isinstance(jid, str)
                or not jid or not isinstance(presences, dict) or not presences):
            return
        logging.info("[on_presence_update] entries=%s", len(presences))

        chat_jid_norm = self._normalize_jid(jid)
        if chat_jid_norm.endswith("@lid"):
            chat_jid_norm = self._lid_to_phone.get(chat_jid_norm, chat_jid_norm)

        presence_scope = chat_jid_norm if chat_jid_norm.endswith("@g.us") else None
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
        # Participants this event reports typing/recording — the open
        # conversation's typing row shows them again even if their last
        # message had taken them off it.
        _fresh_typing = set()
        for participant_jid, data in presences.items():
            if getattr(self, "_shutting_down", False):
                return
            if not isinstance(participant_jid, str) or not participant_jid or not isinstance(data, dict):
                continue
            if not isinstance(data.get("lastKnownPresence", ""), str):
                # A malformed activity cannot grant visibility or start work.
                # Still honor an explicit denial as a clear-only observation.
                if data.get("restricted") is not True:
                    continue
                data = {"restricted": True, "lastSeen": None,
                        "eventTimestamp": data.get("eventTimestamp")}
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

            previous = cached(self, canonical, fresh=False, chat=presence_scope)
            old_lkp = cached(self, canonical, chat=presence_scope).get("lastKnownPresence", "")
            new_lkp = data.get("lastKnownPresence", "unavailable")

            entry = merge(self, canonical, data, chat=presence_scope)
            if entry is previous:
                continue  # An older event must not restart a typing timer/speech.
            new_lkp = entry.get("lastKnownPresence", "")

            # The list displays typing/recording, not online/offline. An
            # online-only burst must not scan/reformat every conversation.
            old_activity = composing_chats.get(chat_jid_norm, {}).get(canonical)
            new_activity = new_lkp if new_lkp in ("composing", "recording") else None
            # The cache may have expired before its queued timer clears the
            # visible suffix. Compare the index actually used by that suffix.
            if new_activity != old_activity:
                presence_changed = True

            # Update composing/recording index for this chat
            if chat_jid_norm not in composing_chats:
                composing_chats[chat_jid_norm] = {}
            timer_key = (chat_jid_norm, canonical)
            if new_lkp in ("composing", "recording"):
                composing_chats[chat_jid_norm][canonical] = new_lkp
                _fresh_typing.add(canonical)
                # Reset the 10-second auto-clear timer on every new event
                old_timer = self._presence_timers.pop(timer_key, None)
                if old_timer is not None:
                    try:
                        old_timer.Stop()
                    except Exception:
                        pass
                def _make_clear(cjid, part):
                    def _clear():
                        # Stop() cannot retract an already queued callback.
                        # Only this chat/participant's current timer owns expiry.
                        if self._presence_timers.get((cjid, part)) is not timer:
                            return
                        self._composing_chats.get(cjid, {}).pop(part, None)
                        self._presence_timers.pop((cjid, part), None)
                        if getattr(self, "_shutting_down", False):
                            return
                        request_presence_ui(self, cjid, row=True)
                        # A missing final push must not leave expired activity
                        # waiting for the next periodic active-chat tick.
                        if refresh_offline:
                            refresh_missing_presence(self, cjid)
                    timer = wx.CallLater(10_000, _clear)
                    return timer
                self._presence_timers[timer_key] = _make_clear(chat_jid_norm, canonical)
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
                # The open conversation only counts while it is on screen: one
                # left open behind another panel (Alt+4, Status, Calls, ...) is
                # not being read, same gate as mark-as-read and the sounds.
                in_view = conversation_in_view(panel)
                # Typing/recording indicators are only meaningful while the user
                # is actually looking at WinZapp — a conversation left open when
                # the window was minimized to the tray must not keep announcing.
                window_active = (
                    not getattr(self, "_shutting_down", False)
                    and not getattr(self, "_window_hidden", False)
                    and self.IsShown()
                    and not self.IsIconized()
                    and self.IsActive()
                )
                logging.info("[on_presence_update] announce_enabled=%s, is_active_chat=%s, window_active=%s, in_view=%s",
                             announce_enabled, active_match, window_active, in_view)
                if announce_enabled and active_match and window_active and in_view:
                    # Mute/archive suppress background notifications, not the
                    # live state of a conversation the user deliberately has
                    # open. The active-chat and active-window gates above are
                    # the relevant boundary for typing/recording speech.
                    name = self._resolve_jid_name(canonical, chat_jid_norm)
                    logging.info("[on_presence_update] display name available=%s", bool(name))
                    if name:
                        try:
                            i18n_key = "typing_text" if new_lkp == "composing" else "recording_text"
                            msg_text = self.i18n.t(i18n_key).format(name=name)
                            logging.info("[on_presence_update] announcing activity")
                            self.speak_output.output(msg_text)
                        except Exception:
                            logging.error("[on_presence_update] activity announcement failed")

        if getattr(self, "_shutting_down", False):
            return
        # Persist the updated pushName map to database metadata.
        if _ppm_updated and hasattr(self, "db") and self.db is not None:
            try:
                self.db.queue_presence_names(self._presence_pushname_map)
            except Exception as db_err:
                logging.warning("[on_presence_update] name snapshot not queued: %s", type(db_err).__name__)

        if getattr(self, "_shutting_down", False):
            return
        request_presence_ui(self, chat_jid_norm, row=presence_changed or _ppm_updated,
                            fresh=_fresh_typing)

        # Refresh the data-button note for the open conversation
        if panel is None or conv is None:
            return
        # WPP pushes the state, but not chatstate.t. Ask for the current
        # snapshot when this contact goes offline instead of waiting for a tick.
        if (refresh_offline and is_active_chat(chat_jid_norm, conv_jid)
                and not conv_jid.endswith("@g.us")
                and not cached(self, conv_jid).get("restricted")
                and cached(self, conv_jid).get("lastKnownPresence") not in ONLINE
                and hasattr(self, "_refresh_open_contact_presence")):
            self._refresh_open_contact_presence()
