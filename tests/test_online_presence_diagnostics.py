"""Presence evidence is bounded, anonymous and follows the queued decision."""
from types import SimpleNamespace
import logging

import pytest

from core.online_presence import OnlinePresenceSender
from core.online_presence_diagnostics import PresenceDiagnostics, diagnostic_headers, record_write
from main_window.window_lifecycle import WindowLifecycleMixin
from tests.test_online_presence_order import workers


def decide(tracker, now, **flags):
    values = dict(connected=True, token=True, shutdown=False, background=False,
                  hidden=False, shown=True, iconized=False, active=True)
    return tracker.decide(**{**values, **flags}, now=now)


def test_decisions_are_rate_limited_but_the_latest_state_remains_sendable(caplog):
    tracker = PresenceDiagnostics()
    with caplog.at_level(logging.INFO):
        first = decide(tracker, 0)
        fast = decide(tracker, .1, active=False)
        settled = decide(tracker, 1.1, active=False)
        idle = decide(tracker, 20, active=False)
        heartbeat = decide(tracker, 61.1, active=False)
    assert first.online and first.trace
    assert not fast.online and not fast.trace
    assert not settled.online and settled.trace
    assert not idle.trace and heartbeat.trace
    assert len(caplog.records) == 3
    assert "active=False" in caplog.text


@pytest.mark.parametrize("flag", ["hidden", "background", "iconized", "active", "shown"])
def test_decision_keeps_the_existing_window_policy(flag):
    value = flag not in ("active", "shown")
    assert not decide(PresenceDiagnostics(), 0, **{flag: value}).online


def test_reconnect_changes_epoch_and_queued_context_cannot_drift(workers):
    tracker = PresenceDiagnostics()
    first = decide(tracker, 0)
    offline = decide(tracker, 2, active=False)
    sent = []
    sender = OnlinePresenceSender(lambda state, **kw: sent.append((state, kw["diagnostic"])))
    sender.submit("available", diagnostic=first)
    sender.submit("unavailable", diagnostic=offline)
    workers.pop()()
    assert sent == [("unavailable", offline)]
    disconnected = decide(tracker, 3, connected=False)
    reconnected = decide(tracker, 5)
    assert first.epoch < disconnected.epoch < reconnected.epoch


@pytest.mark.parametrize("payload,status,result", [
    ({"status": "success"}, 200, "success"),
    ({"status": "error"}, 200, "api_error"),
    ({"status": "error"}, 500, "http_error"),
    (None, 200, "unknown"),
])
def test_only_bounded_diagnostic_fields_reach_the_log(caplog, payload, status, result):
    decision = decide(PresenceDiagnostics(), 0)
    with caplog.at_level(logging.INFO):
        record_write(decision, response=SimpleNamespace(status_code=status, json=lambda: payload), elapsed=.1)
    assert "result=" + result in caplog.text
    assert "receiver_confirmed=false" in caplog.text


def test_untrusted_body_and_exception_text_are_never_logged(caplog):
    secret = "PRIVATE_NAME_Bearer_SECRET@lid"
    payload = {"status": "error", "message": secret,
               "diagnostic": {"native": secret, "runtime": {"status": secret, "available": secret}}}
    decision = decide(PresenceDiagnostics(), 0)
    with caplog.at_level(logging.INFO):
        record_write(decision, response=SimpleNamespace(status_code=200, json=lambda: payload))
    assert secret not in caplog.text
    assert "native=unknown runtime=unknown" in caplog.text


def test_send_logs_the_captured_decision_and_same_header_without_reading_wx(monkeypatch, caplog):
    import main_window.window_lifecycle as lifecycle
    decision = decide(PresenceDiagnostics(), 0, active=False)
    owner = SimpleNamespace(token="synthetic", _wa_connected=True, _shutting_down=False,
                            wpp_server="https://invalid.test", wpp_port=1)
    calls = []
    monkeypatch.setattr(lifecycle, "api_post", lambda url, **kw: calls.append(kw) or
                        SimpleNamespace(status_code=200, json=lambda: {"status": "success"}))
    with caplog.at_level(logging.INFO):
        WindowLifecycleMixin._send_presence(owner, "unavailable", diagnostic=decision)
    assert calls[0]["json"] == {"isOnline": False}
    assert calls[0]["headers"]["X-WinZapp-Presence-Sequence"] == str(decision.sequence)
    assert "online=False" in caplog.text and "synthetic" not in caplog.text


def test_cancel_drops_the_pending_context(workers):
    sent = []
    sender = OnlinePresenceSender(lambda *a, **kw: sent.append((a, kw)))
    sender.submit("available", diagnostic=decide(PresenceDiagnostics(), 0))
    sender.submit(None)
    workers.pop()()
    assert sent == []
    assert diagnostic_headers(None) == {}


def test_context_stays_with_each_inflight_write_and_its_newer_pending_state(workers):
    tracker = PresenceDiagnostics()
    first = decide(tracker, 0)
    second = decide(tracker, 2, active=False)
    sent = []
    def send(state, *, diagnostic):
        sent.append((state, diagnostic))
        if len(sent) == 1:
            sender.submit("unavailable", diagnostic=second)
    sender = OnlinePresenceSender(send)
    sender.submit("available", diagnostic=first)
    workers.pop()()
    assert sent == [("available", first), ("unavailable", second)]


@pytest.mark.parametrize("kind,expected", [("transport", "transport_error"), ("skipped", "skipped"),
                                         ("invalid_json", "invalid_response")])
def test_failure_evidence_is_fixed_and_receiver_is_never_confirmed(caplog, kind, expected):
    decision = decide(PresenceDiagnostics(), 0)
    def invalid():
        raise ValueError("PRIVATE_JSON_OR_TOKEN")
    with caplog.at_level(logging.INFO):
        record_write(decision, failure=kind == "transport", skipped=kind == "skipped",
                     response=SimpleNamespace(status_code=200, json=invalid) if kind == "invalid_json" else None)
    assert "result=" + expected in caplog.text
    assert "receiver_confirmed=false" in caplog.text and "PRIVATE" not in caplog.text


def test_unsampled_write_does_not_parse_or_log_the_response(caplog):
    tracker = PresenceDiagnostics()
    decide(tracker, 0)
    decision = decide(tracker, .1)
    assert not decision.trace
    caplog.clear()  # the sampled decision above may already have logged
    record_write(decision, response=SimpleNamespace(json=lambda: pytest.fail("unsampled body parsed")))
    assert caplog.records == []


def test_short_diagnostic_counters_wrap_without_becoming_phone_shaped():
    tracker = PresenceDiagnostics()
    tracker.sequence = tracker.epoch = 9_999_999
    decision = decide(tracker, 0)
    headers = diagnostic_headers(decision)
    assert decision.sequence == decision.epoch == 1
    assert headers["X-WinZapp-Presence-Sequence"] == headers["X-WinZapp-Presence-Epoch"] == "1"
