"""Marking people in the "Add participants" dialog: the selection mode.

The same scheme as the lists of chats and messages (ChatListSelectionMixin):

* **Ctrl+Space** marks or unmarks the person the list is on, with the sound
  and the "Selecionado"/"Desmarcado" announcement the other lists give.
* **Space** does the same while someone is marked, if Settings > User
  Interface > "Usar Espaço para marcar/desmarcar enquanto houver algo marcado"
  is on; with nothing marked it stays the list's own key.
* **Shift+Up/Down** mark the neighbouring row; **Shift+Home/End** mark every
  row up to the first/last one; **Ctrl+Shift+Space** marks everyone, or
  unmarks them all when they all are.
* **Esc** unmarks everything, if "Esc desmarca…" is on — and only then: with
  nothing marked, or the setting off, Esc is the dialog's Cancel.

This replaces a wx.CheckListBox, whose check boxes gave a screen reader no
sound and no "selected" announcement when Space ticked one. The marks are a
plain set of JIDs; the rows say "selected" in their text (position per
Settings), and the selection mode is derived from the set, never stored.
"""
import logging
import os

import wx

from core.sound_system import load_sound
from core.utils import append_selected_marker
from ui.conversation_panel.selection_rules import toggle_jid_selection, visible_jid_selected
from ui.shortcut_bindings import command_key_event


def row_labels(rows, marks, unknown, word, position):
    """The text of each row of the list: the name (never a JID), with the
    "selected" word on the marked ones."""
    return [append_selected_marker(name or unknown, word, position, jid in marks)
            for jid, name in rows]


