"""The bridge's synthetic onack ack=1 never lands on top of a real ack.

Every send is followed by a deferred `onack {"ack": 1}` 600 ms later (WPPConnect
says "sent" right after the send; the app only registers the message after the
HTTP response). In a measured case the real DELIVERED beat the delay and the
row still flipped back to "enviada" 280 ms later, because a stale "sent" stage
was written after the delivery verdict. The bridge now records every
delivered/read/played ack it emits and skips the synthetic ack=1 for a message
id already acked for real."""

import sys
import types

import server


class _SilentSocket:
    """Stand-in for the aiohttp loop/sio pair; the session never emits."""

    def __init__(self):
        self.emitted = []

    def emit(self, *args, **kwargs):
        self.emitted.append((args, kwargs))


def _session(tmp_path):
    s = server.Session("account1", str(tmp_path / "account1"), _SilentSocket(), None)
    s.emitted = []
    s.emit = lambda event, data: s.emitted.append((event, data))
    return s


def test_recording_a_real_ack_marks_the_id_known(tmp_path):
    s = _session(tmp_path)
    assert s._real_ack_emitted("ID1") is False
    s._record_ack("ID1", 2)
    assert s._real_ack_emitted("ID1") is True
    assert s._acked_ids["ID1"] == 2


def test_record_ack_is_bounded(tmp_path):
    s = _session(tmp_path)
    for i in range(600):
        s._record_ack(f"ID{i}", 2)
    assert len(s._acked_ids) <= 512
    # the newest ids are kept
    assert "ID599" in s._acked_ids
    assert "ID0" not in s._acked_ids


def test_unrecorded_ids_are_unaffected_by_the_bound(tmp_path):
    s = _session(tmp_path)
    for i in range(600):
        s._record_ack(f"ID{i}", 2)
    assert s._real_ack_emitted("never-seen") is False


def test_sent_ack_payload_is_the_wppconnect_sent_ack(tmp_path):
    import synthetic_ack
    s = _session(tmp_path)
    assert synthetic_ack.sent_ack_payload(s, "ID1", "5511@c.us", "true_5511@c.us_ID1") == {
        "session": "account1", "ack": 1, "to": "5511@c.us", "from": "5511@c.us",
        "id": {"fromMe": True, "remote": "5511@c.us", "id": "ID1",
               "_serialized": "true_5511@c.us_ID1"}}


def test_sent_ack_payload_is_none_once_a_real_ack_went_out(tmp_path):
    import synthetic_ack
    s = _session(tmp_path)
    s._record_ack("ID1", 2)
    assert synthetic_ack.sent_ack_payload(s, "ID1", "5511@c.us", "serial") is None
    assert synthetic_ack.sent_ack_payload(s, "ID2", "5511@c.us", "serial") is not None
