"""A send WhatsApp already accepted must never be retried.

accepted_message_id() rejects the false-success bodies WPPConnect returns after
a WA-JS change — a 201 carrying no message id, a negative ACK, an embedded
error. The rejection happens *after* the message is on the network, so the one
thing it must never do is look like a transient failure: MessageQueue retries a
retryable failure up to four times, on top of the quote-less and legacy-@c.us
retries the send helpers try first, and every one of those is a real duplicate
delivered to the recipient. That is why the Node side reports the same verdict
inside its ordinary 201 body (messageController.ts's auditSendResult) instead
of throwing it into returnError, whose 500 main.py classifies as retryable.

So this file covers both halves: the pure function, and the return value the
send_* wrappers hand to MessageQueue.
"""

import pytest

from core.send_contract import SendContractError, accepted_message_id
from main import MainWindow


def test_accepts_a_queued_message_with_an_id():
    body = {
        "status": "success",
        "response": [{"id": {"_serialized": "true_chat@c.us_ABC"}, "ack": 0}],
    }
    assert accepted_message_id(body) == "ABC"


@pytest.mark.parametrize(
    "result",
    [None, {}, {"ack": -1, "id": "ABC"}, {"error": "broken", "id": "ABC"}],
)
def test_rejects_false_success_results(result):
    with pytest.raises(SendContractError):
        accepted_message_id({"status": "success", "response": [result]})


def test_rejects_an_internal_send_result_failure():
    with pytest.raises(SendContractError):
        accepted_message_id({
            "status": "success",
            "response": [{
                "id": "ABC",
                "sendMsgResult": {"messageSendResult": "ERROR_UNKNOWN"},
            }],
        })


def test_rejects_http_success_without_api_success_status():
    with pytest.raises(SendContractError):
        accepted_message_id({"status": "error", "message": "failed"})


def test_a_negative_ack_is_reported_as_a_refusal_not_a_missing_id():
    """send_media_attachment() picks its user-facing string off `reason`, not
    off the English message — matching the text was how "ack=" became a
    substring test."""
    with pytest.raises(SendContractError) as excinfo:
        accepted_message_id({"status": "success", "response": [{"id": "A", "ack": -1}]})
    assert excinfo.value.reason == "rejected"


def test_the_annotated_body_the_node_side_really_sends_is_still_a_refusal():
    """The bare {"id", "ack": -1} above is not a shape production produces.

    describeSendRejection() (messageController.ts) tests the embedded error
    *before* the ACK and writes "send-file was rejected (ack=-1)" into the
    result, so every refusal reaching Python carries both halves. Reading the
    error first here made reason="rejected" unreachable in production: a user
    sending a file WhatsApp refuses was told "WhatsApp did not confirm this
    send — check the conversation before sending again", which sends a blind
    user off to inspect a conversation that, by construction, has nothing in
    it, instead of "the file appears to be corrupted or in a format WhatsApp
    cannot process".
    """
    body = {
        "status": "success",
        "response": [
            {"id": "A", "ack": -1, "error": "send-file was rejected (ack=-1)"}
        ],
    }
    with pytest.raises(SendContractError) as excinfo:
        accepted_message_id(body)
    assert excinfo.value.reason == "rejected"


def test_an_embedded_error_without_a_negative_ack_stays_unconfirmed():
    """The other body auditSendResult() produces — a 201 with no message id.
    Nothing says WhatsApp examined and refused anything, so the user is told
    to check the conversation, which is the right advice there."""
    body = {
        "status": "success",
        "response": [
            {"ack": 0, "error": "send-message returned success without a message id"}
        ],
    }
    with pytest.raises(SendContractError) as excinfo:
        accepted_message_id(body)
    assert excinfo.value.reason == "unconfirmed"


