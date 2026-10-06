"""ConversationPanelVisibilityMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

One ConversationsPanel serves three chat lists (main, archived, locked), so
at most one conversation is open at a time. A conversation belongs to the
panel it was opened from and is visible only there.

Switching panels never shows it: the conversation stays open, message list
untouched, but hidden, and the user lands on the chat list. Showing it again
on every Alt+1 <-> Alt+4 was a perceptible delay, so a switch is Show/Hide of
the panels plus focus on the chat list, nothing else. The conversation comes
back only on an explicit ask (Alt+M, Alt+2, Alt+3: reveal_open_conversation())
or by opening a chat, and then in the panel it belongs to.

Pressing Alt+1 or Alt+4 inside the panel the open conversation already is
visible in switches nothing, so it hides nothing: the focus goes to that
panel's chat list and the conversation stays on screen. Only a real change of
panel (main <-> archived <-> locked, or coming back from Status or Calls) hides
it.

Every way of making one of the three chat panels visible goes through
show_chat_panel(): it hides the detail pane and puts focus on that panel's
chat list, never in a message list. Nothing here rebuilds the conversation or
asks the network for anything.
"""

import logging
from core.conversation_view import (
    ARCHIVED,
    LOCKED,
    MAIN,
    panel_layout,
    resolve_origin,
)


def _is_shown(widget):
    """IsShown() that cannot raise (a destroyed widget answers None)."""
    try:
        return bool(widget.IsShown())
    except Exception:
        return None


class ConversationPanelVisibilityMixin:

    def _begin_conversation_visit(self, conversation, origin=None) -> str:
        """Record which panel `conversation` is being opened for."""
        origin = resolve_origin(
            origin, getattr(self, "_conversation_origin", None),
            self.conversations_list.IsShown(), self.conversation_panel.IsShown())
        self._conversation_origin = origin
        return origin

    def _list_panel_for(self, shown: str):
        """The archived or locked list panel that `shown` stands for (None for
        MAIN: its list is ConversationsPanel's own)."""
        name = {ARCHIVED: "archived_conversations_panel",
                LOCKED: "locked_conversations_panel"}.get(shown)
        return getattr(self.main_window, name, None) if name else None

    def _detail_on_screen(self) -> bool:
        """The open conversation's pane is visible in this panel right now.
        Read before anything is hidden: coming from Status or Calls the panel
        itself has been hidden by then, which is a real switch."""
        return (self.conversation is not None and bool(_is_shown(self))
                and bool(_is_shown(getattr(self, "conversation_panel", None))))

    def _apply_panel_layout(self, shown: str, list_panel, reveal: bool,
                            keep: bool = False) -> dict:
        """Show and hide this panel's parts for `shown` being the chat list on
        screen (see panel_layout()). Only Show/Hide/Layout."""
        layout = panel_layout(
            getattr(self, "_conversation_origin", None), shown,
            self.conversation is not None, reveal, keep)
        if self.conversation is not None:
            self.conversation_panel.Show(layout["detail"])
        self.conversations_label.Show(layout["own_list"])
        self.conversations_list.Show(layout["own_list"])
        self.Show(layout["panel"])
        if list_panel is not None:
            list_panel.Show(layout["list_panel"])
        self.Layout()
        self.main_window.content_panel.Layout()
        return layout

    def _log_panel_switch(self, shown: str, layout: dict) -> None:
        """One line per switch saying what the layout asked for and what is
        actually shown afterwards, so a report of the wrong thing on screen
        can be read off the log. Panel names and flags only: no JID, name or
        message text."""
        logging.info(
            "[panel-switch] shown=%s origin=%s layout=%s actual: "
            "detail=%s panel=%s messages_list=%s message_field=%s",
            shown, getattr(self, "_conversation_origin", None), layout,
            _is_shown(getattr(self, "conversation_panel", None)),
            _is_shown(self),
            _is_shown(getattr(self, "messages_list", None)),
            _is_shown(getattr(self, "message_field", None)))

    def show_chat_panel(self, shown: str, *, focus: bool = True,
                        reveal: bool = False, keep: "bool | None" = None) -> None:
        """The one way a chat panel (main, archived, locked) becomes the
        visible one: every other panel is hidden, the open conversation's pane
        is hidden too, and keyboard focus goes to that panel's chat list,
        never into a message list.

        `reveal=True` is only for an explicit ask for the open conversation
        (reveal_open_conversation()): `shown` is then the panel it belongs
        to and its pane is shown. `focus=False` for callers that place focus
        themselves.

        `keep` says the conversation pane is on screen in `shown` right now, so
        nothing is being switched and it must not disappear. None means read
        it from the screen; a caller that hides panels before it calls (the
        navigation list) reads it first and passes it in."""
        mw = self.main_window
        if keep is None:
            keep = not reveal and self._detail_on_screen()
        list_panel = self._list_panel_for(shown)
        for name in ("archived_conversations_panel", "locked_conversations_panel",
                     "status_panel", "calls_panel"):
            other = getattr(mw, name, None)
            if other is not None and other is not list_panel:
                other.Hide()
        layout = self._apply_panel_layout(shown, list_panel, reveal, keep)
        self._log_panel_switch(shown, layout)
        if focus:
            if list_panel is None:
                self._restore_conversation_selection()
            else:
                list_panel.restore_selection()

    def reveal_open_conversation(self) -> None:
        """Make the open conversation visible, in the panel it belongs to,
        whichever panel is on screen now (Alt+M, Alt+2, Alt+3). No focus is
        moved here: the caller puts it on the messages. Costs Show/Hide only,
        the conversation was never closed."""
        if self.conversation is None:
            return
        if self.IsShown() and self.conversation_panel.IsShown():
            return
        origin = getattr(self, "_conversation_origin", None) or MAIN
        self._conversation_origin = origin
        self.show_chat_panel(origin, focus=False, reveal=True)

    # The chat list's own explicit asks for the open conversation (Alt+2,
    # Alt+3, Alt+M): reveal it, then do what the
    # shortcut does inside the conversation. With none open the handler says so.
    def _on_list_jump_last(self, event):
        self.reveal_open_conversation()
        self._on_accel_jump_last(event)

    def _on_list_jump_unread(self, event):
        self.reveal_open_conversation()
        self._on_accel_jump_unread(event)

    def _on_list_focus_messages(self, event):
        self.reveal_open_conversation()
        self._on_accel_focus_list(event)
