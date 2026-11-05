"""An accepted/ended native dial must not be lost before Python registration."""
import base64
import threading

import pytest

from test_call_media import FakeCalls, media, states
from call_placement_state import PlacementStateError


def snapshot(generation=7, accepted=True, ended=False, phase=4):
    return {"generation": generation, "peerAccepted": accepted, "ended": ended, "phase": phase}


def prepared(value):
    fake = FakeCalls()
    fake.placement_state = lambda _id: value
    fake.audio_format = lambda _id: {"generation": 7, "sampleRate": 16000}
    m, sent = media(fake)
    m.ring_limit = 0
    started = []
    m._start_audio = lambda cid, claim: started.append((cid, claim))
    m.group_calls = True
    return fake, m, sent, started


@pytest.mark.parametrize("group", [False, True])
def test_early_peer_accept_is_recovered_for_both_placement_routes(group):
    fake, m, sent, started = prepared(snapshot())
    def dial(*args, **kwargs):
        m.handle({"ev": "accepted", "call": "EARLY", "generation": 7})
        return {"call": "EARLY", "generation": 7}
    fake.dial = fake.group_dial = dial
    assert (m.group_dial("1203@g.us") if group else m.dial("5511999990000@c.us")) == "EARLY"
    assert m.status("EARLY")["state"] == "ACTIVE"
    assert states(sent).count("ACTIVE") == 1 and len(started) == 1
    m.handle({"ev": "ready", "call": "EARLY", "generation": 7})
    m.handle({"ev": "accepted", "call": "EARLY", "generation": 7})
    assert len(started) == 1 and states(sent).count("ACTIVE") == 1


@pytest.mark.parametrize("value", [None, snapshot(ended=True)])
@pytest.mark.parametrize("group", [False, True])
def test_a_call_ended_before_registration_is_never_resurrected(value, group):
    fake, m, sent, started = prepared(value)
    fake.dial = fake.group_dial = lambda *a, **kw: {"call": "OVER", "generation": 7}
    m.group_dial("1203@g.us") if group else m.dial("5511999990000@c.us")
    assert not m.in_call("OVER") and m.live_call() == "" and started == []
    assert "ACTIVE" not in states(sent)
    assert m._ring_timers == {}


def test_relay_active_without_peer_accept_does_not_enable_microphone():
    fake, m, sent, started = prepared(snapshot(accepted=False))
    fake.dial = lambda *a: {"call": "RING", "generation": 7}
    m.dial("5511999990000@c.us")
    m.microphone({"pcm": base64.b64encode(b"\x01\x00" * 2880).decode(), "sampleRate": 48000})
    assert m.status("RING")["state"] == "CONNECTING"
    assert fake.written == [] and started == []


@pytest.mark.parametrize("bad", [snapshot(generation=8), {}, [],
    snapshot(accepted=1), snapshot(ended=0), snapshot(phase=True), snapshot(phase=100)])
def test_bad_or_replaced_snapshot_cannot_authorize_a_call(bad):
    fake, m, sent, started = prepared(bad)
    fake.dial = lambda *a: {"call": "BAD", "generation": 7}
    with pytest.raises(PlacementStateError):
        m.dial("5511999990000@c.us")
    assert started == [] and m.live_call() == ""


def test_legacy_native_results_keep_the_existing_event_driven_path():
    fake, m, sent, started = prepared(snapshot())
    fake.dial = lambda *a: {"call": "OLD"}
    fake.placement_state = lambda _id: (_ for _ in ()).throw(AssertionError("legacy must not poll"))
    m.dial("5511999990000@c.us")
    assert m.status("OLD")["state"] == "CALLING" and started == []
    m.handle({"ev": "accepted", "call": "OLD"})
    assert m.status("OLD")["state"] == "ACTIVE" and len(started) == 1


def test_an_unrelated_existing_call_is_not_replaced_by_a_stale_snapshot():
    fake, m, sent, started = prepared(snapshot(generation=8))
    m._placed("SAME", "5511999990000@c.us", False, "")
    current = m._calls["SAME"]
    current["native_generation"] = 8
    fake.dial = lambda *a: {"call": "SAME", "generation": 7}
    with pytest.raises(PlacementStateError):
        m.dial("5511999990000@c.us")
    assert m._calls["SAME"] is current
    assert fake.hung == []


