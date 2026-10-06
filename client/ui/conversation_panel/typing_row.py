"""TypingRowMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

The temporary last row of the messages list that says who in the open
conversation is typing or recording audio — "Maria está digitando...", in a
group one phrase per participant. It reuses the presence state MainWindow
already keeps for the chat-list label (``_composing_chats``, filled and expired
by on_presence_update() and its 10-second timers) and the strings the spoken
announcement already uses (``typing_text`` / ``recording_text``).

The row is not a message, and it is deliberately kept OUT of
``_sorted_messages``: it exists only in the control, always as its very last
row, at index ``len(self._sorted_messages)``. Every handler that maps a list
index to a message already bounds-checks it against ``len(self._sorted_messages)``
(the house pattern — context menu, Enter, copy, reply, delete, bookmarks,
selection, the media buttons, mark-as-read), so on this row they find no
message and do nothing, without a new sentinel type each of them would have
to learn. Putting it into ``_sorted_messages`` instead would have made it the
"last message" of every tail-based rule (Alt+2, the signature cache,
_append_new_tail_rows(), the unread separator's anchor, the ", N de M" count).

What that costs is the other direction: code that reads the CONTROL's row
count, or appends a message row to the control, must not count or append past
this row. That code goes through message_row_count() and append_message_row().
Inserting at an index (_sync_message_rows(), the unread separator) needs
nothing: every such index is at most ``len(self._sorted_messages)``, which is
above this row.

It is turned on in Settings > User Interface
(``user_interface.show_typing_row``, off by default; typing_row_enabled()).
Off, nothing here adds a row and a row already showing is removed.

It never speaks. Typing/recording is already announced by
MainWindow.on_presence_update() (announce_typing / announce_recording), and a
row appearing must not become a second announcement — so it is only appended
or rewritten, never focused.

These are plain functions taking the panel, not mixin methods, because they are
called from inside methods (populate_messages(), on_incoming_message(),
_sync_message_rows(), ...) that the tests bind onto plain stubs; every
attribute is read with a default so such a stub needs nothing new.
"""

import logging

from core.conversation_view import conversation_in_view


def typing_row_text(entries, t) -> str:
    """The row's text for *entries* — ``[(name, action), ...]``, action being
    "composing" or "recording" as in MainWindow._composing_chats — or ''.

    One phrase per person, joined: each ``typing_text``/``recording_text``
    already ends a sentence ("...") in every locale, and mixing them is the
    only honest wording when one participant types while another records.
    A person without a name is skipped rather than shown as a JID.
    """
    parts = []
    for name, action in entries:
        if not name:
            continue
        if action == "composing":
            key = "typing_text"
        elif action == "recording":
            key = "recording_text"
        else:
            continue
        phrase = t(key).format(name=name)
        # Two unnamed @lid participants read as the same phrase: say it once.
        if phrase not in parts:
            parts.append(phrase)
    return " ".join(parts)


def typing_row_enabled(settings) -> bool:
    """Settings > User Interface, ``user_interface.show_typing_row``: off unless
    explicitly turned on. A missing or malformed settings dict counts as off,
    like the shipped default."""
    ui = settings.get("user_interface") if isinstance(settings, dict) else None
    if not isinstance(ui, dict):
        return False
    return ui.get("show_typing_row", False) is True


def typing_row_shown(panel) -> bool:
    """Whether the current messages control ends with the typing row.

    Tied to the control object, not a flag: the list mode can be switched at
    runtime (apply_message_list_mode()), and a row added to the other control
    is not in this one.
    """
    lst = getattr(panel, "messages_list", None)
    return lst is not None and getattr(panel, "_typing_row_list", None) is lst


def message_row_count(panel) -> int:
    """How many rows of the messages control are rows of ``_sorted_messages``
    — the item count without the typing row. Use it wherever the control's
    count is compared with ``len(self._sorted_messages)`` or its last row is
    taken to be the last message."""
    count = panel.messages_list.GetItemCount()
    if typing_row_shown(panel):
        count -= 1
    return max(0, count)


def append_message_row(panel, text: str) -> None:
    """Add a message row at the end of the messages — above the typing row
    when it is showing, so the row stays last. InsertItem() at an index keeps
    the focused row where it is in all three controls (classic ListCtrl,
    CompatListBoxMessagesCtrl, the Mac MacListCtrl), so a user sitting on the
    typing row stays on it."""
    if typing_row_shown(panel):
        panel.messages_list.InsertItem(message_row_count(panel), text)
    else:
        panel.messages_list.Append((text,))


def _open_chat_keys(panel) -> list:
    """The keys the open conversation can have in MainWindow._composing_chats:
    normalized and bridged from @lid exactly as on_presence_update() builds
    them, plus the @lid form for a phone JID whose presence came in under it
    before the mapping was known."""
    mw = panel.main_window
    jid = mw._normalize_jid((panel.conversation or {}).get("remoteJid", ""))
    if not jid:
        return []
    if jid.endswith("@lid"):
        jid = getattr(mw, "_lid_to_phone", {}).get(jid, jid)
    keys = [jid]
    lid = getattr(mw, "_phone_to_lid", {}).get(jid, "")
    if lid and lid != jid:
        keys.append(lid)
    return keys


def _dismissed_for(panel, chat: str) -> set:
    """Participants whose message already arrived in this conversation since
    their last typing event. Reset whenever the open conversation changes."""
    if getattr(panel, "_typing_row_chat", None) != chat:
        panel._typing_row_chat = chat
        panel._typing_row_dismissed = set()
    return panel._typing_row_dismissed


