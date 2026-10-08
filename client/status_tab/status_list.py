"""StatusListMixin — part of StatusPanel (see status_tab/__init__.py).

Moved verbatim out of status_panel.py. Methods run with ``self`` bound to
the StatusPanel instance, so every attribute set in StatusPanel.__init__/
init_UI is available here.
"""

from ui.shortcut_bindings import command_key_event
import threading
import wx
from status_tab.status_rules import _status_content_label


class StatusListMixin:
    """The contacts-with-status list: populating it, row text, key handling,
    selection and activation.
    """

    def _set_list_loading(self):
        self._list_is_loading = True
        i18n = self.main_window.i18n
        self._status_list.DeleteAllItems()
        self._status_list.Append((i18n.t("status_loading"),))

    @staticmethod
    def _latest_ts(entry: dict) -> int:
        """Return the highest messageTimestamp among a contact's statuses."""
        return max(
            (int(s.get("messageTimestamp", 0) or 0) for s in entry.get("statuses", [])),
            default=0,
        )

    def _status_preview(self, status: dict, i18n) -> str:
        """Return a short human-readable preview of a single status item."""
        msg_type = status.get("messageType", "")
        msg_obj  = status.get("message") or {}
        return _status_content_label(msg_type, msg_obj, i18n, getattr(self.main_window, "settings", None))

    def _populate_list(self, my_statuses: list, contacts: list):
        i18n = self.main_window.i18n

        # Sort contacts by most-recent status timestamp (newest first)
        contacts = sorted(contacts, key=self._latest_ts, reverse=True)
        # Within each contact keep statuses newest-first too
        for entry in contacts:
            entry["statuses"] = sorted(
                entry.get("statuses", []),
                key=lambda s: int(s.get("messageTimestamp", 0) or 0),
                reverse=True,
            )

        # Split into "Recentes" (at least one status not yet opened) and
        # "Vistos" (every current status already opened) sections, each
        # still newest-first internally — matches the official client's own
        # unseen/seen ring distinction. _status_contacts keeps the flat,
        # concatenated order (recent section first) so every OTHER index
        # into it (_selected_contact_idx, _is_current_status_playable(), …)
        # keeps working unchanged; only the list widget itself gets the
        # extra header rows, tracked via _status_row_contact.
        recent_contacts = [e for e in contacts if not e.get("viewed_all", False)]
        viewed_contacts = [e for e in contacts if e.get("viewed_all", False)]
        contacts = recent_contacts + viewed_contacts

        self._my_statuses          = my_statuses
        self._status_contacts      = contacts
        self._selected_contact_idx = -1
        self._list_is_loading      = False
        self._viewer_panel.Hide()
        self._status_list.DeleteAllItems()
        self._status_row_contact = {}
        self._status_contact_row = {}

        # ── Row 0: always "My Status" ─────────────────────────────────────
        self._status_list.Append((self._my_status_label(i18n),))
        self._status_row_contact[0] = -1

        def _add_section(section_contacts: list, header: str, start_contact_idx: int):
            """Appends one "--- header ---" row followed by one row per
            contact in *section_contacts*. Returns the next free contact
            index (for the following section to continue numbering from)."""
            if not section_contacts:
                return start_contact_idx
            row = self._status_list.GetItemCount()
            self._status_list.Append((f"— {header} —",))
            self._status_row_contact[row] = -1
            contact_idx = start_contact_idx
            for entry in section_contacts:
                row_text = self._status_row_text(entry, i18n)
                row = self._status_list.GetItemCount()
                self._status_list.Append((row_text,))
                self._status_row_contact[row] = contact_idx
                self._status_contact_row[contact_idx] = row
                contact_idx += 1
            return contact_idx

        next_idx = _add_section(recent_contacts, i18n.t("status_recent_updates"), 0)
        _add_section(viewed_contacts, i18n.t("status_viewed_updates"), next_idx)

        if self._status_list.GetItemCount() > 0:
            self._status_list.Focus(0)
            self._status_list.Select(0)
        self.Layout()

    def _status_row_text(self, entry: dict, i18n, nav_info: str = "", status: dict = None) -> str:
        """*status*, when given, overrides which of the contact's statuses
        the preview text is built from — used by _update_focused_status_row_
        text() so the row reflects whatever status is actually being
        navigated to, not always the newest one (statuses[0], the default
        used everywhere else this is called from, e.g. initial population)."""
        name     = entry.get("name", "")
        statuses = entry.get("statuses", [])
        preview_source = status if status is not None else (statuses[0] if statuses else None)
        if preview_source is not None:
            preview = self._status_preview(preview_source, i18n)
            base = f"{name}: {preview}" if preview else name
        else:
            base = name
        return f"{base}, {nav_info}" if nav_info else base

    def _update_focused_status_row_text(self):
        """Appends ", status X de Y" to the list row of whichever contact
        is currently open in the viewer, reflecting _current_status_idx —
        called from _show_current_status() so it stays correct both on
        first opening a contact and on Ctrl+Left/Right navigation between
        their own statuses. The preview text itself is rebuilt from the
        actual status being viewed (not always the newest one) so the row
        doesn't keep announcing the first status after navigating away
        from it."""
        idx = self._selected_contact_idx
        if idx < 0 or idx >= len(self._status_contacts):
            return
        row = self._status_contact_row.get(idx)
        if row is None:
            return
        entry    = self._status_contacts[idx]
        i18n     = self.main_window.i18n
        statuses = entry.get("statuses", [])
        current_idx = self._current_status_idx
        if not (0 <= current_idx < len(statuses)):
            return
        nav_info = i18n.t("status_of").format(
            current=current_idx + 1, total=len(statuses)
        )
        self._status_list.SetItemText(
            row, self._status_row_text(entry, i18n, nav_info, status=statuses[current_idx])
        )

    def _my_status_label(self, i18n) -> str:
        if self._my_statuses:
            suffix = i18n.t("my_status_update")
        else:
            suffix = i18n.t("my_status_none")
        return f"{i18n.t('my_status')}: {suffix}"

    def _is_current_status_playable(self, contact_idx: int) -> bool:
        """True when *contact_idx* is the contact already being shown AND
        its current status is a video/audio update — the case where
        Enter/Space on the status list should toggle play/pause instead of
        re-selecting (which would stop() and restart the player instead of
        actually pausing it — see _show_current_status())."""
        return (
            contact_idx == self._selected_contact_idx
            and self._current_status is not None
            and self._current_status.get("messageType") in ("videoMessage", "audioMessage")
        )

    def _use_status_media_viewer_dialog(self) -> bool:
        """True (default) opens a status in the dedicated, full
        MediaViewerDialog; False keeps the classic in-panel inline viewer
        instead. Settings > Interface do usuário > "Mostrar os status em
        player separado"."""
        return self.main_window.settings.get("user_interface", {}).get(
            "status_media_viewer_dialog", True
        )

    def _on_status_list_key_down(self, event):
        """Space opens the focused status exactly like Enter.

        Plain arrow navigation only changes the selected contact. It never
        opens a status and therefore never marks anything as viewed.
        """
        event = command_key_event(self, 'status_list', event)
        if event.GetKeyCode() != wx.WXK_SPACE:
            event.Skip()
            return
        idx = self._status_list.GetFocusedItem()
        if idx < 0:
            return
        if idx == 0:
            if not self._use_status_media_viewer_dialog():
                self._status_list.Select(idx)
            self._open_my_status_dialog()
            return
        contact_idx = self._status_row_contact.get(idx, -1)
        if contact_idx < 0 or contact_idx >= len(self._status_contacts):
            return

        if not self._use_status_media_viewer_dialog():
            # Play/pause toggle deliberately checked BEFORE Select(idx) runs
            # below: Select() re-fires EVT_LIST_ITEM_SELECTED even for an
            # already-selected row, which would otherwise stop() the player
            # out from under this toggle a moment later — see
            # _is_current_status_playable()'s docstring.
            if self._is_current_status_playable(contact_idx):
                self._on_play_pause_video(None)
                return
            self._status_list.Select(idx)
            self._selected_contact_idx = contact_idx
            self._show_current_status()
            return

        if contact_idx != self._selected_contact_idx:
            self._current_status_idx = 0
        self._selected_contact_idx = contact_idx
        self._open_status_media_viewer(contact_idx)

    def _on_refresh(self, event):
        threading.Thread(target=self._load_statuses, daemon=True).start()

    # ── Status list selection / activation ───────────────────────────────────

    def _on_status_contact_selected(self, event, announce: bool = False):
        """Track focus. In classic (non-dialog) mode this also drives the
        inline viewer directly — see _use_status_media_viewer_dialog()."""
        idx = event.GetIndex()

        if not self._use_status_media_viewer_dialog():
            if idx == 0:
                # My Status row selected — hide the inline viewer; dialog
                # opens on activate.
                self._selected_contact_idx = -1
                self._viewer_panel.Hide()
                self.Layout()
                return
            contact_idx = self._status_row_contact.get(idx, -1)
            if contact_idx < 0 or contact_idx >= len(self._status_contacts):
                self._viewer_panel.Hide()
                self.Layout()
                return
            # Only jump back to the FIRST status when selecting a genuinely
            # different contact. This event also fires from Select() calls
            # elsewhere (e.g. Space re-activating the row the list already
            # has focused, while the user has since moved forward within
            # the viewer via Ctrl+Left/Right) — resetting unconditionally
            # meant pressing Space while sitting on "status 3 de 5" silently
            # snapped it back to "1 de 5" for no reason.
            if contact_idx != self._selected_contact_idx:
                self._current_status_idx = 0
            self._selected_contact_idx = contact_idx
            # Defaults to silent: NVDA/JAWS already read the newly-focused
            # list item on their own on plain arrow-key navigation (EVT_
            # LIST_ITEM_SELECTED) — see _show_current_status()'s own
            # docstring. Callers driven by an explicit action rather than
            # mere focus movement (Space, Enter/double-click activation)
            # pass announce=True.
            self._show_current_status(announce=announce)
            return

        # Dialog mode: the old inline viewer is deliberately not used for
        # passive list navigation. Keeping it hidden is also important for
        # screen readers: arrowing the list should announce only the list
        # item — the dialog only opens on an explicit activation.
        try:
            self._video_player.stop()
        except Exception:
            pass
        self._viewer_panel.Hide()
        self.Layout()

        if idx == 0:
            self._selected_contact_idx = -1
            return
        contact_idx = self._status_row_contact.get(idx, -1)
        if contact_idx < 0 or contact_idx >= len(self._status_contacts):
            return
        if contact_idx != self._selected_contact_idx:
            self._current_status_idx = 0
        self._selected_contact_idx = contact_idx

    def _on_status_contact_activated(self, event):
        idx = event.GetIndex()
        if idx == 0:
            self._open_my_status_dialog()
            return
        contact_idx = self._status_row_contact.get(idx, -1)
        if contact_idx < 0 or contact_idx >= len(self._status_contacts):
            return

        if not self._use_status_media_viewer_dialog():
            if self._is_current_status_playable(contact_idx):
                self._on_play_pause_video(None)
                return
            self._on_status_contact_selected(event, announce=True)
            return

        if contact_idx != self._selected_contact_idx:
            self._current_status_idx = 0
        self._selected_contact_idx = contact_idx
        self._open_status_media_viewer(contact_idx)
