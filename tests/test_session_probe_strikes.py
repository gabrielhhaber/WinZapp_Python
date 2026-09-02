"""Tests for the consecutive-strike tolerance on check_whatsapp_reachable()'s
``/check-connection-session`` probe.

Reported live (logs of 2026-08-08 21:50): WinZapp announced "Modo offline
ativado automaticamente" — sound and speech — while the machine's internet was
perfectly fine, then marked a *complete* 574-chat sync as incomplete, aborted
LID resolution and skipped the whole media phase.

Root cause: WhatsApp Web reloaded its own page inside the headless Chrome
(wppconnect.log: "Execution context was destroyed, most likely because of a
navigation"). For the next 28 seconds WPPConnect's isConnected() evaluated
``WAPI.isConnected()`` against a page whose injected WAPI namespace was gone —
~700 "WAPI is not defined" ReferenceErrors — so /check-connection-session
answered "not connected". The health check happened to land inside that
window, and this branch took a single negative reading as proof of an outage:
it set _offline_probe_strikes straight to the limit and returned False on the
spot, bypassing the very tolerance mechanism the host probe next to it uses.

A page reload is a routine WhatsApp Web event; an outage is not. Both now need
_OFFLINE_PROBE_STRIKES consecutive negatives, which rides out a reload while
still detecting a real outage on the following health-check tick (~30 s).

MainWindow is a wx.Frame and cannot be instantiated without a running wx.App,
so the method under test is exercised as a plain function against a small stub
— same approach as tests/test_socket_stream_nudge.py.
"""

import pytest

from main import MainWindow


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _Stub:
    _OFFLINE_PROBE_STRIKES = MainWindow._OFFLINE_PROBE_STRIKES
    check_whatsapp_reachable = MainWindow.check_whatsapp_reachable

    def __init__(self, connected=True, host_reachable=True):
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "test-token"
        self._wa_connected = connected
        self._offline_probe_strikes = 0
        self._host_reachable = host_reachable
        self.host_probes = 0

    def _probe_whatsapp_host(self):
        self.host_probes += 1
        return self._host_reachable


def _session_says(monkeypatch, *responses):
    """Queue the /check-connection-session responses, last one repeating."""
    queue = list(responses)

    def _fake_get(url, headers=None, timeout=None, **kw):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr("main.requests.get", _fake_get)


