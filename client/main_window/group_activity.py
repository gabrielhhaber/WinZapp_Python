"""Group typing/recording: the chat-list preview and the spoken announcement.

Several people can type or record in a group at once; ``_composing_chats``
already keeps one entry per participant, and these helpers turn that into one
sentence (core/group_activity.py) for the conversations list and for the screen
reader. Both engines feed the same ``on_presence_update`` path, so nothing here
knows which one is running.

Speech: every change of the sentence is announced while the group is the open,
visible conversation — someone starting, someone stopping while others go on —
and nothing is said once nobody is active. Each kind obeys its own setting
(``announce_typing`` / ``announce_recording``): a disabled kind is left out of
the announced sentence, so turning one off never silences the other.
"""
import logging

from core.conversation_view import conversation_in_view
from core.group_activity import group_activity_text


def multiple_enabled(window) -> bool:
    """Settings > Spoken content, "consider activity from multiple participants":
    on unless explicitly turned off. Off keeps the old one-participant preview,
    announcement and Alt+T for groups."""
    speech = (getattr(window, "settings", None) or {}).get("speech_content", {})
    return speech.get("announce_group_multiple_activity", True) is not False


def group_entries(window, chat, *, resolve_missing=True, only_announced=False):
    """``[(name, action), ...]`` of the group's active participants, in arrival
    order; people with no resolvable name are left out, never shown as a JID."""
    active = (getattr(window, "_composing_chats", None) or {}).get(chat) or {}
    speech = {}
    if only_announced:
        speech = (getattr(window, "settings", None) or {}).get("speech_content", {})
    entries = []
    for participant, action in list(active.items()):
        if action == "composing":
            if only_announced and not speech.get("announce_typing", True):
                continue
        elif action == "recording":
            if only_announced and not speech.get("announce_recording", True):
                continue
        else:
            continue
        if resolve_missing:
            name = window._resolve_jid_name(participant, chat)
        else:
            name = window._resolve_jid_name(participant, chat, resolve_missing=False)
        if name:
            entries.append((name, action))
    return entries


def is_active_chat(window, chat, open_jid) -> bool:
    """*chat* is the open conversation, whichever phone/@lid form either has."""
    if not open_jid:
        return False
    if chat == open_jid:
        return True
    to_phone = getattr(window, "_lid_to_phone", {})
    to_lid = getattr(window, "_phone_to_lid", {})
    return (to_phone.get(chat, chat) == to_phone.get(open_jid, open_jid)
            or to_lid.get(chat, chat) == to_lid.get(open_jid, open_jid))


def speech_allowed(window, chat) -> bool:
    """The open conversation, on screen, in a window the user is looking at."""
    panel = getattr(window, "conversations_panel", None)
    conv = getattr(panel, "conversation", None) if panel else None
    if conv is None:
        return False
    open_jid = window._normalize_jid(conv.get("remoteJid", ""))
    if open_jid.endswith("@lid"):
        open_jid = getattr(window, "_lid_to_phone", {}).get(open_jid, open_jid)
    return bool(
        is_active_chat(window, chat, open_jid)
        and conversation_in_view(panel)
        and not getattr(window, "_shutting_down", False)
        and not getattr(window, "_window_hidden", False)
        and window.IsShown() and not window.IsIconized() and window.IsActive()
    )


def announce_group_activity(window, chat) -> None:
    """Speak the group's activity sentence when it changed since the last one.

    The remembered sentence is updated even when speech is not allowed right
    now, so a repeated "composing" event or a window coming back to the front
    never re-announces something that did not change."""
    if not chat.endswith("@g.us") or not multiple_enabled(window):
        return
    text = group_activity_text(
        group_entries(window, chat, only_announced=True), window.i18n.t)
    spoken = getattr(window, "_group_activity_spoken", None)
    if spoken is None:
        spoken = window._group_activity_spoken = {}
    if text == spoken.get(chat, ""):
        return
    if text:
        spoken[chat] = text
    else:
        spoken.pop(chat, None)
    if not text or not speech_allowed(window, chat):
        return
    try:
        window.speak_output.output(text)
    except Exception:
        logging.error("[group_activity] announcement failed")
