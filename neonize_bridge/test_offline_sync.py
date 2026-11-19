"""WhatsApp's own "this much is waiting" / "that was all" reach the client."""
from types import SimpleNamespace

import pytest

import offline_sync


class Cli:
    def __init__(self):
        self.handlers = {}

    def event(self, kind):
        def add(func):
            self.handlers[kind] = func
            return func
        return add


class Session:
    name = "account-1"

    def __init__(self):
        self.sent = []

    def emit(self, event, data):
        self.sent.append((event, data))


def wired():
    nev = pytest.importorskip("neonize.events")
    if not hasattr(nev, "OfflineSyncPreviewEv"):
        pytest.skip("neonize without the offline sync events")
    cli, s = Cli(), Session()
    offline_sync.register_events(cli, s)
    return nev, cli, s


def test_the_preview_tells_the_client_how_many_messages_are_coming():
    nev, cli, s = wired()
    cli.handlers[nev.OfflineSyncPreviewEv](
        None, SimpleNamespace(Total=1800, Message=1500, Receipts=250,
                              Notifications=50, AppDataChanges=0))
    assert s.sent == [("offline-sync", {
        "session": "account-1", "state": "started", "messages": 1500, "total": 1800})]


def test_the_end_of_the_delivery_reaches_the_client():
    nev, cli, s = wired()
    cli.handlers[nev.OfflineSyncCompletedEv](None, SimpleNamespace(Count=1800))
    assert s.sent == [("offline-sync", {
        "session": "account-1", "state": "completed", "count": 1800})]


def test_a_malformed_event_is_logged_and_sends_nothing():
    nev, cli, s = wired()
    cli.handlers[nev.OfflineSyncPreviewEv](None, SimpleNamespace())
    assert s.sent == []


def test_payloads_are_plain_json_values():
    event = SimpleNamespace(Total=3, Message=2, Count=3)
    assert offline_sync.started_payload("a", event) == {
        "session": "a", "state": "started", "messages": 2, "total": 3}
    assert offline_sync.completed_payload("a", event) == {
        "session": "a", "state": "completed", "count": 3}
