"""What history-sync-status says about the history (history_progress.py)."""
from types import SimpleNamespace as N

import history_progress as hp


def test_old_account_without_new_blocks_is_complete():
    p = hp.Progress(now=0)
    assert p.complete(1, stored_chats=40) is True


def test_new_account_waits_for_the_first_block_and_then_gives_up():
    p = hp.Progress(now=0)
    assert p.complete(10, stored_chats=0) is False
    assert p.complete(hp.INITIAL_WAIT, stored_chats=0) is True


def test_block_in_flight_is_never_complete_and_counts_as_unprocessed():
    p = hp.Progress(now=0)
    p.started(5)
    state = p.state(6, stored_chats=10)
    assert state == {"unprocessedChunks": 1, "initialSyncComplete": False,
                     "recentCompleted": False}


def test_progress_below_100_survives_the_short_silence():
    p = hp.Progress(now=0)
    p.started(5)
    p.finished(6, percent=40)
    assert p.complete(7, stored_chats=10) is False
    assert p.complete(6 + hp.SILENCE, stored_chats=10) is False
    assert p.complete(6 + hp.PARTIAL_SILENCE - 1, stored_chats=10) is False
    assert p.complete(6 + hp.PARTIAL_SILENCE, stored_chats=10) is True


def test_blocks_without_a_percent_keep_the_short_silence():
    p = hp.Progress(now=0)
    p.started(5)
    p.finished(6)
    assert p.complete(6 + hp.SILENCE - 1, stored_chats=10) is False
    assert p.complete(6 + hp.SILENCE, stored_chats=10) is True


def test_progress_100_completes_at_once():
    p = hp.Progress(now=0)
    p.started(5)
    p.finished(6, percent=100)
    assert p.state(7, stored_chats=10)["recentCompleted"] is True


def test_new_block_after_complete_goes_back_to_waiting():
    p = hp.Progress(now=0)
    p.started(1)
    p.finished(2, percent=100)
    p.started(3)
    p.finished(4, percent=10)
    assert p.complete(5, stored_chats=10) is False


def test_finished_without_started_does_not_go_negative():
    p = hp.Progress(now=0)
    p.finished(1)
    assert p.in_flight == 0


def test_progress_of_creates_only_one_per_session():
    s = N()
    assert hp.progress_of(s, 0) is hp.progress_of(s, 99)
