import threading

from core.ordered_dispatch import OrderedDispatcher


def test_actions_run_in_ticket_order_even_when_completed_out_of_order():
    d = OrderedDispatcher()
    ran = []
    t = [d.reserve("chat") for _ in range(3)]
    d.complete("chat", t[2], lambda: ran.append(2))
    d.complete("chat", t[1], lambda: ran.append(1))
    assert ran == []
    d.complete("chat", t[0], lambda: ran.append(0))
    assert ran == [0, 1, 2]


def test_skipped_ticket_does_not_block_the_ones_behind_it():
    d = OrderedDispatcher()
    ran = []
    a, b, c = (d.reserve("chat") for _ in range(3))
    d.complete("chat", c, lambda: ran.append("c"))
    d.complete("chat", a, None)
    assert ran == []
    d.complete("chat", b, lambda: ran.append("b"))
    assert ran == ["b", "c"]


def test_failing_action_does_not_block_the_next():
    d = OrderedDispatcher()
    ran = []
    a, b = d.reserve("chat"), d.reserve("chat")

    def boom():
        raise RuntimeError("x")

    d.complete("chat", b, lambda: ran.append("b"))
    d.complete("chat", a, boom)
    assert ran == ["b"]


def test_conversations_are_independent():
    d = OrderedDispatcher()
    ran = []
    d.reserve("one")
    two = d.reserve("two")
    d.complete("two", two, lambda: ran.append("two"))
    assert ran == ["two"]


def test_reentrant_complete_from_other_thread_keeps_order():
    d = OrderedDispatcher()
    ran = []
    tickets = [d.reserve("k") for _ in range(50)]
    threads = [threading.Thread(target=d.complete,
                                args=("k", t, lambda t=t: ran.append(t)))
               for t in reversed(tickets)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert ran == tickets
