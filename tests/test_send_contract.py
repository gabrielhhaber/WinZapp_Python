import pytest

from core.send_contract import SendContractError, accepted_message_id


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
