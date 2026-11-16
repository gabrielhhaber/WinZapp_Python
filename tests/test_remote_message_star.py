"""Phone star events alter flags without replacing content or opening wx windows."""
from types import SimpleNamespace as N

import pytest

from core import remote_message_star as stars
from core.message_stars import remote_star_state

GROUP = "7@g.us"


def window(jid=GROUP, **extra):
    record = {"key": {"id": "M"}, "message": {"conversation": "keep"},
              **remote_star_state({"star": False}, 1)}
    writes, paints = [], []
    mw = N(chats={jid: {"messages": {"messages": {"records": [record]}}}},
        conversations_panel=N(conversation={"remoteJid": jid},
                              _repaint_or_repopulate=lambda mids: paints.append(mids)),
        db=N(update_message_star_state=lambda *a: writes.append(a)),
        _msg_bg_executor=N(submit=lambda f: f()), **extra)
    return mw, record, writes, paints


def test_star_and_unstar_update_one_row_and_atomic_disk_flags_only():
    mw, record, writes, paints = window()
    for value, at in ((True, 2), (False, 3)):
        stars.apply_star_changed(mw, GROUP, "M", remote_star_state({"star": value}, at))
        assert record["starred"] is value
    assert record["message"] == {"conversation": "keep"}
    assert len(writes) == 2 and paints == [["M"], ["M"]]


def test_local_legacy_star_survives_a_remote_unstar():
    mw, record, _, _ = window()
    record.pop("_star_remote")
    record["starred"] = True
    stars.apply_star_changed(mw, GROUP, "M", remote_star_state({"star": False}, 2))
    assert record["starred"] is True and record["_star_local"] is True


def test_late_observation_cannot_undo_a_newer_star():
    mw, record, _, paints = window()
    stars.apply_star_changed(mw, GROUP, "M", remote_star_state({"star": True}, 3))
    stars.apply_star_changed(mw, GROUP, "M", remote_star_state({"star": False}, 2))
    assert record["starred"] is True and paints == [["M"]]


def test_lid_alias_updates_the_phone_chat_but_a_group_never_becomes_its_participant():
    mw, record, writes, _ = window("1@s.whatsapp.net", _lid_to_phone={"901@lid": "1@s.whatsapp.net"})
    stars.apply_star_changed(mw, "901@lid", "M", remote_star_state({"star": True}, 2))
    assert record["starred"] is True
    assert {a[0] for a in writes} == {"901@lid", "1@s.whatsapp.net"}
    mw._phone_to_lid = {GROUP: "901@lid"}
    assert stars.chat_aliases(mw, GROUP) == {GROUP}


def test_unknown_message_updates_disk_without_creating_a_row_or_chat():
    mw, record, writes, paints = window()
    stars.apply_star_changed(mw, "9@g.us", "UNKNOWN", remote_star_state({"star": True}, 2))
    assert writes[0][:2] == ("9@g.us", "UNKNOWN") and paints == []
    assert set(mw.chats) == {GROUP} and record["starred"] is False


def test_locked_chat_is_not_repainted_but_its_encrypted_flags_are_persisted():
    mw, record, writes, paints = window(is_chat_locked=lambda jid: True)
    stars.apply_star_changed(mw, GROUP, "M", remote_star_state({"star": True}, 2))
    assert record["starred"] is False and not paints and len(writes) == 1


@pytest.mark.parametrize("data", [{}, {"chatId": "status@broadcast", "messageId": "M", "starred": True},
    {"chatId": GROUP, "messageId": "M", "starred": "false"}])
def test_invalid_payload_is_not_scheduled(monkeypatch, data):
    calls = []
    monkeypatch.setattr(stars.wx, "CallAfter", lambda *args: calls.append(args))
    stars.on_star_changed(N(_belongs_to_this_session=lambda d: True, main_window=None), data)
    assert calls == []


def test_socket_filters_other_accounts_and_schedules_the_valid_event(monkeypatch):
    calls = []
    monkeypatch.setattr(stars.wx, "CallAfter", lambda fn, *args: calls.append(args))
    client = N(_belongs_to_this_session=lambda d: d.get("session") == "ours", main_window="mw")
    for account in ("other", "ours"):
        stars.on_star_changed(client, {"session": account, "chatId": "1@c.us",
                                      "messageId": "M", "starred": False})
    assert len(calls) == 1 and calls[0][:3] == ("mw", "1@s.whatsapp.net", "M")
    assert calls[0][3]["_star_remote"] is False
