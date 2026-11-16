"""Chat-label refresh notices are account-scoped and never mutate membership."""
from types import SimpleNamespace as N

from core import remote_label_updates as events


def test_only_valid_chat_associations_queue_one_context_guarded_refresh(monkeypatch):
    queued, refreshed = [], []
    monkeypatch.setattr(events.wx, "CallAfter", lambda fn, *a: queued.append((fn, a)))
    mw = N(token="first", _refresh_wa_lists_after_label_event=lambda: refreshed.append(True))
    client = N(main_window=mw, _belongs_to_this_session=lambda d: d.get("session") == "ours")
    event = {"session": "ours", "chat": {"id": {"_serialized": "123@lid"}},
             "ids": ["custom"], "labels": [], "type": "remove"}
    for changes in [{"session": "other"}, {"chat": None},
                    {"chat": {"id": "invalid@lid"}}, {"chat": {"id": "123@newsletter"}},
                    {"type": "unknown"}, {"ids": [42]}, {"labels": None}]:
        events.on_label_update(client, {**event, **changes})
    assert queued == []
    events.on_label_update(client, event)
    fn, args = queued.pop()
    mw.token = "second"
    fn(*args)
    assert refreshed == []
    events.on_label_update(client, event)
    fn, args = queued.pop()
    fn(*args)
    assert refreshed == [True]
