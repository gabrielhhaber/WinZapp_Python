"""One request at a time for a fresh app-state copy (app_state_recovery.py).

Reported: with `regular_low` broken, every chat opened started its own 90 s
wait for the phone, on threads the sends needed too; a message was reported
as not confirmed and went out a moment later."""
import threading

import app_state_recovery as ar


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_a_second_caller_gives_up_while_one_request_runs():
    gate = ar.RecoveryGate(clock=_Clock())
    started, release, results = threading.Event(), threading.Event(), []

    def slow():
        started.set()
        release.wait(5)
        return True

    first = threading.Thread(target=lambda: results.append(gate.run("regular_low", slow)))
    first.start()
    started.wait(5)
    calls = []
    assert gate.run("regular_low", lambda: calls.append(1) or True) is False
    assert calls == []                      # it did not wait, nor ask again
    # another collection is not held back
    assert gate.run("regular_high", lambda: True) is True
    release.set()
    first.join(5)
    assert results == [True]


def test_after_an_unanswered_request_nobody_asks_again_for_a_while():
    clock = _Clock()
    gate = ar.RecoveryGate(clock=clock, cooldown=120)
    calls = []
    assert gate.run("regular_low", lambda: calls.append(1) or False) is False
    clock.now += 60
    assert gate.run("regular_low", lambda: calls.append(2) or True) is False
    clock.now += 61
    assert gate.run("regular_low", lambda: calls.append(3) or True) is True
    assert calls == [1, 3]


def test_a_request_that_raises_counts_as_unanswered():
    clock = _Clock()
    gate = ar.RecoveryGate(clock=clock, cooldown=120)

    def broken():
        raise RuntimeError("socket closed")

    try:
        gate.run("regular_low", broken)
    except RuntimeError:
        pass
    assert gate.run("regular_low", lambda: True) is False


def test_send_seen_runs_on_threads_of_its_own():
    import pathlib
    source = (pathlib.Path(__file__).parent / "server.py").read_text(encoding="utf-8")
    route = source[source.index("    async def send_seen(req):"):
                   source.index("    async def subscribe_presence(req):")]
    assert "run_in_executor(marking, s.read_and_confirm, key)" in route
    assert "run_in_executor(marking, s.mark_unread, keys[0])" in route
    assert "in_thread(s.read_and_confirm" not in route


# ── One operation at a time, and no read notice lost ─────────────────────────

def test_the_bridge_never_runs_two_app_state_operations_at_once():
    import time as _time
    inside, most = [0], [0]

    def work(*args, **kwargs):
        inside[0] += 1
        most[0] = max(most[0], inside[0])
        _time.sleep(0.02)
        inside[0] -= 1
        return args

    from types import SimpleNamespace
    client = SimpleNamespace(send_app_state=work, fetch_app_state=work)
    ar.serialize(client, threading.RLock())
    threads = [threading.Thread(target=client.send_app_state, args=(n,)) for n in range(4)]
    threads += [threading.Thread(target=client.fetch_app_state, args=("regular_low",))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert most[0] == 1
    assert client.send_app_state("x") == ("x",)


def test_a_client_without_app_state_calls_is_left_alone():
    from types import SimpleNamespace
    client = SimpleNamespace()
    ar.serialize(client, threading.RLock())
    assert not hasattr(client, "send_app_state")


def test_pending_reads_keep_the_newest_wish_and_a_failure_never_overwrites_it():
    pending = ar.PendingReads()
    pending.put("a@lid", True)
    pending.put("a@lid", False)             # the user marked it unread afterwards
    pending.put("b@g.us", True)
    taken = pending.take()
    assert sorted(taken) == [("a@lid", False), ("b@g.us", True)] and len(pending) == 0
    pending.put("a@lid", True)              # a newer wish while the flush ran
    pending.restore("a@lid", False)         # the old one failed again
    pending.restore("b@g.us", True)
    assert sorted(pending.take()) == [("a@lid", True), ("b@g.us", True)]


class _Reader:
    """Enough of a Session for state_read and its queue."""
    from server import Session as _S
    state_read = _S.state_read
    flush_pending_reads = _S.flush_pending_reads

    def __init__(self, accepts):
        self.accepts = accepts
        self.sent = []
        self.client = object()
        self.status = "CONNECTED"
        self._pending_reads = ar.PendingReads()
        self._flushing_reads = threading.Lock()

    def _send_read_notice(self, conversation, read_flag):
        self.sent.append((conversation, read_flag))
        return self.accepts


def _wait_flush(reader):
    for _ in range(100):
        if not reader._flushing_reads.locked():
            return
        import time as _time
        _time.sleep(0.01)


def test_a_refused_read_notice_is_sent_once_the_collection_works_again():
    reader = _Reader(accepts=False)
    assert reader.state_read("a@lid", True) is False
    assert reader.state_read("b@lid", True) is False
    assert len(reader._pending_reads) == 2
    reader.accepts = True
    assert reader.state_read("c@lid", True) is True
    _wait_flush(reader)
    assert sorted(reader.sent[-2:]) == [("a@lid", True), ("b@lid", True)]
    assert len(reader._pending_reads) == 0


def test_a_flush_that_fails_again_keeps_the_rest_queued():
    reader = _Reader(accepts=False)
    for chat in ("a@lid", "b@lid", "c@lid"):
        reader.state_read(chat, True)
    reader.flush_pending_reads()
    _wait_flush(reader)
    assert len(reader._pending_reads) == 3