def test_dial_wait_does_not_hold_the_lifecycle_gate():
    fake, m, sent, started = prepared(snapshot())
    entered, release, handled = threading.Event(), threading.Event(), threading.Event()
    errors = []
    def dial(*args):
        entered.set()
        if not release.wait(2):
            raise AssertionError("test did not release its native fake")
        return {"call": "LATE", "generation": 7}
    fake.dial = dial
    def worker():
        try:
            m.dial("5511999990000@c.us")
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=worker)
    thread.start()
    other = None
    try:
        assert entered.wait(1)
        other = threading.Thread(target=lambda: (m.handle({"ev": "end", "call": "UNRELATED"}), handled.set()))
        other.start()
        assert handled.wait(1), "dial blocked the shared lifecycle gate"
    finally:
        release.set()
        thread.join(2)
        if other is not None:
            other.join(2)
    assert not thread.is_alive() and (other is None or not other.is_alive())
    assert errors == []


def test_a_real_separate_event_thread_can_deliver_accept_before_native_return():
    fake, m, sent, started = prepared(snapshot())
    def dial(*args):
        event = threading.Thread(target=m.handle, args=({"ev": "accepted", "call": "EARLY", "generation": 7},))
        event.start()
        event.join(1)
        assert not event.is_alive(), "native wait held the event gate"
        return {"call": "EARLY", "generation": 7}
    fake.dial = dial
    m.dial("5511999990000@c.us")
    assert m.status("EARLY")["state"] == "ACTIVE" and len(started) == 1


@pytest.mark.parametrize("generation", [True, 0, -1, 2**64, "7"])
def test_placement_result_identity_must_be_a_real_positive_uint64(generation):
    fake, m, sent, started = prepared(snapshot())
    fake.dial = lambda *a: {"call": "BAD", "generation": generation}
    with pytest.raises(PlacementStateError):
        m.dial("5511999990000@c.us")
    assert m.live_call() == "" and started == []


def test_new_generation_releases_old_reader_claim_before_starting_its_reader():
    fake, m, sent, started = prepared(snapshot())
    m._placed("SAME", "5511999990000@c.us", False, "")
    old = m._calls["SAME"]
    old["native_generation"] = 6
    old_claim = (object(), old)
    m._audio_readers["SAME"] = old_claim
    fake.dial = lambda *a: {"call": "SAME", "generation": 7}
    m.dial("5511999990000@c.us")
    assert len(started) == 1 and m._audio_readers["SAME"] is not old_claim
    assert m._audio_readers["SAME"][1] is m._calls["SAME"]


def test_an_ended_attempt_never_announces_end_for_a_newer_same_id_record():
    fake, m, sent, started = prepared(None)
    m._placed("SAME", "5511999990000@c.us", False, "")
    newer = m._calls["SAME"]
    newer["native_generation"] = 8
    sent.clear()
    fake.dial = lambda *a: {"call": "SAME", "generation": 7}
    with pytest.raises(PlacementStateError):
        m.dial("5511999990000@c.us")
    assert m._calls["SAME"] is newer and sent == [] and fake.hung == []


def test_replacement_cancels_the_old_attempts_ring_timer():
    from types import SimpleNamespace
    fake, m, sent, started = prepared(snapshot())
    cancelled = []
    m._ring_timers["SAME"] = SimpleNamespace(cancel=lambda: cancelled.append(True))
    fake.dial = lambda *a: {"call": "SAME", "generation": 7}
    m.dial("5511999990000@c.us")
    assert cancelled == [True] and m._ring_timers == {}


def test_an_already_queued_old_timeout_cannot_remove_a_new_attempts_timer():
    fake, m, sent, started = prepared(snapshot())
    m._placed("SAME", "5511999990000@c.us", False, "")
    old = m._calls["SAME"]
    m._placed("SAME", "5511999990000@c.us", False, "")
    newer_timer = object()
    m._ring_timers["SAME"] = newer_timer
    m._ring_expired("SAME", old)
    assert m._ring_timers["SAME"] is newer_timer and fake.hung == []