def _remove_typing_row(panel) -> None:
    lst = panel.messages_list
    idx = lst.GetItemCount() - 1
    was_focused = idx >= 0 and lst.GetFocusedItem() == idx
    if idx >= 0:
        lst.DeleteItem(idx)
    panel._typing_row_list = None
    panel._typing_row_text = ""
    # The row under the cursor is gone. The list box controls already move
    # their selection to the row above on their own; the classic ListCtrl can
    # be left with no focused row at all, which a screen reader reads as the
    # list going silent. Put the cursor on the last message — the row that
    # was right above it. Not while no conversation is open: the panel is
    # hidden then, and a Focus() would only run the focused-row side effects
    # (history paging, mark-as-read) for a conversation that is gone.
    #
    # This runs from the incoming-message path too, which must not mark the
    # conversation read for a user who is not looking (window in the tray or
    # inactive, conversation hidden behind another panel): the cursor still
    # moves, but the focused-row side effects are suppressed then.
    if was_focused and getattr(panel, "conversation", None) is not None:
        last = lst.GetItemCount() - 1
        if last >= 0 and lst.GetFocusedItem() != last:
            mw = getattr(panel, "main_window", None)
            present = bool(getattr(mw, "_allow_ui_focus_changes", lambda: False)()) \
                and conversation_in_view(panel)
            previous = getattr(panel, "_suppress_selection_side_effects", False)
            panel._suppress_selection_side_effects = previous or not present
            try:
                lst.Focus(last)
                lst.Select(last)
            finally:
                panel._suppress_selection_side_effects = previous


def sync_typing_row(panel, chat_jid_norm: str = "", fresh=()) -> None:
    """Show, rewrite or remove the typing row so it matches who is typing or
    recording in the open conversation right now.

    *chat_jid_norm*/*fresh*: the chat of a presence event and the participants
    it reported typing or recording. A fresh event for someone whose message
    already arrived (see dismiss_typing_row_for_message()) shows them again.

    Writes only when the text changes: presence repeats "composing" every few
    seconds while someone types, and rewriting an unchanged row would make the
    screen reader re-read it when it is the focused one.
    """
    lst = getattr(panel, "messages_list", None)
    if lst is None:
        return
    mw = getattr(panel, "main_window", None)
    composing = getattr(mw, "_composing_chats", None)
    if not isinstance(composing, dict):
        composing = {}
    shown = typing_row_shown(panel)
    # Read on every update, so turning it off in Settings applies at once
    # (the dialog calls refresh_typing_row() after Apply) and a row already
    # on screen goes away.
    if not typing_row_enabled(getattr(mw, "settings", None)):
        if shown:
            lst.Freeze()
            try:
                _remove_typing_row(panel)
            finally:
                lst.Thaw()
        return
    # Cheap way out for the overwhelmingly common case — nobody typing
    # anywhere and no row on screen — since this runs on every presence event.
    if not shown and not any(composing.values()):
        return

    text = ""
    if getattr(panel, "conversation", None) is not None:
        keys = _open_chat_keys(panel)
        if keys:
            chat = keys[0]
            dismissed = _dismissed_for(panel, chat)
            if fresh and chat_jid_norm in keys:
                dismissed.difference_update(fresh)
            entries = []
            seen = set()
            for key in keys:
                for participant, action in (composing.get(key) or {}).items():
                    if participant in dismissed or participant in seen:
                        continue
                    seen.add(participant)
                    # resolve_missing=False: this runs on the UI thread on
                    # every presence event; the spoken announcement for the
                    # same event already kicks off the background lookup.
                    name = mw._resolve_jid_name(participant, chat, resolve_missing=False)
                    entries.append((name, action))
            text = typing_row_text(entries, mw.i18n.t)

    if text == (getattr(panel, "_typing_row_text", "") if shown else ""):
        return
    lst.Freeze()
    try:
        if not text:
            _remove_typing_row(panel)
        elif shown:
            lst.SetItemText(lst.GetItemCount() - 1, text)
            panel._typing_row_text = text
        else:
            lst.Append((text,))
            panel._typing_row_list = lst
            panel._typing_row_text = text
    finally:
        lst.Thaw()


def dismiss_typing_row_for_message(panel, msg: dict) -> None:
    """Take the sender of *msg* — a message that just arrived in the open
    conversation — off the typing row: what they were typing is here now.

    Local to the row on purpose. MainWindow._composing_chats, which also
    drives the chat-list label and Alt+T, is left to the presence events and
    their timers exactly as before.
    """
    if not isinstance(msg, dict) or (msg.get("key") or {}).get("fromMe"):
        return
    mw = getattr(panel, "main_window", None)
    composing = getattr(mw, "_composing_chats", None)
    if not typing_row_shown(panel) or not isinstance(composing, dict):
        return
    keys = _open_chat_keys(panel)
    if not keys:
        return
    dismissed = _dismissed_for(panel, keys[0])
    if keys[0].endswith("@g.us"):
        key = msg.get("key") or {}
        sender = key.get("participant") or msg.get("participant") or ""
        sender = mw._normalize_jid(sender) if sender else ""
        if sender.endswith("@lid"):
            sender = getattr(mw, "_lid_to_phone", {}).get(sender, sender)
        if sender:
            dismissed.add(sender)
    else:
        # One other person in a direct chat: whoever was typing, it was them.
        for chat_key in keys:
            dismissed.update((composing.get(chat_key) or {}).keys())
    sync_typing_row(panel)


class TypingRowMixin:
    """Entry point MainWindow uses for the typing row."""

    def refresh_typing_row(self, chat_jid_norm: str = "", fresh=()) -> None:
        """Called by MainWindow after every presence change (and when a typing
        indicator times out). See sync_typing_row()."""
        try:
            sync_typing_row(self, chat_jid_norm, fresh)
        except Exception:
            # A presence event must never take the conversation down with it.
            logging.exception("[refresh_typing_row] failed")
