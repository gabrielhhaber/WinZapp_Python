"""The setter echoes OLD state: verify with one read and never retry a write."""
from types import SimpleNamespace
from urllib.parse import unquote
import pytest
import requests
from main_window import message_stars as transport
from main_window.sending import SendingMixin
from urllib3.exceptions import NewConnectionError


class MW(transport.MessageStarsMixin):
    _serialize_msg_id = SendingMixin._serialize_msg_id
    _phone_to_lid = {"test@s.whatsapp.net": "device@lid"}
    wpp_server, wpp_port, token = "http://synthetic.invalid", 6300, "test"


def response(star=False, status=200, mid="false_device@lid_M", body=None):
    return SimpleNamespace(status_code=status, text="{}", json=lambda: body if body is not None else
                           {"status": "Success", "response": {"data": {"id": {"_serialized": mid}, "star": star}}})


@pytest.fixture
def calls(monkeypatch):
    writes, reads = [], []
    monkeypatch.setattr(transport, "api_post", lambda *a, **kw: writes.append((a, kw)) or response(False))
    monkeypatch.setattr(transport, "api_get", lambda *a, **kw: reads.append((a, kw)) or response(True))
    return writes, reads


def test_old_state_echo_is_ignored_and_exact_lid_message_is_read(calls):
    assert MW().star_message("test@s.whatsapp.net", {"id": "M"}, True) == "confirmed"
    writes, reads = calls
    assert len(writes) == len(reads) == 1
    assert writes[0][1]["json"] == {"messageId": "false_device@lid_M", "star": True}
    assert unquote(reads[0][0][0]).endswith("/message-by-id/false_device@lid_M")
    assert writes[0][1]["timeout"] == reads[0][1]["timeout"] == 15


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429])
def test_definite_refusal_makes_no_verification_read(calls, monkeypatch, status):
    monkeypatch.setattr(transport, "api_post", lambda *a, **kw: response(status=status))
    assert MW().star_message("test@s.whatsapp.net", {"id": "M"}, True) == "refused"
    assert not calls[1]


@pytest.mark.parametrize("exc", [requests.ReadTimeout(), requests.ConnectionError("reset"), RuntimeError("synthetic")])
def test_uncertain_write_is_read_once_without_post_retry(calls, monkeypatch, exc):
    def write(*a, **kw):
        calls[0].append((a, kw))
        raise exc
    monkeypatch.setattr(transport, "api_post", write)
    assert MW().star_message("test@s.whatsapp.net", {"id": "M"}, True) == "confirmed"
    assert len(calls[0]) == len(calls[1]) == 1


@pytest.mark.parametrize("exc", [requests.ConnectTimeout(), requests.ConnectionError(NewConnectionError(None, "synthetic"))])
def test_proven_connection_failure_is_refused(calls, monkeypatch, exc):
    def write(*a, **kw):
        raise exc
    monkeypatch.setattr(transport, "api_post", write)
    assert MW().star_message("test@s.whatsapp.net", {"id": "M"}, True) == "refused"
    assert not calls[1]


@pytest.mark.parametrize("result", [response(False), response(True, mid="false_device@lid_OTHER"),
    response(True, mid="false_OTHER@lid_M"), response(True, mid="true_device@lid_M"),
    response(True, status=500), response(body={}),
    response(body={"response": {"id": "false_device@lid_M"}}),
    response(body={"status": "error", "response": {"id": "false_device@lid_M", "star": True}})])
def test_unverified_read_never_reports_success(calls, monkeypatch, result):
    monkeypatch.setattr(transport, "api_get", lambda *a, **kw: result)
    assert MW().star_message("test@s.whatsapp.net", {"id": "M"}, True) == "unknown"
    assert len(calls[0]) == 1


def test_ambiguous_server_failure_can_be_verified(calls, monkeypatch):
    monkeypatch.setattr(transport, "api_post", lambda *a, **kw: response(status=500))
    assert MW().star_message("test@s.whatsapp.net", {"id": "M"}, True) == "confirmed"


def test_empty_id_never_calls_transport(calls):
    assert MW().star_message("test@s.whatsapp.net", {}, True) == "refused"
    assert calls == ([], [])


def test_group_participant_and_unstar_are_preserved(calls, monkeypatch):
    monkeypatch.setattr(transport, "api_get", lambda *a, **kw: response(False, mid="false_group@g.us_M_sender@lid"))
    assert MW().star_message("group@g.us", {"id": "M", "participant": "sender@lid"}, False) == "confirmed"
    assert calls[0][0][1]["json"] == {"messageId": "false_group@g.us_M_sender@lid", "star": False}


def test_read_naming_the_chat_by_its_phone_jid_confirms(calls, monkeypatch):
    monkeypatch.setattr(transport, "api_get", lambda *a, **kw: response(True, mid="false_test@c.us_M"))
    assert MW().star_message("test@s.whatsapp.net", {"id": "M"}, True) == "confirmed"


@pytest.mark.parametrize("star,expected", [(False, "confirmed"), (True, "unknown")])
def test_message_without_star_field_confirms_only_an_unstar(calls, monkeypatch, star, expected):
    body = {"status": "Success", "response": {"data": {"id": {"_serialized": "false_device@lid_M"}}}}
    monkeypatch.setattr(transport, "api_get", lambda *a, **kw: response(body=body))
    assert MW().star_message("test@s.whatsapp.net", {"id": "M"}, star) == expected


def test_requests_use_the_session_token_like_other_actions(calls):
    MW().star_message("test@s.whatsapp.net", {"id": "M"}, True)
    assert calls[0][0][1]["headers"] == calls[1][0][1]["headers"] == {"Authorization": "Bearer test"}


def test_chat_without_a_cached_lid_is_read_back_under_its_phone_jid(calls, monkeypatch):
    monkeypatch.setattr(transport, "api_get", lambda *a, **kw: response(True, mid="false_555@c.us_M"))
    assert MW().star_message("555@s.whatsapp.net", {"id": "M"}, True) == "confirmed"
    assert not MW()._star_chat_aliases("555@s.whatsapp.net") - {"555@s.whatsapp.net", None}
