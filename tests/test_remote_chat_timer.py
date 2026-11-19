from types import SimpleNamespace as N

import pytest
from core import remote_chat_timer as timer


@pytest.mark.parametrize("duration,stamp", [(0, 0), (86400, 100), (7776000, 200)])
def test_valid_timer_identity(duration, stamp):
    assert timer.timer_target({"chatId": "1@c.us", "duration": duration, "settingTimestamp": stamp}) == (
        "1@s.whatsapp.net", duration, stamp)


@pytest.mark.parametrize("data", [None, {}, {"chatId": "status@broadcast", "duration": 0, "settingTimestamp": 0},
    {"chatId": "7@g.us", "duration": True, "settingTimestamp": 0},
    {"chatId": "7@g.us", "duration": -1, "settingTimestamp": 0},
    {"chatId": "7@g.us", "duration": 1, "settingTimestamp": "100"}])
def test_invalid_timers_do_not_become_off(data):
    assert timer.timer_target(data) is None


def test_aliases_and_open_record_update_and_stale_event_is_ignored():
    calls = []
    chat = {"remoteJid": "1@s.whatsapp.net", "ephemeralDuration": 86400, "ephemeralSettingTimestamp": 100}
    shown = dict(chat)
    mw = N(chats={"1@s.whatsapp.net": chat}, _lid_to_phone={"901@lid": "1@s.whatsapp.net"},
           conversations_panel=N(conversation=shown), _schedule_save=lambda **kw: calls.append(kw))
    timer.apply_chat_timer(mw, "901@lid", 604800, 200)
    assert chat["ephemeralDuration"] == shown["ephemeralDuration"] == 604800
    timer.apply_chat_timer(mw, "901@lid", 0, 100)
    assert chat["ephemeralDuration"] == 604800 and len(calls) == 1
    timer.apply_chat_timer(mw, "901@lid", 0, 300)
    assert chat["ephemeralDuration"] == 0
    timer.apply_chat_timer(mw, "7@g.us", 86400, 400)
    assert "7@g.us" not in mw.chats


def test_socket_is_account_scoped_and_shutdown_is_quiet(monkeypatch):
    calls = []
    monkeypatch.setattr(timer.wx, "CallAfter", lambda f, *a: calls.append(a))
    mw = N(_shutting_down=True)
    client = N(main_window=mw, _belongs_to_this_session=lambda d: d.get("session") == "ours")
    for session in ("other", "ours"):
        timer.on_chat_timer(client, {"session": session, "chatId": "7@g.us", "duration": 0, "settingTimestamp": 100})
    assert calls == [(mw, "7@g.us", 0, 100)]
    timer.apply_chat_timer(*calls[0])
