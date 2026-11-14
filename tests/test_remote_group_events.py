from types import SimpleNamespace as N

from core.group_event_data import group_update, merge_participants
from core import remote_group_events as events
from main_window.groups import GroupsMixin


def test_partial_settings_description_deletion_and_unknown_values():
    assert group_update({"id": "7@g.us", "desc": "", "announce": False}) == ("7@g.us", {"desc": "", "announce": False}, None)
    for data in [{"id": "7@g.us", "announce": "false"}, {"id": "1@lid", "desc": "x"},
                 {"id": "7@g.us", "participants": [], "participantsComplete": True}]:
        assert group_update(data) is None


def test_member_deltas_use_phone_lid_aliases_and_preserve_other_participants():
    mw = N(_lid_to_phone={"901@lid": "1@s.whatsapp.net"}, _phone_to_lid={})
    current = [{"id": "1@c.us", "isAdmin": True}, {"id": "2@s.whatsapp.net", "isAdmin": True}]
    result = merge_participants(mw, current, "demote", ["901@lid"])
    assert result[0]["isAdmin"] is False and result[1]["isAdmin"] is True and current[0]["isAdmin"] is True
    result = merge_participants(mw, result, "remove", ["901@lid"])
    assert len(result) == 1 and result[0]["id"] == "2@s.whatsapp.net"


def window():
    calls = []
    chat = {"remoteJid": "7@g.us", "groupMetadata": {"desc": "old", "announce": False,
        "restrict": True, "participants": [{"id": "1@s.whatsapp.net", "isAdmin": False}]}}
    mw = N(chats={"7@g.us": chat}, my_jid="1@s.whatsapp.net", my_lid="901@lid", _group_send_perms={},
        _GROUP_SEND_PERMS_MAX_AGE_SECONDS=86400, _phone_digits_equivalent=lambda a,b:a==b,
        _live_events_ready=lambda: True, _group_info_recent={"7@g.us": (0, {"desc": "old"})},
        _persist_group_send_perms=lambda: calls.append("permissions"), _schedule_save=lambda **kw: calls.append("save"))
    mw._record_group_send_perms = lambda jid, chat: GroupsMixin._record_group_send_perms(mw, jid, chat)
    mw._is_group_send_restricted = lambda: GroupsMixin._is_group_send_restricted(mw, chat)
    mw.conversations_panel = N(conversation=chat,
        refresh_composer_permissions=lambda jid: calls.append("composer"),
        _set_group_participants_cache=lambda values,jid: calls.append("mentions"),
        _show_group_size=lambda jid,n: calls.append(("size", n)),
        _set_group_call_limit=lambda jid,n: calls.append(("limit", n)))
    return mw, chat, calls


def test_live_permissions_and_roles_refresh_composer_and_invalidate_group_info():
    mw, chat, calls = window()
    events.apply_group_update(mw, "7@g.us", {"announce": True, "desc": ""}, None)
    assert mw._is_group_send_restricted() and chat["groupMetadata"]["restrict"] is True
    assert chat["groupMetadata"]["desc"] == "" and "7@g.us" not in mw._group_info_recent
    events.apply_group_update(mw, "7@g.us", {}, ("promote", ["1@s.whatsapp.net"]))
    assert not mw._is_group_send_restricted() and "mentions" in calls and ("size", 1) in calls
    events.apply_group_update(mw, "7@g.us", {}, ("demote", ["1@s.whatsapp.net"]))
    assert mw._is_group_send_restricted()


def test_unknown_roster_does_not_invent_a_group_size_and_hidden_view_is_not_repainted():
    mw, chat, calls = window()
    del chat["groupMetadata"]["participants"]
    mw.is_chat_hidden_by_vault = lambda jid: True
    events.apply_group_update(mw, "7@g.us", {}, ("add", ["2@s.whatsapp.net"]))
    assert "participants" not in chat["groupMetadata"] and "composer" not in calls
    assert not any(isinstance(c, tuple) and c[0] == "size" for c in calls)


def test_sync_gate_and_account_scope(monkeypatch):
    mw, _, calls = window()
    mw._live_events_ready = lambda: False
    events.apply_group_update(mw, "7@g.us", {"restrict": False}, None)
    assert calls == []
    queued = []
    monkeypatch.setattr(events.wx, "CallAfter", lambda fn,*a: queued.append(a))
    client = N(main_window=mw, _belongs_to_this_session=lambda d:d.get("session")=="ours")
    for session in ("other", "ours"):
        events.on_groups_event(client, {"session": session, "data": [{"id":"7@g.us","restrict":False}]})
    assert len(queued) == 1
