"""A 5xx from a send endpoint must never be answered by sending again.

Reported as "I reply to a message, it shows up correctly, and then it is
duplicated". Measured on a real install on 2026-09-09, and the ordering is the
whole diagnosis:

    16:17:04.744  echo  id=3EB0B499A4020A9246C939  type=extendedTextMessage
    16:17:04.754  POST /send-reply -> 500
    16:17:04.754  [send_text_message] Quoted send failed (HTTP 500). Retrying
                  without quote...
    16:17:04.819  echo  id=3EB04F1F7AD701833157FC  type=conversation
    16:17:04.992  POST /send-message -> 201

The echo of the delivered reply arrived **ten milliseconds before** the error
response for the request that sent it. Two WhatsApp message ids came out of one
user action, and the second one is missing the quote — which is what makes the
duplicate read as the same message sent twice.

A 5xx is `returnError` on the Node side: an exception thrown somewhere inside
the controller, by which point WPPConnect has usually already handed the
message to WhatsApp Web. So it proves nothing about delivery, and the codebase
already has the right answer for that class of outcome —
_classify_send_exception()'s `retry: False, ambiguous: True`, written after
users saw 30+ copies arrive at once when connectivity returned. This is the
same failure reached through a status code instead of an exception.

Three decision points had to change together, because fixing fewer just moves
the duplicate: the two in-function fallbacks (legacy @lid address, quote
stripping) and the verdict handed back to MessageQueue, which would otherwise
resend the whole thing itself.

MainWindow is a wx.Frame, so send_text_message() is bound onto a stub carrying
only what it touches — the pattern tests/test_probe_timeout_is_not_offline.py
uses for the same method.
"""

import json
import types

import pytest

import main
from main import MainWindow
from core.send_contract import send_failure_is_ambiguous


JID = "5511999999999@s.whatsapp.net"
GROUP = "120363426331215016@g.us"
QUOTED = {"key": {"id": "3EB0AAA", "remoteJid": GROUP, "fromMe": False}}


class _Response:
    def __init__(self, status_code, payload=None):
        self._payload = payload if payload is not None else {
            "status": "error", "message": "boom",
        }
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class _Stub:
    send_text_message = MainWindow.send_text_message
    _check_wa_connection_closed = MainWindow._check_wa_connection_closed
    _serialize_quoted_id = MainWindow._serialize_quoted_id
    _serialize_msg_id = MainWindow._serialize_msg_id
    _is_self_jid = MainWindow._is_self_jid

    def __init__(self):
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "tok"
        self.my_jid = "5500000000000@s.whatsapp.net"
        self.my_lid = ""
        self.connection_flags = []
        # The quote-stripped fallback announces "reply_quote_lost" on its way
        # out, so the success path needs a translator to reach.
        self.i18n = types.SimpleNamespace(t=lambda key: key)

    def _resolve_jid_for_send(self, jid):
        return jid

    def _build_link_preview_options(self, link_preview):
        return {}

    def _legacy_phone_for_send(self, jid):
        return jid.replace("@lid", "@c.us")

    # Bound from the real class: the outer `except` funnels through it, and a
    # stub that swallowed exceptions would turn a genuine break into a pass.
    _classify_send_exception = MainWindow._classify_send_exception

    def output(self, text, interrupt=False):
        pass

    def _set_wa_connected(self, connected, reason="", **kwargs):
        self.connection_flags.append(bool(connected))

    def check_wa_connection_http(self):
        pass


def _answer(monkeypatch, *responses):
    """Return each response in turn; record every request that was made."""
    calls = []
    queue = list(responses)

    def _fake_api_post(url, **kwargs):
        calls.append(url)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(main, "api_post", _fake_api_post)
    return calls


