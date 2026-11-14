"""Settings notices use the existing local actions, with no outbound requests."""
from types import SimpleNamespace as N

import pytest

from core import remote_chat_settings as settings
from main_window.chat_actions import ChatActionsMixin


@pytest.mark.parametrize("setting,value", [("mute", -1), ("mute", 0), ("mute", 2000000000),
                                          ("archive", True), ("pin", False), ("lock", True)])
def test_valid_targets_normalize_chat_identity(setting, value):
    assert settings.settings_target({"chatId": "1:2@c.us", "setting": setting, "value": value}) == (
        "1@s.whatsapp.net", setting, value)


@pytest.mark.parametrize("setting,value", [("mute", True), ("mute", "0"), ("mute", -2),
    ("mute", float("nan")), ("mute", 2**53), ("archive", "false"), ("pin", 1), ("other", True)])
def test_invalid_fields_do_not_guess_a_setting(setting, value):
    assert settings.settings_target({"chatId": "7@g.us", "setting": setting, "value": value}) is None


def test_mute_and_unmute_update_aliases_and_persist_without_sending(monkeypatch):
    writes, calls = [], []
    mw = N(_muted_chats={}, _lid_to_phone={"901@lid": "1@s.whatsapp.net"}, _phone_to_lid={},
           _normalize_jid=lambda jid: jid, _schedule_set_chats=lambda: calls.append("refresh"),
           db=N(set_metadata_json=lambda key, value: writes.append((key, dict(value)))))
    mw._mute_state_jids = lambda jid: ChatActionsMixin._mute_state_jids(mw, jid)
    mw._apply_mute_state = lambda jid, expiry: ChatActionsMixin._apply_mute_state(mw, jid, expiry)
    monkeypatch.setattr(settings.time, "time", lambda: 100)
    for value, expected in [(-1, -1), (200, 200), (0, None), (99, None)]:
        settings.apply_chat_setting(mw, "901@lid", "mute", value)
        assert mw._muted_chats == ({"901@lid": expected, "1@s.whatsapp.net": expected} if expected else {})
    assert len(writes) == len(calls) == 4


def test_archive_pin_and_shutdown_use_only_the_local_update_paths():
    calls = []
    mw = N(on_chat_archive_update=lambda *a: calls.append(("archive", *a)),
           on_chat_pin_update=lambda *a: calls.append(("pin", *a)))
    settings.apply_chat_setting(mw, "7@g.us", "archive", True)
    settings.apply_chat_setting(mw, "7@g.us", "pin", False)
    mw._shutting_down = True
    settings.apply_chat_setting(mw, "7@g.us", "archive", False)
    assert calls == [("archive", "7@g.us", True), ("pin", "7@g.us", False)]


def test_socket_guard_rejects_wrong_accounts_and_statuses(monkeypatch):
    calls = []
    monkeypatch.setattr(settings.wx, "CallAfter", lambda fn, *args: calls.append(args))
    client = N(main_window="mw", _belongs_to_this_session=lambda d: d.get("session") == "ours")
    for session, chat in [("other", "7@g.us"), ("ours", "status@broadcast"), ("ours", "7@g.us")]:
        settings.on_chat_settings(client, {"session": session, "chatId": chat, "setting": "mute", "value": -1})
    assert calls == [("mw", "7@g.us", "mute", -1)]