def test_the_accepted_send_results_are_the_ones_the_node_side_accepts():
    """Two halves of one contract: describeSendRejection() allows exactly
    SUCCESS and OK, so anything else must not pass here either."""
    for accepted in ("SUCCESS", "OK", "ok"):
        body = {
            "status": "success",
            "response": [
                {"id": "A", "sendMsgResult": {"messageSendResult": accepted}}
            ],
        }
        assert accepted_message_id(body) == "A"

    for refused in ("0", "ERROR_UNKNOWN"):
        body = {
            "status": "success",
            "response": [
                {"id": "A", "sendMsgResult": {"messageSendResult": refused}}
            ],
        }
        with pytest.raises(SendContractError):
            accepted_message_id(body)


def test_an_unconfirmable_body_is_not_reported_as_a_refusal():
    with pytest.raises(SendContractError) as excinfo:
        accepted_message_id({"status": "success", "response": [{"ack": 0}]})
    assert excinfo.value.reason == "unconfirmed"


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class _I18n:
    def t(self, key):
        return f"<{key}>"


class _SendStub:
    """Minimal stand-in for MainWindow for the send_* wrappers.

    MainWindow is a wx.Frame, so the method is exercised as a plain function
    against the handful of attributes it actually touches.
    """

    def __init__(self):
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self.token = "tok"
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.i18n = _I18n()
        self.connection_calls = []

    def _set_wa_connected(self, connected, reason="", **kwargs):
        self.connection_calls.append((connected, reason))

    _resolve_jid_for_chat_state = MainWindow._resolve_jid_for_chat_state
    _resolve_jid_for_send = MainWindow._resolve_jid_for_send
    _legacy_phone_for_send = MainWindow._legacy_phone_for_send
    _build_link_preview_options = staticmethod(MainWindow._build_link_preview_options)
    _classify_send_exception = MainWindow._classify_send_exception
    send_text_message = MainWindow.send_text_message


class TestSendTextMessageNeverAsksForARetry:
    """The regression this file exists for: a 201 the contract rejects has to
    come back as a permanent failure, because the message is already sent."""

    @pytest.mark.parametrize(
        "body",
        [
            {"status": "success", "response": [{"ack": 0}]},          # no id
            {"status": "success", "response": [{"id": "A", "ack": -1}]},
            {"status": "success", "response": []},
            {"status": "success", "response": [{"error": "boom", "id": "A"}]},
            # What auditSendResult() puts in the body instead of throwing.
            {
                "status": "success",
                "response": [
                    {"ack": 0, "error": "send-message returned success without a message id"}
                ],
            },
        ],
    )
    def test_a_contract_violation_is_permanent(self, monkeypatch, body):
        import main

        monkeypatch.setattr(
            main, "api_post", lambda *a, **k: _Response(201, body)
        )
        stub = _SendStub()

        result = stub.send_text_message("551199990001@s.whatsapp.net", "oi")

        assert isinstance(result, dict)
        assert result["ok"] is False
        assert result["retry"] is False

    def test_the_reported_error_is_translated_not_raw_english(self, monkeypatch):
        """It reaches msg.last_error and, for media, a wx.MessageBox."""
        import main

        monkeypatch.setattr(
            main,
            "api_post",
            lambda *a, **k: _Response(201, {"status": "success", "response": [{"ack": 0}]}),
        )
        stub = _SendStub()

        result = stub.send_text_message("551199990001@s.whatsapp.net", "oi")

        assert result["error"] == "<send_not_confirmed_error>"

    def test_a_confirmed_send_still_returns_the_clean_id(self, monkeypatch):
        import main

        monkeypatch.setattr(
            main,
            "api_post",
            lambda *a, **k: _Response(
                201,
                {
                    "status": "success",
                    "response": [{"id": {"_serialized": "true_chat@c.us_ABC"}, "ack": 0}],
                },
            ),
        )
        stub = _SendStub()

        assert stub.send_text_message("551199990001@s.whatsapp.net", "oi") == "ABC"