class TestTheClassifier:
    @pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
    def test_a_server_error_is_ambiguous(self, status):
        assert send_failure_is_ambiguous(status) is True

    @pytest.mark.parametrize("status", [200, 201, 400, 404, 408, 429, 499])
    def test_a_client_error_is_not(self, status):
        """4xx means the controller rejected the request before doing anything
        with it — a bad phone, a quoted message it cannot find — so a fallback
        there is free."""
        assert send_failure_is_ambiguous(status) is False

    @pytest.mark.parametrize("status", [None, "", "nonsense", object()])
    def test_an_unreadable_status_is_ambiguous(self, status):
        """Being wrong this way costs a message the user sends again; being
        wrong the other way costs a duplicate nobody can take back."""
        assert send_failure_is_ambiguous(status) is True


class TestAQuotedSendThatFailsAmbiguously:
    def test_the_quote_is_not_stripped_and_resent(self, monkeypatch):
        """The reported duplicate, end to end."""
        calls = _answer(monkeypatch, _Response(500))
        stub = _Stub()

        result = stub.send_text_message(GROUP, "oi", quoted=QUOTED)

        assert len(calls) == 1, (
            f"a second send went out after an ambiguous failure: {calls}"
        )
        assert calls[0].endswith("/send-reply")
        assert result["ok"] is False
        assert result["ambiguous"] is True
        assert result["retry"] is False, (
            "handed back as retryable, so MessageQueue resends it instead — "
            "the same duplicate, one layer down"
        )

    def test_a_definite_rejection_still_retries_without_the_quote(
            self, monkeypatch, wx_app):
        """The fallback keeps working where it was meant to: a 4xx is the
        controller refusing the request, most often because it cannot find the
        quoted message.

        Needs the shared wx.App: this is the only branch that reaches
        wx.CallAfter, to announce that the quote was lost."""
        calls = _answer(monkeypatch, _Response(400), _Response(201, {
            "status": "success", "response": {"id": "3EB0NEW"},
        }))
        stub = _Stub()

        result = stub.send_text_message(GROUP, "oi", quoted=QUOTED)

        assert [c.rsplit("/", 1)[-1] for c in calls] == ["send-reply", "send-message"]
        assert result["ok"] is True
        assert result["quote_lost"] is True


class TestAPlainSendThatFailsAmbiguously:
    def test_it_is_not_handed_back_as_retryable(self, monkeypatch):
        """No quote involved, so only the third decision point applies — and on
        its own it is what stops MessageQueue resending."""
        calls = _answer(monkeypatch, _Response(500))
        stub = _Stub()

        result = stub.send_text_message(JID, "oi")

        assert len(calls) == 1
        assert result["ambiguous"] is True
        assert result["retry"] is False

    @pytest.mark.parametrize("status", [408, 429])
    def test_a_transient_client_error_is_still_retryable(self, monkeypatch, status):
        """Nothing reached WhatsApp, so resending cannot duplicate anything."""
        _answer(monkeypatch, _Response(status))
        stub = _Stub()

        result = stub.send_text_message(JID, "oi")

        assert result["retry"] is True
        assert result.get("ambiguous") is not True


class TestTheLidFallback:
    def test_an_ambiguous_failure_does_not_try_the_legacy_address(
            self, monkeypatch):
        """"any definite 4xx/5xx is worth one legacy attempt" — the word that
        was not being honoured is *definite*. A second attempt on a 5xx is a
        second message."""
        calls = _answer(monkeypatch, _Response(500))
        stub = _Stub()

        result = stub.send_text_message("123456@lid", "oi")

        assert len(calls) == 1, f"a legacy retry went out anyway: {calls}"
        assert result["ambiguous"] is True

    def test_a_definite_refusal_still_falls_back(self, monkeypatch):
        calls = _answer(monkeypatch, _Response(400), _Response(201, {
            "status": "success", "response": {"id": "3EB0NEW"},
        }))
        stub = _Stub()

        result = stub.send_text_message("123456@lid", "oi")

        assert len(calls) == 2
        assert result == "3EB0NEW"
