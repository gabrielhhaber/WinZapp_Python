"""Live poll events refresh the correct poll without opening any wx windows."""
from types import SimpleNamespace as N

import pytest

from core import poll_events
from ui.conversation_panel.poll_messages import PollMessagesMixin


@pytest.mark.parametrize("mid", ["X" * 18, "X" * 20, "X" * 22, "X" * 32])
def test_browser_and_bridge_msgkeys_are_accepted(mid):
    assert poll_events.poll_target({"chatId": {"_serialized": "7@g.us"},
                                    "msgId": {"id": mid}}) == ("7@g.us", mid)
    assert poll_events.poll_target({"chatId": "1@c.us",
                                    "msgId": f"false_1@c.us_{mid}"}) == ("1@s.whatsapp.net", mid)


@pytest.mark.parametrize("data", [None, {}, {"chatId": "status@broadcast", "msgId": "X"},
    {"chatId": "7@g.us", "msgId": {"id": ""}}, {"chatId": "7@g.us", "msgId": {"id": []}}])
def test_malformed_events_and_statuses_are_rejected(data):
    assert poll_events.poll_target(data) is None


def test_socket_dispatch_is_scoped_to_the_account(monkeypatch):
    delivered = []
    monkeypatch.setattr(poll_events.wx, "CallAfter", lambda f, *a: f(*a))
    client = N(_belongs_to_this_session=lambda d: d.get("session") == "ours",
        main_window=N(conversations_panel=N(on_poll_response=lambda *a: delivered.append(a))))
    for session in ("other", "ours"):
        poll_events.on_poll_response(client, {"session": session,
            "msgId": {"id": "P"}, "chatId": {"_serialized": "7@g.us"}})
    assert delivered == [("7@g.us", "P")]


def panel():
    calls = []
    stub = N(main_window=N(), conversation={"remoteJid": "7@g.us"},
        _open_poll_records=lambda: [{"key": {"id": "P"}}],
        refresh_poll_votes=lambda **kw: calls.append(kw))
    stub.on_poll_response = lambda *a: PollMessagesMixin.on_poll_response(stub, *a)
    return stub, calls


def test_only_a_poll_in_the_open_chat_triggers_an_immediate_refresh():
    stub, calls = panel()
    stub.on_poll_response("other@g.us", "P")
    stub.on_poll_response("7@g.us", "OTHER")
    assert calls == []
    stub.on_poll_response("7@g.us", "P")
    assert calls == [{"force": True}]


def test_known_lid_phone_aliases_refresh_the_same_open_poll():
    stub, calls = panel()
    stub.conversation = {"remoteJid": "1@s.whatsapp.net"}
    stub.main_window._lid_to_phone = {"901@lid": "1@s.whatsapp.net"}
    stub.on_poll_response("901@lid", "P")
    assert calls == [{"force": True}]


def test_an_event_during_a_lookup_is_retried_even_if_the_old_result_is_empty():
    stub, calls = panel()
    stub._poll_votes_busy = True
    stub.on_poll_response("7@g.us", "P")
    assert calls == []
    PollMessagesMixin._apply_poll_votes(stub, "7@g.us", [], {})
    assert calls == [{"force": True}]
    assert stub._poll_votes_pending_event is None


def test_a_pending_event_is_ignored_if_the_user_has_left_the_chat():
    stub, calls = panel()
    stub._poll_votes_busy = True
    stub.on_poll_response("7@g.us", "P")
    stub.conversation = {"remoteJid": "other@g.us"}
    PollMessagesMixin._apply_poll_votes(stub, "7@g.us", [], {})
    assert calls == []
