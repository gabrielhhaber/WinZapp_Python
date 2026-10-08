"""ChatListSelectionMixin — the multi-selection of chats, shared by every list
of chats (the conversations list, the archived list, the locked list).

The selection model was born inside ChatSelectionMixin, welded to the main
conversations list. The archived and locked lists had no way to select chats at
all, so the model moved here and each surface plugs into it. Whatever a surface
does not share is a hook it overrides:

* ``_repaint_chat_selection()`` — repaint the rows after the set changed (the
  "selecionado" suffix lives in the row text). Default: the main list's.
* ``_selection_owner()`` — the object holding ``selected_chats`` state; the
  surface itself.

Attributes the host must provide: ``main_window``, ``chats_list``,
``conversations_list``, ``selected_chats`` (a set of remoteJids) and
``selection_sound``.

Deliberately here, once, so the three lists cannot drift: the keys (Ctrl+Space,
Space in selection mode, Shift+Up/Down/Home/End, Ctrl+Shift+Space), the
"Selecionado"/"Desmarcado"/"Modo de seleção ativado" announcements, the sound,
and the three ``user_interface`` settings that steer them.
"""

from ui.shortcut_bindings import command_key_event
import wx
from ui.dialogs.clear_chat_confirm import confirm_clear_chat


class ChatListSelectionMixin:
    """Selecting chats in a list and the settings/announcements around it."""

    # ── Hooks ────────────────────────────────────────────────────────────────

    def _repaint_chat_selection(self) -> None:
        """Repaint the rows after ``selected_chats`` changed. The main
        conversations list rebuilds through MainWindow.add_chats_to_ui(); the
        archived and locked lists override this with their own repaint."""
        self.main_window.add_chats_to_ui()

    # ── Selection helpers ────────────────────────────────────────────────────

    def _select_chat_at(self, idx: int) -> bool:
        """Add the chat at *idx* to self.selected_chats. Returns whether it
        was added (i.e. wasn't already selected)."""
        if not (0 <= idx < len(self.chats_list)):
            return False
        jid = self.chats_list[idx].get("remoteJid", "")
        if not jid or jid in self.selected_chats:
            return False
        self.selected_chats.add(jid)
        return True

    def _all_chat_jids(self) -> list:
        return [c.get("remoteJid", "") for c in self.chats_list if c.get("remoteJid", "")]

    def _chat_selection_visible(self) -> bool:
        """Whether any chat currently listed in chats_list is selected.

        The gate for a chat list's selection mode (issue #99), and
        deliberately narrower than `bool(self.selected_chats)`: chats_list is
        reassigned to the *filtered* list every time the conversation filter
        RadioBox or the search box changes (MainWindow.add_chats_to_ui, which
        rebuilds it from the unfiltered _all_chats_list on every pass), and
        nothing clears selected_chats when it does. So a user who selects two
        chats and then switches to "Não lidas" sees no selection and was told
        nothing, yet plain Space would still have quietly added a third chat to
        a selection they believe does not exist. Hiding every selected chat now
        takes Space back to its native behaviour, which is what the user
        perceives; clearing the filter brings the mode back.

        The mass actions deliberately still act on the whole set — a user who
        selects and then filters expects them to hit everything they picked,
        and that behaviour predates the selection mode. This only gates what
        the unmodified Space key does.
        """
        if not self.selected_chats:
            return False
        return any(
            c.get("remoteJid", "") in self.selected_chats for c in self.chats_list
        )

    def _announce_chat_selected(self, was_active: bool) -> None:
        """Sound + "Selecionado" (+ the mode transition) after chats were added."""
        self.selection_sound.play()
        self.main_window.output(
            self._selection_mode_announcement(
                self.main_window.i18n.t("selected"),
                was_active, self._chat_selection_visible()),
            interrupt=True)

    def _toggle_chat_selection(self, idx: int) -> bool:
        """Toggle the chat at *idx* in self.selected_chats, repaint the list
        and announce the change. Shared by Ctrl+Space and, once a selection
        exists, plain Space (_handle_chat_selection_key).

        Returns whether anything was actually toggled — plain Space hands the
        key back to the control when it wasn't (no focused row, or a row with
        no jid), rather than consuming a keystroke with no sound, speech or
        effect. Ctrl+Space keeps swallowing it, as it always has.

        The was_active/is_active bookkeeping below reads
        _chat_selection_visible(), not the raw set, because that is what
        _handle_chat_selection_key() gates plain Space on — and the announcement
        has to name the mode the gate actually enforces. Reading the raw set
        here left the two disagreeing: with one selected chat hidden by the
        filter, Ctrl+Space on a visible one announced no mode change (the raw
        set was already non-empty), deselecting it again announced none
        either, and the next plain Space then fell through to the control with
        nothing to show for it — the silent dead key, one surface over.
        """
        if not (0 <= idx < len(self.chats_list)):
            return False
        jid = self.chats_list[idx].get("remoteJid", "")
        if not jid:
            return False
        was_active = self._chat_selection_visible()
        if jid in self.selected_chats:
            self.selected_chats.remove(jid)
            self._repaint_chat_selection()
            self.main_window.output(
                self._selection_mode_announcement(
                    self.main_window.i18n.t("unselected"),
                    was_active, self._chat_selection_visible()),
                interrupt=True)
        else:
            self.selected_chats.add(jid)
            self._repaint_chat_selection()
            self._announce_chat_selected(was_active)
        return True

    # ── Settings (Settings > User Interface) ─────────────────────────────────

    def _bulk_shortcuts_enabled(self) -> bool:
        """Settings > User Interface > "Substituir atalhos por ações em massa
        ao selecionar conversas e mensagens" (default on). When enabled and a
        selection exists, the single-item shortcuts (forward, save, clear,
        delete, ...) act on the whole selection instead."""
        return self.main_window.settings.get("user_interface", {}).get(
            "bulk_action_shortcuts", True
        )

    def _selection_mode_enabled(self) -> bool:
        """Settings > User Interface > "Usar Espaço para marcar/desmarcar
        enquanto houver algo marcado" (default on). When enabled and the
        surface already has a selection, plain Space keeps selecting instead of
        doing its normal job (issue #99)."""
        return self.main_window.settings.get("user_interface", {}).get(
            "space_selects_in_selection_mode", True
        )

    def _escape_clears_selection_enabled(self) -> bool:
        """Settings > User Interface > "Esc desmarca as mensagens marcadas
        antes de fechar a conversa" (default on) — see
        _on_escape_conversation()."""
        return self.main_window.settings.get("user_interface", {}).get(
            "escape_clears_selection", True
        )

    def _selection_mode_announcement(self, base: str, was_active: bool, is_active: bool) -> str:
        """Append the selection-mode transition to *base* when the mode just
        turned on or off, and return the combined line.

        One string rather than a second output() call: every announcement here
        goes through output(..., interrupt=True), so speaking twice would cut
        "Selecionado" off mid-word with "Modo de seleção ativado".

        *base* is already-translated text and the two booleans are passed
        explicitly, so the forward dialog can use this against its own local
        set — the mode is derived from whether a selection exists, never
        stored.

        Deliberately not called from the mass actions (forward, save, delete,
        ...): they clear the selection as a side effect of acting on it and
        already announce their own result, and "5 mensagens encaminhadas. Modo
        de seleção desativado" is noise. The derived mode still ends correctly
        there, since it only ever reads the set.
        """
        if not self._selection_mode_enabled():
            return base
        if is_active and not was_active:
            return f"{base}. {self.main_window.i18n.t('selection_mode_on')}"
        if was_active and not is_active:
            return f"{base}. {self.main_window.i18n.t('selection_mode_off')}"
        return base

    # ── Keys ─────────────────────────────────────────────────────────────────

    def _handle_chat_selection_key(self, event) -> bool:
        """The selection keys of a chat list. Returns True when the key was
        dealt with (the caller must not process it further) and False when it
        is none of the selection keys, so the caller carries on with its own.

        Ctrl+Space toggles the focused chat's membership in
        self.selected_chats (the mass actions act on that set). Plain Space
        does the same once a selection already exists ("selection mode",
        issue #99) and otherwise keeps its old meaning on that surface.
        Shift+Up/Down extend the selection to the previous/next row;
        Shift+Home/Shift+End select every row above/below the focused one and
        move focus to the first/last row; Ctrl+Shift+Space selects every chat,
        or clears the selection if everything is already selected.
        """
        event = command_key_event(self, 'chat_selection', event)
        key   = event.GetKeyCode()
        ctrl  = event.ControlDown()
        shift = event.ShiftDown()
        lst   = self.conversations_list
        idx   = lst.GetFocusedItem()
        total = len(self.chats_list)

        if shift and key in (wx.WXK_DOWN, wx.WXK_NUMPAD_DOWN,
                             wx.WXK_UP, wx.WXK_NUMPAD_UP):
            down = key in (wx.WXK_DOWN, wx.WXK_NUMPAD_DOWN)
            target = (idx + 1 if down else idx - 1) if idx >= 0 else 0
            if 0 <= target < total:
                # Every announcement on a chat list derives the mode from
                # _chat_selection_visible(), the same predicate plain Space is
                # gated on below — see _toggle_chat_selection().
                was_active = self._chat_selection_visible()
                lst.Focus(target)
                lst.Select(target, True)
                lst.EnsureVisible(target)
                if self._select_chat_at(target):
                    self._repaint_chat_selection()
                    self._announce_chat_selected(was_active)
            return True

        if shift and key in (wx.WXK_HOME, wx.WXK_NUMPAD_HOME,
                             wx.WXK_END, wx.WXK_NUMPAD_END):
            to_end = key in (wx.WXK_END, wx.WXK_NUMPAD_END)
            if total > 0:
                idx0 = idx if idx >= 0 else 0
                lo, hi = (idx0, total - 1) if to_end else (0, idx0)
                was_active = self._chat_selection_visible()
                selected_any = False
                for i in range(lo, hi + 1):
                    if self._select_chat_at(i):
                        selected_any = True
                target = total - 1 if to_end else 0
                lst.Focus(target)
                lst.Select(target, True)
                lst.EnsureVisible(target)
                if selected_any:
                    self._repaint_chat_selection()
                    self._announce_chat_selected(was_active)
            return True

        if ctrl and shift and key == wx.WXK_SPACE:
            all_jids = self._all_chat_jids()
            was_active = self._chat_selection_visible()
            if all_jids and all(j in self.selected_chats for j in all_jids):
                self.selected_chats.clear()
                self._repaint_chat_selection()
                self.main_window.output(
                    self._selection_mode_announcement(
                        self.main_window.i18n.t("all_unselected"),
                        was_active, self._chat_selection_visible()),
                    interrupt=True)
            elif all_jids:
                self.selected_chats.update(all_jids)
                self._repaint_chat_selection()
                self.selection_sound.play()
                self.main_window.output(
                    self._selection_mode_announcement(
                        self.main_window.i18n.t("all_selected"),
                        was_active, self._chat_selection_visible()),
                    interrupt=True)
            return True

        # Plain Space keeps selecting once a selection exists (issue #99).
        # With nothing selected it has no meaning here, so it keeps falling
        # through to what the surface did before — and so it does when the
        # toggle itself refuses (no focused row, or a row with no jid), rather
        # than swallowing the key with nothing to show for it.
        if (key == wx.WXK_SPACE and not ctrl and not shift
                and self._selection_mode_enabled() and self._chat_selection_visible()):
            if not self._toggle_chat_selection(idx):
                event.Skip()
            return True

        if ctrl and not shift and key == wx.WXK_SPACE:
            self._toggle_chat_selection(idx)
            return True

        return False

    def _on_chat_row_focused_sound(self, jid: str) -> None:
        """Play the "selected" cue when focus lands on an already selected row,
        so the selection is audible while arrowing through the list."""
        if jid and jid in self.selected_chats:
            self.selection_sound.play()

    def _run_bulk_chat_action(self, handler, event):
        """The dedicated mass-action shortcuts are inert without a selection,
        mirroring how a chat list's "Ações em massa" submenu isn't built until
        conversations are selected — and announce that rather than doing
        nothing at all, which reads as a broken shortcut to a screen-reader
        user."""
        if not self.selected_chats:
            self.main_window.output(
                self.main_window.i18n.t("bulk_no_chat_selection"), interrupt=True
            )
            return
        handler(event)

    # ── Mass actions every chat list shares ─────────────────────────────────

    def _append_chat_mass_menu(self, menu, entries) -> None:
        """Add the "Ações em massa" submenu, then a separator, to a chat row's
        context menu. *entries* is ``[(i18n_key, shortcut, handler), ...]``;
        every entry shows its dedicated shortcut, which works whatever
        "Substituir atalhos por ações em massa..." is set to. The one place
        the submenu is built, so the three lists cannot drift apart."""
        i18n = self.main_window.i18n
        mass_menu = wx.Menu()
        for key, shortcut, handler in entries:
            item = mass_menu.Append(wx.ID_ANY, f"{i18n.t(key)}\t{shortcut}")
            self.Bind(wx.EVT_MENU, handler, item)
        menu.AppendSubMenu(mass_menu, i18n.t("mass_actions"))
        menu.AppendSeparator()

    def _prune_stale_chat_selection(self, chats) -> None:
        """Forget selected chats that are no longer in *chats* (unarchived,
        deleted, vault locked). A jid nobody can see would otherwise keep
        ``selected_chats`` non-empty, and with it the bulk shortcuts
        overriding the single-chat ones for a selection the user cannot see."""
        live = {c.get("remoteJid", "") for c in chats}
        self.selected_chats &= live

    def _on_mass_mark_read_chats(self, event):
        if not self.selected_chats: return
        # One paced batch, not one /send-seen per chat at once — see
        # MainWindow.mark_conversations_as_read().
        self.main_window.mark_conversations_as_read(list(self.selected_chats), force=True)
        self.selected_chats.clear()
        self._repaint_chat_selection()

    def _on_mass_mark_unread_chats(self, event):
        if not self.selected_chats: return
        for jid in list(self.selected_chats):
            self.main_window.mark_conversation_as_unread(jid)
        self.selected_chats.clear()
        self._repaint_chat_selection()

    def _on_accel_bulk_read_chats(self, event):
        """Ctrl+Alt+Shift+R: mark every selected conversation as read."""
        self._run_bulk_chat_action(self._on_mass_mark_read_chats, event)

    def _on_accel_bulk_unread_chats(self, event):
        """Ctrl+Alt+Shift+U: mark every selected conversation as unread."""
        self._run_bulk_chat_action(self._on_mass_mark_unread_chats, event)

    def _on_accel_toggle_read_selection(self, event, single_handler) -> None:
        """Ctrl+Shift+M: with a selection (and bulk shortcuts on), mark the
        whole selection read — or unread when its first chat has nothing
        unread — otherwise run *single_handler* for the focused chat."""
        if self._bulk_shortcuts_enabled() and self.selected_chats:
            first_jid = next(iter(self.selected_chats))
            first_chat = next(
                (c for c in self.chats_list if c.get("remoteJid", "") == first_jid), None
            )
            if first_chat and int(first_chat.get("unreadCount") or 0) > 0:
                self._on_mass_mark_read_chats(event)
            else:
                self._on_mass_mark_unread_chats(event)
            return
        single_handler(event)

    def _reset_after_chat_cleared(self, jid: str) -> None:
        """Drop the open conversation's view if *jid* is the one just cleared.
        The conversations panel owns that view; the other lists reach it
        through the main window."""
        self._reset_view_after_chat_cleared(jid)

    def _on_mass_clear_chats(self, event):
        i18n = self.main_window.i18n
        if not self.selected_chats: return
        count = len(self.selected_chats)
        confirmed, keep_starred = confirm_clear_chat(
            self,
            i18n.t("clear_confirm_msg_bulk").format(count=count),
            i18n.t("clear_chat_bulk_title"),
            i18n.t("clear_chat_keep_starred"),
            yes_label=i18n.t("yes_button"),
            no_label=i18n.t("no_button"),
        )
        if not confirmed:
            return
        for jid in list(self.selected_chats):
            self.main_window.clear_chat(jid, keep_starred=keep_starred)
            self._reset_after_chat_cleared(jid)
        self.selected_chats.clear()
        self._repaint_chat_selection()
        self.main_window.output(i18n.t("success_clear"), interrupt=True)

    def _on_mass_delete_chats(self, event):
        i18n = self.main_window.i18n
        if not self.selected_chats: return
        count = len(self.selected_chats)
        if wx.MessageBox(
            i18n.t("delete_confirm_msg_bulk").format(count=count),
            i18n.t("delete_chat_bulk_title"),
            wx.YES_NO | wx.ICON_QUESTION, self,
        ) != wx.YES:
            return
        for jid in list(self.selected_chats):
            self.main_window.delete_chat(jid)
        self.selected_chats.clear()
        self._repaint_chat_selection()
        self.main_window.output(i18n.t("success_delete"), interrupt=True)

    def _on_accel_bulk_clear_chats(self, event):
        """Ctrl+Alt+Shift+L: clear every selected conversation."""
        self._run_bulk_chat_action(self._on_mass_clear_chats, event)

    def _on_accel_bulk_delete_chats(self, event):
        """Ctrl+Shift+Delete: delete every selected conversation."""
        self._run_bulk_chat_action(self._on_mass_delete_chats, event)
