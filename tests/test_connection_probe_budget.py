"""The two halves of the offline verdict, pinned to each other.

``check_whatsapp_reachable()``'s strike tally is the only thing in the app that
can declare a WhatsApp Web page dead: everything downstream of it — the offline
announcement, ``_nudge_whatsapp_socket_stream()``, the
``_DEAD_BROWSER_RESTART_STRIKES`` escalation into ``_restart_wpp_session()``, the
orphan-session sweep — is gated on it returning False. And the tally is only
ever reached when ``/check-connection-session`` *answers*: a request that times
out client-side raises, lands in the ``except``, and falls through to
``_probe_whatsapp_host()`` (a HEAD at web.whatsapp.com, which happily succeeds
on a machine with internet) without counting a strike at all.

So the Python half is worthless unless the Node half answers inside the budget
this client gives it, and on wppconnect 2.3.2 that stopped being free:
``isConnected()`` awaits ``waitForPageLoad()``, which sits on puppeteer's 30 s
default waiting for ``WPP.isReady`` — and never returns at all when the page's
``load`` event never fired. Called raw, the route outlived the client's 10 s on
exactly the state it exists to detect: ``status-session`` said CONNECTED,
``check_whatsapp_reachable()`` said True, every send came back
``probe_timeout`` and was requeued in silence, and a blind user heard nothing at
all. On 2.3.1 the same page threw immediately, the route answered
``{status: false}``, and two strikes (~60 s) took the app offline and into the
restart ladder — so an unbounded route here is a regression, not inherited debt.

MainWindow is a wx.Frame and cannot be instantiated without a running wx.App,
so the method is exercised unbound against a small stub — the same pattern as
tests/test_session_probe_strikes.py, whose cases cover the tally itself in
depth. What is new here is the coupling: the budget the Node route works to,
read out of the patch that ships it.
"""

import re
from pathlib import Path

import pytest

from main import MainWindow


ROOT = Path(__file__).resolve().parents[1]
SESSION_CONTROLLER = (
    ROOT / "client" / "api_patches" / "src" / "controller" / "sessionController.ts"
).read_text(encoding="utf-8")

# Only checkConnectionSession is under test; the file holds ~30 other routes.
CHECK_CONNECTION_ROUTE = SESSION_CONTROLLER[
    SESSION_CONTROLLER.index("export async function checkConnectionSession(") :
].split("\nexport ")[0]


def _node_budget_seconds() -> float:
    match = re.search(r"const CONNECTION_PROBE_BUDGET_MS = (\d+);", SESSION_CONTROLLER)
    assert match, "the route no longer declares a probe budget at all"
    return int(match.group(1)) / 1000.0


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _Stub:
    _OFFLINE_PROBE_STRIKES = MainWindow._OFFLINE_PROBE_STRIKES
    _LIVE_WPP_EVENT_FRESHNESS_SECONDS = MainWindow._LIVE_WPP_EVENT_FRESHNESS_SECONDS
    check_whatsapp_reachable = MainWindow.check_whatsapp_reachable

    def __init__(self, connected=True, host_reachable=True):
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "test-token"
        self._wa_connected = connected
        self._offline_probe_strikes = 0
        self._offline_probe_first_strike_ts = 0.0
        self._host_reachable = host_reachable
        self.host_probes = 0

    def _probe_whatsapp_host(self):
        self.host_probes += 1
        return self._host_reachable


@pytest.fixture
def session_probe(monkeypatch):
    """Answer /check-connection-session, recording the timeout the client
    actually allowed it. Returns the recording list."""
    calls = []

    def _fake_get(url, headers=None, timeout=None, **kw):
        calls.append({"url": url, "timeout": timeout})
        return _fake_get.response

    _fake_get.response = _Resp(200, {"status": True})
    monkeypatch.setattr("main.requests.get", _fake_get)
    return calls, _fake_get


class TestTheRouteAnswersInsideTheClientsBudget:
    def test_the_node_budget_is_under_the_timeout_this_client_allows(
        self, session_probe
    ):
        """The whole point. Read from both sides rather than asserted as two
        literals: the client's timeout comes off the real request, the budget
        off the patch that ships. If either moves past the other, the route
        stops answering in time and the tally below stops being reachable."""
        calls, _ = session_probe
        _Stub().check_whatsapp_reachable()

        assert len(calls) == 1
        assert calls[0]["url"].endswith("/check-connection-session")
        assert _node_budget_seconds() < calls[0]["timeout"]

    def test_the_route_never_awaits_is_connected_unbounded(self):
        """`await req.client.isConnected()` is what 2.3.2 turned into a 30 s
        (or unbounded) call. It must go through the shared bounded probe, the
        same one statusConnection.ts uses."""
        assert "await req.client.isConnected()" not in CHECK_CONNECTION_ROUTE
        assert "await probeIsConnected(" in CHECK_CONNECTION_ROUTE
        assert "CONNECTION_PROBE_BUDGET_MS" in CHECK_CONNECTION_ROUTE
        assert "probeIsConnected" in SESSION_CONTROLLER.split(
            "export async function checkConnectionSession("
        )[0], "the probe has to be imported, not redefined here"

    def test_an_unanswered_probe_is_reported_as_disconnected(self):
        """Only an explicit `true` is Connected. undefined (budget spent) and
        false (the page answering) both leave as `status: false` — this route
        is not behind statusConnection, so there is no probe_timeout shade to
        draw here, and reporting the timeout as Connected would restore the
        exact bug: a permanently stuck page that nothing can ever declare
        dead."""
        assert "if (connected === true) {" in CHECK_CONNECTION_ROUTE
        connected = CHECK_CONNECTION_ROUTE.index("if (connected === true) {")
        disconnected = CHECK_CONNECTION_ROUTE.index(
            "res.status(200).json({ status: false, message: 'Disconnected' });"
        )
        assert connected < disconnected


class TestTheAnswerReachesTheStrikeTally:
    def test_one_negative_answer_counts_a_strike_without_going_offline(
        self, session_probe
    ):
        """A WhatsApp Web reload answers exactly this for ~28 s and is not an
        outage, so the first negative only arms the tally."""
        calls, fake = session_probe
        fake.response = _Resp(200, {"status": False})
        stub = _Stub(connected=True)

        assert stub.check_whatsapp_reachable() is True
        assert stub._offline_probe_strikes == 1
        assert stub.host_probes == 0

    def test_two_consecutive_negative_answers_take_the_app_offline(
        self, session_probe
    ):
        """The verdict the recovery ladder is gated on: False is what lets
        check_wa_connection_http() nudge the socket stream and, after
        _DEAD_BROWSER_RESTART_STRIKES, restart the session. ~8 s of budget plus
        two 30 s ticks — versus never, with the route unbounded."""
        calls, fake = session_probe
        fake.response = _Resp(200, {"status": False})
        stub = _Stub(connected=True)

        assert stub.check_whatsapp_reachable() is True
        assert stub.check_whatsapp_reachable() is False

    def test_a_client_side_timeout_counts_nothing_at_all(self, monkeypatch):
        """The failure mode the budget exists to prevent, spelled out: when the
        route outlives the client, the request raises here, no strike is
        counted, and the host probe — which knows nothing about the browser —
        answers True for a machine that merely has internet. The app stays
        "connected" over a page that will never send anything again."""
        def _timeout(*a, **kw):
            raise TimeoutError("read timed out")

        monkeypatch.setattr("main.requests.get", _timeout)
        stub = _Stub(connected=True, host_reachable=True)

        assert stub.check_whatsapp_reachable() is True
        assert stub._offline_probe_strikes == 0
        assert stub.host_probes == 1
