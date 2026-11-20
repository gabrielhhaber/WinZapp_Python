"""Direct tests of core/incremental_send.py, the send side of a recording's
incremental noise-reduction session (wx-free). The full send path through
the panel is covered by tests/test_incremental_wiring.py."""

import threading

from core import incremental_send as isend


class _Controller:
    def __init__(self):
        self.released = []
        self.forgotten = []

    def release_slot(self, local_id):
        self.released.append(local_id)

    def forget(self, local_id):
        self.forgotten.append(local_id)


class _Worker:
    def __init__(self):
        self.aborted = 0

    def abort(self):
        self.aborted += 1


def _session(**extra):
    session = {"local_id": "n1", "frames": [], "rate": 48000, "ch": 1,
               "worker_ch": 1, "worker": None, "thread": None,
               "stop": False, "cancel": False, "failed": False,
               "revoked": False, "stopped_event": threading.Event(),
               "device_failed": False, "basic_only": False}
    session.update(extra)
    return session


def test_reclaim_drain_remnant_aborts_deletes_and_frees_once(tmp_path):
    spool = tmp_path / "s.pcm"
    spool.write_bytes(b"x")
    worker = _Worker()
    session = _session(worker=worker, spool=str(spool))
    controller = _Controller()

    isend._reclaim_drain_remnant(session, controller, "n1")
    isend._reclaim_drain_remnant(session, controller, "n1")

    assert worker.aborted == 1
    assert session["worker"] is None
    assert not spool.exists()
    assert controller.released == ["n1"]


def test_reclaim_drain_remnant_without_worker_touches_nothing(tmp_path):
    spool = tmp_path / "s.pcm"
    spool.write_bytes(b"x")
    controller = _Controller()

    isend._reclaim_drain_remnant(_session(spool=str(spool)), controller, "n1")

    assert spool.exists()
    assert controller.released == []


def test_discard_finished_result_removes_spool_and_result(tmp_path):
    spool = tmp_path / "s.pcm"
    result = tmp_path / "r.pcm"
    spool.write_bytes(b"x")
    result.write_bytes(b"y")
    worker = _Worker()
    controller = _Controller()

    isend._discard_finished_result(
        worker, _session(spool=str(spool)),
        {"result": (str(result), "neural")}, controller, "n1")

    assert worker.aborted == 1
    assert not spool.exists() and not result.exists()
    assert controller.released == ["n1"]


def test_revoke_while_waiting_for_the_device_close_stops_the_send():
    controller = _Controller()
    session = _session(revoked=True)   # stopped_event never set

    outcome = isend.consume_incremental_session(
        session, settings={}, controller=controller, local_id="n1",
        frames=[], actual_ch=1, channel_mode="auto", mixed_audio=False, t0=0.0)

    assert outcome is None
    assert controller.forgotten == ["n1"]


def test_basic_only_session_leaves_the_audio_to_the_offline_path():
    controller = _Controller()
    session = _session(basic_only=True)
    session["stopped_event"].set()

    outcome = isend.consume_incremental_session(
        session, settings={"general": {"noise_reduction_enabled": True}},
        controller=controller, local_id="n1", frames=[b"\0\0"], actual_ch=1,
        channel_mode="auto", mixed_audio=False, t0=0.0)

    assert outcome == isend.IncrementalSendOutcome(None, None, False, None)
    assert session["cancel"] is True
    assert controller.forgotten == []
