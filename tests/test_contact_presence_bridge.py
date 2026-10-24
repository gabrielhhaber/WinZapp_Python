"""Actual TS bridge in Node VM plus actual Python socket/cache path; no GUI/account."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from core.contact_presence import cached, merge
from core.websocket_client import WebSocketClient
import core.websocket_client as transport
from tests.test_contact_presence_freshness import Owner, PN

ROOT = Path(__file__).resolve().parents[1]
COMPILER = Path(os.environ.get("WINZAPP_TEST_TYPESCRIPT", ROOT / "client/api/node_modules/typescript/lib/typescript.js"))
NODE = shutil.which("node")


def run(mode="fields", case="t", **fields):
    if not NODE or not COMPILER.is_file():
        pytest.skip("Existing Node and TypeScript compiler required; no install")
    result = subprocess.run([NODE, str(Path(__file__).with_name("contact_presence_bridge_runtime.cjs")),
                             json.dumps({"compiler": str(COMPILER), "mode": mode, "case": case, **fields})],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("case,fields", [
    ("t", {"lastSeen": 2000, "isOnline": False}),
    ("add", {"lastSeen": 1000, "isOnline": False}),
    ("online", {"lastSeen": None, "isOnline": True}),
    ("deny", {"lastSeen": None, "restricted": True}),
    ("hasData-pending", {"lastSeen": None, "isOnline": None, "snapshotStatus": "pending"}),
    ("hasData-ready", {"lastSeen": 1000, "snapshotStatus": "ready"}),
    ("chatstate", {"lastSeen": 3000}),
    ("type", {"state": "composing", "lastSeen": None}),
    ("burst", {"restricted": True, "lastSeen": None}),
    ("duplicate", {"lastSeen": 1000}),
    ("reinstall", {"lastSeen": 1000}),
    ("old-generation", {"lastSeen": 4000}),
    ("replacement-store", {"lastSeen": 1000}),
])
def test_real_field_events_emit_one_current_snapshot(case, fields):
    result = run(case=case)
    assert result["installed"] is True
    assert len(result["sent"]) == 1
    event = result["sent"][0]["event"]
    assert all(event[k] == value for k, value in fields.items())
    assert event["t"] == 1700000000123
    assert event["lastSeen"] != event["t"]
    if case == "replacement-store":
        assert result["listeners"] == 0
    else:
        assert result["listeners"] == 9


@pytest.mark.parametrize("case", ["old-model", "remove", "reset", "unsupported", "attach-failure"])
def test_removed_unready_or_unsupported_store_has_no_stale_delivery(case):
    result = run(case=case)
    assert result["sent"] == []
    if case == "attach-failure":
        assert result["installed"] is False and result["listeners"] == 0


@pytest.mark.parametrize("jid,expected", [("456@lid", 1), (PN, 1), ("123@c.us", 1),
    ("1@g.us", 0), ("1@newsletter", 0), ("bad", 0)])
def test_native_bridge_keeps_private_identity_scope(jid, expected):
    assert len(run(id=jid)["sent"]) == expected


@pytest.mark.parametrize("case", ["initial", "repeat", "owner", "reload", "late-evaluate"])
def test_exposed_binding_routes_only_current_owner_and_load_generation(case):
    result = run("host", case)
    assert result["exposes"] == 1 and result["loadHooks"] == 2
    assert result["timers"] == 0 and result["listeners"] == 9
    assert len(result["sent"]) == 1
    event = result["sent"][0]["event"]
    assert event["session"] == ("second" if case in ("owner", "late-evaluate") else "first")
    assert "ignoredSecret" not in event
    assert result["firstLegacy"] is False and result["groupLegacy"] is True


def test_bridge_readiness_retry_is_finite_and_retains_legacy_fallback():
    result = run("host", "retry")
    assert result["evaluates"] == 21 and result["timers"] == 0
    assert result["firstLegacy"] is True and result["sent"] == []
    assert len(result["logs"]) == 1


def test_close_cancels_install_and_rejects_events():
    result = run("host", "close")
    assert result["evaluates"] == 1 and result["timers"] == 0
    assert result["firstLegacy"] is False and result["sent"] == []


def test_malformed_native_envelopes_never_reach_local_socket():
    assert run("host", "malformed")["sent"] == []


@pytest.mark.parametrize("case", ["hung", "reject"])
def test_real_registration_does_not_wait_for_failed_subscription_and_keeps_webhook_unchanged(case):
    result = run("bootstrap", case)
    assert result["completed"] and result["calls"] == ["legacy", "group"]
    assert result["nativeListeners"] == 9
    assert len(result["io"]) == 2  # legacy group and native private snapshot.
    assert len(result["webhooks"]) == 2  # only the two pre-existing legacy events.
    assert result["webhooks"][0] == {"id": "123@c.us", "state": "typing", "t": 10}
    assert all("lastSeen" not in event for event in result["webhooks"])


@pytest.fixture
def wire(monkeypatch):
    owner, callbacks, received = Owner(), [], []
    def receive(jid, presences, **kwargs):
        received.append(kwargs)
        for participant, data in presences.items():
            merge(owner, participant, data)
    owner.on_presence_update = receive
    monkeypatch.setattr(transport, "wx", SimpleNamespace(CallAfter=callbacks.append))
    peer = SimpleNamespace(main_window=owner, _presence_delivery_generation=0,
                           _belongs_to_this_session=lambda payload: True)
    def deliver(event):
        WebSocketClient.on_wpp_presence_changed(peer, event)
        while callbacks:
            callbacks.pop(0)()
        return cached(owner, PN)
    return owner, deliver, received


@pytest.mark.parametrize("case", ["t", "online", "deny", "type", "hasData-pending"])
def test_actual_ts_snapshot_through_python_socket_and_cache(wire, case):
    owner, deliver, received = wire
    merge(owner, PN, {"lastKnownPresence": "available", "isOnline": True, "lastSeen": 1000})
    event = run(case=case)["sent"][0]["event"]
    entry = deliver(event)
    if case == "hasData-pending":
        assert entry["isOnline"] is None and entry["lastKnownPresence"] == ""
        assert entry["lastSeen"] is None
        assert received == [{}]
    else:
        assert received == [{"refresh_offline": False}]
        if case == "t":
            assert entry["lastSeen"] == 2000
        elif case == "deny":
            assert entry["restricted"] is True and entry["isOnline"] is None
            assert entry["lastSeen"] is None
        elif case == "type":
            assert entry["lastKnownPresence"] == "composing"
        else:
            assert entry["isOnline"] is True and entry["lastSeen"] is None


def test_pending_cannot_revoke_existing_denial(wire):
    owner, deliver, _ = wire
    merge(owner, PN, {"restricted": True})
    event = {"id": PN, "state": "", "isOnline": None, "lastSeen": None,
             "snapshotStatus": "pending", "restricted": None}
    assert deliver(event)["restricted"] is True


def test_socket_log_does_not_contain_identity_or_last_seen(wire, caplog):
    _, deliver, _ = wire
    with caplog.at_level("INFO"):
        deliver({"id": PN, "state": "unavailable", "isOnline": False,
                 "lastSeen": 1700000000, "restricted": False, "snapshotStatus": "ready"})
    assert PN not in caplog.text and "1700000000" not in caplog.text


@pytest.mark.parametrize("model,status", [
    ({"hasData": True, "isOnline": False, "chatstate": {}}, "malformed"),
    ({"hasData": True, "chatstate": {"type": "unavailable"}}, "malformed"),
    ({"hasData": True, "isOnline": False, "chatstate": {"type": "unavailable", "deny": "false"}}, "malformed"),
    ({"hasData": False}, "pending"),
    ({"hasData": "true"}, "pending"),
    ({"hasData": False, "chatstate": {"deny": True}}, "pending"),
])
def test_native_model_readiness_is_not_invented_from_malformed_fields(model, status):
    result = run("snapshot", model=model)
    assert result["status"] == status
    assert "lastSeen" not in result
    if model.get("chatstate", {}).get("deny") is True:
        assert result["restricted"] is True


def test_native_read_exception_is_error_not_pending_and_cannot_fallback():
    result = run("snapshot", fault="primary", fallback={"hasData": True, "isOnline": False,
                 "chatstate": {"type": "unavailable", "t": 2000}})
    assert result == {"status": "error"}


def test_unready_primary_uses_ready_same_identity_chat_cache():
    result = run("snapshot", model={"hasData": False}, fallback={"hasData": True,
                 "isOnline": False, "chatstate": {"type": "unavailable", "t": 2000}})
    assert result["status"] == "ready" and result["lastSeen"] == 2000


def test_primary_denial_cannot_be_bypassed_with_ready_chat_cache():
    result = run("snapshot", model={"hasData": False, "chatstate": {"deny": True}},
                 fallback={"hasData": True, "isOnline": True, "chatstate": {"type": "available", "t": 2000}})
    assert result == {"status": "pending", "restricted": True}


def test_usable_primary_does_not_require_a_working_fallback_store():
    result = run("snapshot", fault="chat")
    assert result["status"] == "ready" and result["lastSeen"] == 1000


def test_actual_http_route_keeps_native_read_error_terminal():
    from tests.test_contact_presence_runtime import _run
    result = _run({"storeThrows": True})
    assert result["body"]["presence"] == {"status": "error"}
    assert result["calls"]["legacy"] == []


@pytest.mark.parametrize("case", ["t", "online", "deny", "hasData-pending"])
def test_native_snapshot_reaches_all_three_real_views_without_ready_query_loop(monkeypatch, case, caplog):
    from tests.test_contact_presence_transition_acceptance import pipeline
    event = run(case=case)["sent"][0]["event"]
    loop, _, rendered, button = pipeline(monkeypatch)
    loop.begin()
    assert rendered() == ("Online", "Online", "Online")
    loop.now = 101
    if case == "hasData-pending":
        loop.answer = None
    peer = SimpleNamespace(main_window=loop.owner, _presence_delivery_generation=0,
                           _belongs_to_this_session=lambda event: True)
    with caplog.at_level("INFO"):
        WebSocketClient.on_wpp_presence_changed(peer, event)
        while loop.callbacks:
            loop.callbacks.pop(0)()
    if case == "t":
        assert all("Seen" in view for view in rendered())
    elif case == "online":
        assert rendered() == ("Online", "Online", "Online")
    else:
        assert rendered() == ("Unknown", "Contact", "")
    assert len(loop.reads) == 1
    assert button.focuses == 0
    assert PN not in caplog.text and "1700000000123" not in caplog.text


def test_native_typing_is_announced_once_without_logging_person_or_text(monkeypatch, caplog):
    from tests.test_contact_presence_transition_acceptance import pipeline
    import main_window.chat_events as events
    import main_window.contact_presence as coordinator
    event = run(case="type")["sent"][0]["event"]
    loop, _, _, _ = pipeline(monkeypatch)
    monkeypatch.setattr(events, "wx", coordinator.wx)
    spoken = []
    loop.owner.speak_output.output = spoken.append
    loop.owner._resolve_jid_name = lambda *args: "SYNTHETIC_PERSON"
    peer = SimpleNamespace(main_window=loop.owner, _presence_delivery_generation=0,
                           _belongs_to_this_session=lambda item: True)
    with caplog.at_level("INFO"):
        for _ in range(2):
            WebSocketClient.on_wpp_presence_changed(peer, event)
            while loop.callbacks:
                loop.callbacks.pop(0)()
    assert len(spoken) == 1
    assert "SYNTHETIC_PERSON" not in caplog.text and PN not in caplog.text
