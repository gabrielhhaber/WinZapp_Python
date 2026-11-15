from types import SimpleNamespace as N

from core import remote_chat_delete as deletion
from main_window.chat_actions import ChatActionsMixin


def window(newest=100):
    calls = []
    phone, lid = "1@s.whatsapp.net", "901@lid"
    chat = {"remoteJid": phone, "t": newest}
    mw = N(chats={phone: chat, lid: dict(chat)}, _lid_to_phone={lid: phone}, _phone_to_lid={phone: lid},
        _deleted_chats=set(), _forget_chat_lock=lambda jid: calls.append(("forget", jid)),
        _schedule_save=lambda **kw: None, _schedule_set_chats=lambda: None,
        db=N(delete_chat=lambda jid: calls.append(("delete", jid)), set_metadata_json=lambda *a: calls.append(("metadata", a))))
    mw.delete_chat_local = lambda jid: ChatActionsMixin.delete_chat_local(mw, jid)
    mw.i18n = N(t=lambda key: key)
    mw.output = lambda text, interrupt=False: calls.append(("say", text))
    panel = N(conversation=chat)
    def close():
        calls.append(("close",))
        panel.conversation = None
    panel.close_conversation_for_panel_switch = close
    mw.conversations_panel = panel
    return mw, calls


def test_delete_removes_all_known_aliases_and_records_durable_markers_without_server_actions():
    mw, calls = window()
    deletion.apply_chat_deleted(mw, "901@lid", 100)
    assert not mw.chats
    assert mw._deleted_chats == {"1@s.whatsapp.net", "901@lid"}
    assert ("close",) in calls and ("delete", "1@s.whatsapp.net") in calls and ("delete", "901@lid") in calls
    assert ("say", "remote_chat_deleted_closed") in calls


def test_a_chat_that_stays_open_is_not_deleted_under_the_reader():
    mw, calls = window()
    mw.conversations_panel.close_conversation_for_panel_switch = lambda: calls.append(("close",))
    deletion.apply_chat_deleted(mw, "901@lid", 100)
    assert len(mw.chats) == 2 and not mw._deleted_chats
    assert not any(call[0] in ("delete", "say") for call in calls)


def test_newer_message_preserves_chat_and_view_even_with_only_loaded_record_timestamp():
    mw, calls = window(newest=0)
    mw.chats["901@lid"]["messages"] = {"messages": {"records": [{"messageTimestamp": 101}]}}
    deletion.apply_chat_deleted(mw, "901@lid", 100)
    assert mw.chats and not mw._deleted_chats and calls == []


def test_shutdown_and_wrong_account_or_invalid_cutoff_are_ignored(monkeypatch):
    calls = []
    owner = N(db=object(), _msg_bg_executor=N(submit=lambda fn: calls.append("checked")))
    client = N(main_window=owner, _belongs_to_this_session=lambda d: d.get("session") == "ours")
    for session, cutoff in [("other", 100), ("ours", True), ("ours", 0), ("ours", 100)]:
        deletion.on_chat_deleted(client, {"session": session, "chatId": "7@g.us", "until": cutoff})
    assert calls == ["checked"]
    # Without a database there is no stored history to check: nothing is deleted.
    owner.db = None
    deletion.on_chat_deleted(client, {"session": "ours", "chatId": "7@g.us", "until": 100})
    assert calls == ["checked"]
    # A closed executor during shutdown is not an error on the socket thread.
    owner.db = object()
    owner._msg_bg_executor = N(submit=lambda fn: (_ for _ in ()).throw(RuntimeError("closed")))
    deletion.on_chat_deleted(client, {"session": "ours", "chatId": "7@g.us", "until": 100})
    mw, calls = window()
    mw._shutting_down = True
    deletion.apply_chat_deleted(mw, "901@lid", 100)
    assert calls == []


def test_newer_unloaded_disk_row_prevents_deletion(monkeypatch):
    mw, calls = window()
    mw.db.get_messages = lambda jid, limit: [{"messageTimestamp": 101}]
    mw._msg_bg_executor = N(submit=lambda fn: fn())
    monkeypatch.setattr(deletion.wx, "CallAfter", lambda fn, *args: fn(*args))
    client = N(main_window=mw, _belongs_to_this_session=lambda data: True)
    deletion.on_chat_deleted(client, {"chatId": "901@lid", "until": 100})
    assert mw.chats and calls == []