class CallInviteMarksMixin:
    """The marks of the people list, for `CallInviteDialog`."""

    def _init_invite_marks(self) -> None:
        self._marks = set()
        self._labels = []
        self._mark_sound = None
        self._list.Bind(wx.EVT_KEY_DOWN, self._on_invite_key_down)
        self._list.Bind(wx.EVT_LISTBOX, self._on_invite_row_focused)
        # Esc is the dialog's Cancel before it is anything else; only a hook on
        # the list that does not pass it on can keep it for "unmark everything".
        self._list.Bind(wx.EVT_CHAR_HOOK, self._on_invite_char_hook)

    # ── Settings (Settings > User Interface), shared with the other lists ────

    def _invite_ui_setting(self, name, default=True):
        return self._mw.settings.get("user_interface", {}).get(name, default)

    # ── Rows ─────────────────────────────────────────────────────────────────

    def _invite_labels(self):
        i18n = self._mw.i18n
        return row_labels(
            self._rows, self._marks, i18n.t("unknown_contact"), i18n.t("selected_suffix"),
            self._invite_ui_setting("selected_announcement_position", "end"))

    def _invite_jid_at(self, index):
        return self._rows[index][0] if 0 <= index < len(self._rows) else ""

    def _invite_marks_visible(self) -> bool:
        return visible_jid_selected(self._marks, (jid for jid, _name in self._rows))

    def _repaint_invite_marks(self) -> None:
        """Redraw only the rows whose text changed, so the reader hears none of
        the rows that did not, and keep the row the list is on."""
        fresh = self._invite_labels()
        focus = self._list.GetSelection()
        self._list.Freeze()
        try:
            for index, (old, new) in enumerate(zip(self._labels, fresh)):
                if old != new:
                    self._list.SetString(index, new)
            if focus != wx.NOT_FOUND:
                self._list.SetSelection(focus)
        finally:
            self._list.Thaw()
        self._labels = fresh

    # ── Announcements ────────────────────────────────────────────────────────

    def _announce_invite_marks(self, key, was_active, sound=False) -> None:
        i18n = self._mw.i18n
        text = i18n.t(key)
        if self._invite_ui_setting("space_selects_in_selection_mode"):
            is_active = self._invite_marks_visible()
            if is_active and not was_active:
                text = f"{text}. {i18n.t('selection_mode_on')}"
            elif was_active and not is_active:
                text = f"{text}. {i18n.t('selection_mode_off')}"
        if sound:
            self._play_invite_mark_sound()
        self._mw.output(text, interrupt=True)

    def _play_invite_mark_sound(self) -> None:
        """The "selected" cue the other lists play. Guarded: a sound that cannot
        load must not cost the announcement (docs/traps/audio-devices.md)."""
        try:
            if self._mark_sound is None:
                self._mark_sound = load_sound(
                    self._mw.sound_system, os.path.join("default", "selected.ogg"))
            self._mark_sound.play()
        except Exception:
            logging.warning("[call] could not play the selection sound")

    def _on_invite_row_focused(self, event) -> None:
        """Arrowing onto a marked person plays the cue, so the marks are audible."""
        if self._invite_jid_at(self._list.GetSelection()) in self._marks:
            self._play_invite_mark_sound()
        event.Skip()

    # ── Marking ──────────────────────────────────────────────────────────────

    def _mark_invite_rows(self, indexes) -> bool:
        """Mark the rows at `indexes` that are not marked yet; False if none."""
        added = [i for i in indexes if self._invite_jid_at(i) and self._invite_jid_at(i) not in self._marks]
        if not added:
            return False
        was_active = self._invite_marks_visible()
        self._marks.update(self._invite_jid_at(i) for i in added)
        self._repaint_invite_marks()
        self._announce_invite_marks("selected", was_active, sound=True)
        return True

    def _toggle_invite_mark(self, index) -> bool:
        """Mark or unmark the person at `index`. False when the row has none."""
        jid = self._invite_jid_at(index)
        if not jid:
            return False
        was_active = self._invite_marks_visible()
        now_marked, _ = toggle_jid_selection(self._marks, jid)
        self._repaint_invite_marks()
        self._announce_invite_marks(
            "selected" if now_marked else "unselected", was_active, sound=now_marked)
        return True

    def _toggle_all_invite_marks(self) -> bool:
        everyone = [jid for jid, _name in self._rows if jid]
        if not everyone:
            return False
        was_active = self._invite_marks_visible()
        everything = all(jid in self._marks for jid in everyone)
        if everything:
            self._marks.difference_update(everyone)
        else:
            self._marks.update(everyone)
        self._repaint_invite_marks()
        self._announce_invite_marks(
            "all_unselected" if everything else "all_selected", was_active, sound=not everything)
        return True

    def _clear_invite_marks(self) -> bool:
        """Unmark everyone. False when nobody was marked."""
        if not self._marks:
            return False
        was_active = self._invite_marks_visible()
        self._marks.clear()
        self._repaint_invite_marks()
        self._announce_invite_marks("all_unselected", was_active)
        return True

    # ── Keys ─────────────────────────────────────────────────────────────────

    def _handle_invite_mark_key(self, key, ctrl, shift) -> bool:
        """The selection keys of the people list. True when one was dealt with."""
        focus = self._list.GetSelection()
        count = len(self._rows)
        if shift and not ctrl and key in (wx.WXK_DOWN, wx.WXK_NUMPAD_DOWN,
                                          wx.WXK_UP, wx.WXK_NUMPAD_UP):
            down = key in (wx.WXK_DOWN, wx.WXK_NUMPAD_DOWN)
            target = (focus + 1 if down else focus - 1) if focus >= 0 else 0
            if 0 <= target < count:
                self._list.SetSelection(target)
                self._mark_invite_rows([target])
            return True
        if shift and not ctrl and key in (wx.WXK_HOME, wx.WXK_NUMPAD_HOME,
                                          wx.WXK_END, wx.WXK_NUMPAD_END):
            to_end = key in (wx.WXK_END, wx.WXK_NUMPAD_END)
            if count:
                start = focus if focus >= 0 else 0
                span = range(start, count) if to_end else range(0, start + 1)
                target = count - 1 if to_end else 0
                self._list.SetSelection(target)
                self._mark_invite_rows(list(span))
            return True
        if ctrl and shift and key == wx.WXK_SPACE:
            self._toggle_all_invite_marks()
            return True
        if ctrl and not shift and key == wx.WXK_SPACE:
            # Swallowed even on a row with nothing to mark, as in the other lists.
            self._toggle_invite_mark(focus)
            return True
        if (key == wx.WXK_SPACE and not ctrl and not shift
                and self._invite_ui_setting("space_selects_in_selection_mode")
                and self._invite_marks_visible()):
            return self._toggle_invite_mark(focus)
        return False

    def _on_invite_key_down(self, event) -> None:
        event = command_key_event(self, "chat_selection", event)
        if not self._handle_invite_mark_key(
                event.GetKeyCode(), event.ControlDown(), event.ShiftDown()):
            event.Skip()

    def _on_invite_char_hook(self, event) -> None:
        if (event.GetKeyCode() == wx.WXK_ESCAPE
                and not event.ControlDown() and not event.ShiftDown() and not event.AltDown()
                and self._invite_ui_setting("escape_clears_selection")
                and self._invite_marks_visible()
                and self._clear_invite_marks()):
            return
        event.Skip()