class TestSessionProbeStrikes:
    def test_a_single_negative_does_not_go_offline(self, monkeypatch):
        """The regression: one WAPI-is-not-defined blip used to be enough."""
        _session_says(monkeypatch, _Resp(200, {"status": False}))
        s = _Stub(connected=True)
        assert s.check_whatsapp_reachable() is True
        assert s._offline_probe_strikes == 1

    def test_two_consecutive_negatives_do_go_offline(self, monkeypatch):
        _session_says(monkeypatch, _Resp(200, {"status": False}))
        s = _Stub(connected=True)
        assert s.check_whatsapp_reachable() is True   # strike 1 — held
        assert s.check_whatsapp_reachable() is False  # strike 2 — believed

    def test_a_recovered_session_clears_the_tally(self, monkeypatch):
        """The 28-second reload case: negative, then healthy again. The second
        reading must reset the count so a later, unrelated blip still gets its
        own full grace rather than tipping straight over."""
        _session_says(
            monkeypatch,
            _Resp(200, {"status": False}),
            _Resp(200, {"status": True}),
        )
        s = _Stub(connected=True)
        assert s.check_whatsapp_reachable() is True
        assert s._offline_probe_strikes == 1
        assert s.check_whatsapp_reachable() is True
        assert s._offline_probe_strikes == 0

    def test_a_404_is_treated_the_same_as_a_false_status(self, monkeypatch):
        _session_says(monkeypatch, _Resp(404))
        s = _Stub(connected=True)
        assert s.check_whatsapp_reachable() is True
        assert s.check_whatsapp_reachable() is False

    def test_the_first_strike_does_not_run_the_host_probe(self, monkeypatch):
        """A negative session probe is authoritative about WhatsApp Web being
        down inside the browser — reachable WhatsApp servers say nothing about
        that, so holding the current state must not consult them."""
        _session_says(monkeypatch, _Resp(200, {"status": False}))
        s = _Stub(connected=True)
        s.check_whatsapp_reachable()
        assert s.host_probes == 0

    def test_a_blip_while_already_offline_stays_offline(self, monkeypatch):
        """Holding "the current state" must not read as online when the app
        was already offline."""
        _session_says(monkeypatch, _Resp(200, {"status": False}))
        s = _Stub(connected=False)
        assert s.check_whatsapp_reachable() is False

    def test_a_healthy_session_still_defers_to_the_host_probe(self, monkeypatch):
        """status: true is not conclusive — the plain "this machine has no
        internet" case is only caught by the direct probe."""
        _session_says(monkeypatch, _Resp(200, {"status": True}))
        s = _Stub(connected=True, host_reachable=False)
        assert s.check_whatsapp_reachable() is True   # host strike 1 — held
        assert s.check_whatsapp_reachable() is False  # host strike 2
        assert s.host_probes == 2

    def test_a_healthy_session_and_a_reachable_host_is_online(self, monkeypatch):
        _session_says(monkeypatch, _Resp(200, {"status": True}))
        s = _Stub(connected=False)
        assert s.check_whatsapp_reachable() is True
        assert s._offline_probe_strikes == 0

    def test_a_request_exception_falls_through_to_the_host_probe(self, monkeypatch):
        """An unreachable local API is not a session verdict — that case has
        its own strike counter in check_wa_connection_http()."""
        def _boom(*a, **kw):
            raise ConnectionError("local API down")

        monkeypatch.setattr("main.requests.get", _boom)
        s = _Stub(connected=True)
        assert s.check_whatsapp_reachable() is True
        assert s.host_probes == 1


class TestOfflineProbeToleranceDuringInitialSync:
    """Both strike counters in check_whatsapp_reachable() now widen tenfold
    while an initial sync is running — the same reasoning, and the same *10
    factor, check_wa_connection_http()'s except branch already applies to
    _HTTP_PROBE_STRIKES. Reported live: a long history sync can leave the
    local Node process too busy to answer this probe quickly, which used to
    flip the app to "modo offline" and abort the very sync that was keeping
    Node busy, well before the more tolerant HTTP check ever would."""

    def test_session_probe_tolerates_ten_times_the_strikes_during_sync(self, monkeypatch):
        _session_says(monkeypatch, _Resp(200, {"status": False}))
        s = _Stub(connected=True)
        s._initial_sync_running = True
        for _ in range(19):
            assert s.check_whatsapp_reachable() is True
        assert s._offline_probe_strikes == 19
        assert s.check_whatsapp_reachable() is False
        assert s._offline_probe_strikes == 20

    def test_host_probe_tolerates_ten_times_the_strikes_during_sync(self, monkeypatch):
        _session_says(monkeypatch, _Resp(200, {"status": True}))
        s = _Stub(connected=True, host_reachable=False)
        s._initial_sync_running = True
        for _ in range(19):
            assert s.check_whatsapp_reachable() is True
        assert s.check_whatsapp_reachable() is False

    def test_outside_a_sync_the_tolerance_is_unchanged(self, monkeypatch):
        """_initial_sync_running absent (the common case) must behave exactly
        as before this change — two strikes, not twenty."""
        _session_says(monkeypatch, _Resp(200, {"status": False}))
        s = _Stub(connected=True)
        assert s.check_whatsapp_reachable() is True
        assert s.check_whatsapp_reachable() is False

    def test_a_finished_sync_goes_back_to_the_normal_tolerance(self, monkeypatch):
        """_initial_sync_running=False (sync completed/aborted) must not
        leave the widened budget behind."""
        _session_says(monkeypatch, _Resp(200, {"status": False}))
        s = _Stub(connected=True)
        s._initial_sync_running = False
        assert s.check_whatsapp_reachable() is True
        assert s.check_whatsapp_reachable() is False
