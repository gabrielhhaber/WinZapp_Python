"""Missing metadata is unknown; legacy local stars require explicit migration."""
import pytest
from core.message_stars import apply_remote_star, apply_star_fields, carry_over_stars, is_same_message, read_star_matches, confirmed_star_state, is_local_star, merge_star_state, remote_star_state, stamp_star_snapshot
from core.websocket_client import WebSocketClient


@pytest.mark.parametrize("raw", [{}, {"star": None}, {"star": 1}, {"star": "false"}])
def test_missing_or_non_boolean_star_is_not_an_unstar(raw):
    assert remote_star_state(raw) == {}
    assert merge_star_state(raw, {"starred": True})["starred"] is True


@pytest.mark.parametrize("star", [False, True])
def test_legacy_star_survives_remote_observation(star):
    merged = merge_star_state(remote_star_state({"star": star}, 10), {"starred": True})
    assert merged["starred"] is True and is_local_star(merged)
    assert merged["_star_remote"] is star


def test_explicit_confirmation_consumes_legacy_marker_in_both_directions():
    for value in [False, True]:
        merged = merge_star_state(confirmed_star_state(value), {"starred": True})
        assert merged["starred"] is value and not is_local_star(merged)


def test_phone_unstar_clears_server_managed_star():
    stored = {**remote_star_state({"star": True}, 10), "_star_local": False}
    assert merge_star_state(remote_star_state({"star": False}, 20), stored)["starred"] is False


def test_late_snapshot_keeps_newer_confirmation_and_incoming_content():
    stored = {**remote_star_state({"star": True}, 20), "_star_local": False}
    incoming = {**remote_star_state({"star": False}, 10), "message": {"conversation": "new text"}}
    merged = merge_star_state(incoming, stored)
    assert merged["starred"] is True and merged["message"] == incoming["message"]


def test_duplicate_remote_observation_updates_only_star_fields():
    existing = {"key": {"id": "M"}, "message": {"conversation": "keep"}, "starred": False}
    incoming = {**remote_star_state({"star": True}, 10), "message": {"conversation": "ignore"}}
    assert apply_remote_star(existing, incoming)
    assert existing["message"]["conversation"] == "keep"
    assert not apply_remote_star(existing, incoming) and not apply_remote_star(existing, {})


def test_sync_preserves_legacy_stars_by_id():
    incoming = [{"key": {"id": "M"}, **remote_star_state({"star": False}, 10)}]
    carry_over_stars(incoming, [{"key": {"id": "M"}, "starred": True}])
    assert incoming[0]["starred"] is True


def test_read_started_before_action_cannot_undo_it_even_if_normalized_later():
    incoming = [{"key": {"id": "M"}, **remote_star_state({"star": False}, 30)}]
    stamp_star_snapshot(incoming, 10)
    carry_over_stars(incoming, [{"key": {"id": "M"}, **remote_star_state({"star": True}, 20), "_star_local": False}])
    assert incoming[0]["starred"] is True


def test_identical_observation_advances_timestamp_without_requesting_repaint():
    existing = {**remote_star_state({"star": True}, 10), "_star_local": False}
    assert not apply_remote_star(existing, remote_star_state({"star": True}, 20))
    assert existing["_star_observed_at"] == 20


@pytest.mark.parametrize("star", [False, True, None])
def test_normalizer_retains_whatsapp_star_metadata(star):
    normalizer = type("Normalizer", (), {
        "_normalize_wpp_message": WebSocketClient._normalize_wpp_message,
        "_clean_jid": WebSocketClient._clean_jid,
    })()
    raw = {"id": "false_test@c.us_M", "from": "test@c.us", "to": "me@c.us", "type": "chat", "body": "synthetic", "t": 123, "star": star}
    normalized = normalizer._normalize_wpp_message(raw)
    if star is None:
        assert "_star_remote" not in normalized
    else:
        assert normalized["starred"] is star and normalized["_star_remote"] is star


def test_star_fields_are_applied_to_the_same_objects():
    pending = {"key": {"id": "uuid"}, "_local_pending": True}
    stored = {"key": {"id": "old"}, "starred": False}
    merged = [{**pending}, {**stored, "starred": True, "_star_local": True}]
    assert apply_star_fields([pending, stored], merged) == [pending, stored]
    assert stored["starred"] is True and stored["_star_local"] is True
    assert pending == {"key": {"id": "uuid"}, "_local_pending": True}


@pytest.mark.parametrize("raw_id", ["false_device@lid_M", "false_123@c.us_M", "false_123@s.whatsapp.net_M",
    {"fromMe": False, "remote": {"_serialized": "123@c.us"}, "id": "M"}])
def test_read_naming_our_chat_by_lid_or_phone_is_the_same_message(raw_id):
    aliases = {"123@s.whatsapp.net", "device@lid"}
    assert is_same_message(raw_id, "false_device@lid_M", aliases)


@pytest.mark.parametrize("raw_id", ["false_other@lid_M", "true_device@lid_M", "false_device@lid_OTHER",
    "", None, "device@lid", {"fromMe": False, "remote": "device@lid"}])
def test_other_chat_direction_or_id_is_not_our_message(raw_id):
    assert not is_same_message(raw_id, "false_device@lid_M", {"device@lid", "123@s.whatsapp.net"})


@pytest.mark.parametrize("raw,star,ok", [({"star": True}, True, True), ({"star": False}, False, True),
    ({}, False, True), ({}, True, False), ({"star": None}, False, False), ({"star": True}, False, False)])
def test_missing_star_in_the_verification_read_satisfies_only_an_unstar(raw, star, ok):
    assert read_star_matches(raw, star) is ok
