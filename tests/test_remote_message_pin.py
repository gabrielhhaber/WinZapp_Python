from types import SimpleNamespace as N

from core import remote_message_pin as pins
from core.message_pin_state import apply_pin_state


def test_stale_and_expired_pin_flags(monkeypatch):
    monkeypatch.setattr('core.message_pin_state.time.time', lambda: 100)
    record = {}
    assert apply_pin_state(record, True, 100000, 86400) and record["pinInChat"]
    assert not apply_pin_state(record, False, 99000, 0) and record["pinInChat"]
    assert apply_pin_state(record, False, 101000, 0) and not record["pinInChat"]
    assert apply_pin_state({}, True, 1000, 1)
    expired = {}
    apply_pin_state(expired, True, 1000, 1)
    assert expired["pinInChat"] is False


async def test_database_pin_update_is_atomic_scoped_and_preserves_content(in_memory_db):
    db = in_memory_db
    for jid in ("7@g.us", "8@g.us"):
        await db.insert_message(jid, {"key": {"id": "M"}, "messageTimestamp": 1,
            "message": {"conversation": "edited content"}, "status": 3})
    await db.update_message_pin_state("7@g.us", "M", True, 2000000000000, 86400)
    await db.update_message_pin_state("7@g.us", "M", False, 1999999999000, 0)
    row = await db.get_message_by_id("7@g.us", "M")
    assert row["pinInChat"] is True and row["message"]["conversation"] == "edited content"
    assert "pinInChat" not in await db.get_message_by_id("8@g.us", "M")
    await db.update_message_pin_state("7@g.us", "missing", True, 2000000000000, 0)
    assert await db.get_message_by_id("7@g.us", "missing") is None


def test_open_panel_flags_overview_and_persistence_are_updated():
    record = {"key": {"id": "M"}}
    chat = {"remoteJid": "7@g.us", "messages": {"messages": {"records": [record]}}}
    calls = []
    panel = N(conversation=chat, _sorted_messages=[],
        _pinned_messages_state_changed=lambda msgs, jid: calls.append(("overview", jid)),
        _repaint_or_repopulate=lambda ids: calls.append(("paint", ids)))
    mw = N(chats={"7@g.us": chat}, conversations_panel=panel,
        db=N(update_message_pin_state=lambda *a: calls.append(("db", a))),
        _msg_bg_executor=N(submit=lambda f: f()))
    pins.apply_message_pin(mw, "7@g.us", "M", True, 2000000000000, 86400)
    assert record["pinInChat"] and ("overview", "7@g.us") in calls and ("paint", ["M"]) in calls
    assert any(c[0] == "db" for c in calls)


def test_pin_socket_validation_and_account_scope(monkeypatch):
    calls = []
    monkeypatch.setattr(pins.wx, "CallAfter", lambda fn, *a: calls.append(a))
    client = N(main_window="mw", _belongs_to_this_session=lambda d: d.get("session") == "s")
    good = {"session": "s", "chatId": "7@g.us", "messageId": "M", "pinned": True,
            "timestamp": 2000000000000, "duration": 86400}
    for data in [{**good, "session": "other"}, {**good, "pinned": "false"}, {**good, "timestamp": True}, good]:
        pins.on_message_pin(client, data)
    assert calls == [("mw", "7@g.us", "M", True, 2000000000000, 86400)]
