"""Which rows a voice-note sequence holds back (core/chain_repaint_guard.py).

The rule on its own, with no list control: while a sequence runs, the row
about to be rewritten is parked when it has list focus, whichever message is
playing, and so is the row the sequence has just moved focus off. Nothing
else. tests/test_audio_chain_played_repaint_hold.py runs the same rule through
the real panel methods and a real wx.ListCtrl.
"""

from core.chain_repaint_guard import ChainRepaintGuard


def test_with_no_sequence_running_nothing_is_parked():
    """Outside a sequence a status change on the focused row is ordinary
    feedback, read by the screen reader as it always was."""
    guard = ChainRepaintGuard()

    assert guard.armed is False
    assert guard.must_park("m0", has_focus=True) is False
    assert guard.release() == set()


def test_the_focused_row_is_parked_whichever_message_it_is():
    """Not "the voice note that just ended": the user may have left the audio
    playing and moved to a message of their own, or turned the focus move off
    and stayed where they were. The row the screen reader would read over the
    audio is the focused one."""
    guard = ChainRepaintGuard()
    guard.arm()

    assert guard.must_park("voice-note-that-ended", has_focus=True) is True
    assert guard.must_park("sent-just-now", has_focus=True) is True


def test_a_row_without_focus_is_written():
    """The reported bug: a receipt for a message the user had just sent was
    held for as long as a sequence was alive, paused or not."""
    guard = ChainRepaintGuard()
    guard.arm()

    assert guard.must_park("sent-just-now", has_focus=False) is False
    assert guard.must_park("voice-note-that-ended", has_focus=False) is False


def test_the_row_the_sequence_moved_focus_off_stays_parked_until_release():
    """It has no focus any more, but the screen reader handles a batch's name
    changes before that batch's focus change, so it may not know that yet."""
    guard = ChainRepaintGuard()
    guard.arm()
    guard.focus_moved_off("m0")

    assert guard.must_park("m0", has_focus=False) is True

    guard.release()
    assert guard.armed is False
    assert guard.must_park("m0", has_focus=False) is False
    assert guard.must_park("m0", has_focus=True) is False


def test_every_step_of_a_sequence_adds_the_row_it_left():
    guard = ChainRepaintGuard()
    guard.arm()
    guard.focus_moved_off("m0")
    guard.arm()
    guard.focus_moved_off("m1")

    assert guard.must_park("m0", has_focus=False) is True
    assert guard.must_park("m1", has_focus=False) is True


def test_release_returns_what_is_owed_once():
    guard = ChainRepaintGuard()
    guard.arm()
    guard.parked.add("m0")

    assert guard.release() == {"m0"}
    assert guard.release() == set(), "a second release must not rewrite rows"


def test_taking_the_parked_rows_keeps_the_sequence_guarded():
    """A repaint pass re-examines parked rows; that alone must not end the
    hold."""
    guard = ChainRepaintGuard()
    guard.arm()
    guard.focus_moved_off("m0")
    guard.parked.add("m0")

    assert guard.take_parked() == {"m0"}
    assert guard.parked == set()
    assert guard.armed is True
    assert guard.must_park("m0", has_focus=False) is True


def test_an_empty_id_is_not_remembered_as_a_row_left():
    """A row with no id (a pending send) must not start matching every other
    id-less row."""
    guard = ChainRepaintGuard()
    guard.arm()
    guard.focus_moved_off("")

    assert guard.must_park("", has_focus=False) is False
