"""Tests for MainWindow._still_linked_on_server(), the host-device probe that
is the last gate before any destructive wipe.

Its answer is deliberately three-valued, and every one of the three has a
different consequence in _act_on_unlink_decision(): LINKED vetoes the wipe,
UNLINKED authorises it, UNKNOWN falls back to the pairing dialog with the
data kept. So the mapping from an HTTP answer to one of those three IS the
fix this file guards -- collapsing "the probe failed" into "not linked" is
exactly what turned a Node restarted under a rotated local token into a full
database wipe 60s later: the probe leaves through the very auth middleware
that had been answering 401, so it was refused too, and a boolean read that
as permission.

The sibling files (tests/test_local_auth_rejected_logout_gate.py,
tests/test_unlink_decision_thread_safety.py) stub this method out to drive
the decision logic around it, so nothing there ever runs the real body --
inverting the last line of it to LINKED/UNLINKED left both of them green
while handing a live, still-linked session's database to the fourth strike.
"""

import pytest

import connection_state as cs
from main import MainWindow


class _Stub:
    """Only what the probe itself touches: the URL it builds and the token it
    authenticates with."""

    _still_linked_on_server = MainWindow._still_linked_on_server
    # The sibling it delegates to, under its real name: the probe itself
    # lives there and also reports WHICH phone answered, which only
    # MainWindow._wipe_local_data_if_another_number_linked() reads.
    _host_device_link_probe = MainWindow._host_device_link_probe

    def __init__(self):
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "session-token"


class _Resp:
    def __init__(self, status_code, payload=None, *, unreadable=False):
        self.status_code = status_code
        self._payload = payload
        self._unreadable = unreadable

    def json(self):
        if self._unreadable:
            # requests raises on a body that is not JSON at all (an HTML error
            # page from a proxy, a truncated answer) -- not a return value.
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


def _probe(monkeypatch, responder):
    """Run the real probe against *responder*, which stands in for api_get."""
    monkeypatch.setattr("main.api_get", responder)
    return _Stub()._still_linked_on_server()


def _answering(resp):
    return lambda *a, **kw: resp


class TestTheProbeCouldNotReachAVerdict:
    """Everything that is not a readable answer is UNKNOWN -- never UNLINKED,
    which is the only value that may authorise a wipe."""

    def test_a_transport_failure_is_unknown(self, monkeypatch):
        def _boom(*a, **kw):
            raise OSError("connection refused")

        assert _probe(monkeypatch, _boom) == cs.LINK_PROBE_UNKNOWN

    def test_a_server_error_is_unknown(self, monkeypatch):
        assert _probe(monkeypatch, _answering(_Resp(500))) == cs.LINK_PROBE_UNKNOWN

    def test_a_local_401_is_unknown(self, monkeypatch):
        """The case the whole PR is named after: our own auth middleware
        refused the probe, so it never reached WhatsApp and says nothing
        about the link at all."""
        assert _probe(monkeypatch, _answering(_Resp(401))) == cs.LINK_PROBE_UNKNOWN

    def test_a_body_that_cannot_be_read_is_unknown(self, monkeypatch):
        resp = _Resp(200, unreadable=True)
        assert _probe(monkeypatch, _answering(resp)) == cs.LINK_PROBE_UNKNOWN

    def test_a_2xx_with_no_response_object_is_unknown(self, monkeypatch):
        """No "response" mapping at all is a shape we cannot read, so it is
        not a verdict about the link either way. Contrast with a response
        object that IS there and simply carries no phone number -- that one
        is a real unlink, see TestTheProbeAnswered below."""
        for payload in ({}, {"response": None}, {"status": "error"},
                        {"response": "nonsense"}):
            resp = _Resp(200, payload)
            assert _probe(monkeypatch, _answering(resp)) == cs.LINK_PROBE_UNKNOWN, payload


class TestTheProbeAnswered:
    def test_a_phone_number_means_still_linked(self, monkeypatch):
        resp = _Resp(200, {"response": {"phoneNumber": "1234@c.us"}})
        assert _probe(monkeypatch, _answering(resp)) == cs.LINK_PROBE_LINKED

    def test_a_serialized_phone_object_means_still_linked(self, monkeypatch):
        """WPPConnect reports phoneNumber either as a plain string or as the
        wrapped WID object -- host-device's own consumer above handles both,
        so this must too."""
        resp = _Resp(200, {"response": {"phoneNumber": {"_serialized": "1234@c.us"}}})
        assert _probe(monkeypatch, _answering(resp)) == cs.LINK_PROBE_LINKED

    def test_201_counts_as_an_answer_too(self, monkeypatch):
        resp = _Resp(201, {"response": {"phoneNumber": "1234@c.us"}})
        assert _probe(monkeypatch, _answering(resp)) == cs.LINK_PROBE_LINKED

    @pytest.mark.parametrize("empty", [None, "", {}, {"_serialized": ""}])
    def test_a_reported_but_empty_phone_number_means_unlinked(self, monkeypatch, empty):
        """The one reading that genuinely authorises a wipe: the session
        answered, and it holds no linked device."""
        resp = _Resp(200, {"response": {"phoneNumber": empty}})
        assert _probe(monkeypatch, _answering(resp)) == cs.LINK_PROBE_UNLINKED

    @pytest.mark.parametrize("body", [{}, {"id": "x", "wa_version": "2.3"}])
    def test_a_missing_phone_number_key_also_means_unlinked(self, monkeypatch, body):
        """The shape a REAL unlink actually arrives in, so it must not be
        read as an unreadable probe. deviceController's host-device sends
        `{...hostDevice, phoneNumber}` with phoneNumber = getWid(), getWid()
        is undefined once the session holds no linked user, and
        JSON.stringify drops undefined keys -- so the key is simply absent.
        Reading that as UNKNOWN made LINK_PROBE_UNLINKED unreachable against
        real traffic and left the wipe branch dead code, which in turn let an
        unlinked-then-repaired phone sync a different account's chats into
        this database."""
        resp = _Resp(200, {"response": body})
        assert _probe(monkeypatch, _answering(resp)) == cs.LINK_PROBE_UNLINKED


def test_it_asks_this_session_host_device_with_its_own_token(monkeypatch):
    """A probe pointed at the wrong session would answer about someone else's
    link -- and this one is trusted to authorise a wipe."""
    seen = {}

    def _capture(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs.get("headers") or {}
        return _Resp(200, {"response": {"phoneNumber": "1234@c.us"}})

    assert _probe(monkeypatch, _capture) == cs.LINK_PROBE_LINKED
    assert seen["url"] == "http://127.0.0.1:6300/api/session-token/host-device"
    assert seen["headers"]["Authorization"] == "Bearer session-token"
