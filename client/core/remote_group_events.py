"""Main-thread group updates: patch stated fields, preserve everything else."""
import wx

from core.group_event_data import group_update, merge_participants


def apply_group_update(mw, jid, fields, membership):
    if getattr(mw, "_shutting_down", False) or not mw._live_events_ready():
        return
    chat = mw.chats.get(jid)
    if not isinstance(chat, dict):
        return
    meta = chat.get("groupMetadata")
    meta = dict(meta) if isinstance(meta, dict) else {}
    meta.update(fields)
    roster_known = isinstance(meta.get("participants"), list) and bool(meta["participants"])
    if membership:
        action, members = membership
        roster_known = roster_known or action == "snapshot"
        if roster_known:
            meta["participants"] = merge_participants(mw, meta.get("participants") or [], action, members)
    chat["groupMetadata"] = meta
    # A cached group-info answer must not keep an old description or roster.
    from main_window.groups import _GROUP_INFO_GUARD
    with _GROUP_INFO_GUARD:
        getattr(mw, "_group_info_recent", {}).pop(jid, None)
    # The subject never reaches here: on_groups_event() routes it to
    # on_group_subject_updated(), the existing rename path.
    if "announce" in fields or membership:
        if mw._record_group_send_perms(jid, chat) is not None:
            mw._persist_group_send_perms()
    panel = getattr(mw, "conversations_panel", None)
    shown = getattr(panel, "conversation", None)
    hidden = getattr(mw, "is_chat_hidden_by_vault", lambda j: False)(jid)
    if isinstance(shown, dict) and shown.get("remoteJid") == jid:
        shown["groupMetadata"] = meta
        if not hidden:
            if "announce" in fields or membership:
                panel.refresh_composer_permissions(jid)
            if membership:
                # A removed member must not remain in @ suggestions. The next
                # mention lookup uses the established fresh group-info path.
                panel._set_group_participants_cache([], jid)
                if roster_known:
                    panel._show_group_size(jid, len(meta["participants"]))
                    panel._set_group_call_limit(jid, len(meta["participants"]))
    mw._schedule_save(dirty_jid=jid)


def on_groups_event(client, info):
    if not isinstance(info, dict) or not client._belongs_to_this_session(info):
        return
    data = info.get("data", [])
    for item in data if isinstance(data, list) else [data]:
        target = group_update(item)
        if target is not None:
            jid, fields, membership = target
            if "subject" in fields:
                wx.CallAfter(client.main_window.on_group_subject_updated, jid, fields.pop("subject"))
            if fields or membership:
                wx.CallAfter(apply_group_update, client.main_window, jid, fields, membership)
