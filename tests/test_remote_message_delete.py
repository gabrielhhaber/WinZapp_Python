"""Delete-for-me events remove only their scoped message and use the existing UI path."""
from types import SimpleNamespace as N

from core import remote_message_delete as deletion

GROUP = "7@g.us"


def window(shown=GROUP, **extra):
    calls = []
    def record(mid):
        return {"key": {"id": mid}, "message": {"conversation": mid}}
    chat = {"remoteJid": GROUP, "messages": {"messages": {"records": [record("M"), record("KEEP")]}},
            "lastMessage": record("M"), "unreadCount": 4}
    panel = N(conversation={"remoteJid": shown},
        messages_list=N(Freeze=lambda: calls.append("freeze"), Thaw=lambda: calls.append("thaw")),
        remove_messages_by_id=lambda mids, **kw: calls.append(("remove", mids, kw)))
    mw = N(chats={GROUP: chat, "other@g.us": {"messages": {"messages": {"records": [record("M")]}}}},
        conversations_panel=panel, db=N(delete_message=lambda *a: calls.append(("delete", *a))),
        _recompute_chat_last_message=lambda jid: calls.append(("preview", jid)),
        _schedule_save=lambda **kw: calls.append(("save", kw)),
        _schedule_set_chats=lambda: calls.append("chats"), **extra)
    return mw, chat, calls


def test_explicit_delete_removes_one_message_without_touching_same_id_in_another_chat():
    mw, chat, calls = window()
    deletion.apply_message_deleted(mw, GROUP, "M")
    assert [r["key"]["id"] for r in chat["messages"]["messages"]["records"]] == ["KEEP"]
    assert mw.chats["other@g.us"]["messages"]["messages"]["records"][0]["key"]["id"] == "M"
    assert ("delete", GROUP, "M") in calls and chat["unreadCount"] == 4
    assert calls[:3] == ["freeze", ("remove", {"M"}, {"focus_previous": True}), "thaw"]


def test_a_closed_chat_is_updated_without_touching_the_open_conversation():
    mw, _, calls = window(shown="other@g.us")
    deletion.apply_message_deleted(mw, GROUP, "M")
    assert "freeze" not in calls and not any(isinstance(c, tuple) and c[0] == "remove" for c in calls)
    assert ("delete", GROUP, "M") in calls


def test_lid_alias_updates_the_existing_phone_chat():
    mw, chat, calls = window(shown="1@s.whatsapp.net", _lid_to_phone={"901@lid": "1@s.whatsapp.net"})
    mw.chats["1@s.whatsapp.net"] = mw.chats.pop(GROUP)
    deletion.apply_message_deleted(mw, "901@lid", "M")
    assert [r["key"]["id"] for r in chat["messages"]["messages"]["records"]] == ["KEEP"]
    assert ("delete", "1@s.whatsapp.net", "M") in calls


def test_unloaded_row_is_removed_from_disk_without_creating_a_chat():
    mw, _, calls = window()
    deletion.apply_message_deleted(mw, "9@g.us", "UNKNOWN")
    assert ("delete", "9@g.us", "UNKNOWN") in calls and "9@g.us" not in mw.chats


def test_shutdown_ignores_a_queued_delete_without_touching_the_panel_or_disk():
    mw, chat, calls = window(_shutting_down=True)
    deletion.apply_message_deleted(mw, GROUP, "M")
    assert calls == []
    assert [r["key"]["id"] for r in chat["messages"]["messages"]["records"]] == ["M", "KEEP"]


def test_socket_guard_rejects_other_accounts_and_invalid_message_targets(monkeypatch):
    calls = []
    monkeypatch.setattr(deletion.wx, "CallAfter", lambda fn, *args: calls.append(args))
    client = N(main_window="mw", _belongs_to_this_session=lambda d: d.get("session") == "ours")
    for account, chat in (("other", GROUP), ("ours", "status@broadcast"), ("ours", GROUP)):
        deletion.on_message_deleted(client, {"session": account, "chatId": chat, "messageId": "M"})
    assert calls == [("mw", GROUP, "M")]
