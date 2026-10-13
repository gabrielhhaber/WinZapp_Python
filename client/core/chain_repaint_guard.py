"""Which message rows must not be rewritten while a voice-note sequence runs.

A row rewritten while the screen reader believes it has focus is read out
whole (see docs/traps/screen-reader-speech.md). During a sequence that is
speech over the audio the user is listening to, and at the moment one voice
note ends and the next begins it is the finished row read back ahead of the
new one.

So while a sequence is alive the question asked of every row about to be
rewritten is only this: does it have list focus? Which message is playing
does not matter. The user may be on the voice note that is playing, on one
that already played, or on a message of their own further down (they can
leave the audio playing and walk the list, and Settings > Interface can stop
the sequence from moving focus at all): the row the screen reader would read
is the focused one, whatever it is.

One row is parked without having focus: the one the sequence has just moved
focus off. NVDA handles the name changes of a batch of events before the focus
change of that same batch, so for a moment it still believes that row has
focus; it stays parked until the sequence is over.

Every other row is written as usual. The first version of this hold parked
every status repaint of the conversation for as long as a sequence was alive,
and a sequence stays alive while its audio is paused: a user who paused a
chained voice note to record a reply saw no "sent", "delivered" or "read" on
anything they sent until some later playback happened to end the sequence
(reported 2026-10-12, with the message data window showing "read" for a row
that showed nothing).

No wx here: the panel asks the list which row has focus and passes the answer.
"""


class ChainRepaintGuard:
    """What one running voice-note sequence keeps from being rewritten."""

    def __init__(self):
        #: True from the first voice note that chains into another until the
        #: sequence ends, is stopped or is paused.
        self.armed = False
        #: Rows the sequence moved list focus off, parked until it is over.
        self._focus_left = set()
        #: Message ids whose repaint was held back and is still owed.
        self.parked = set()

    def arm(self) -> None:
        """A voice note ended and another one follows it."""
        self.armed = True

    def focus_moved_off(self, msg_id) -> None:
        """The sequence is moving list focus off this row."""
        if msg_id:
            self._focus_left.add(msg_id)

    def must_park(self, msg_id, has_focus: bool) -> bool:
        """True when writing this row now could make the screen reader read it."""
        if not self.armed:
            return False
        return has_focus or msg_id in self._focus_left

    def take_parked(self) -> set:
        """The parked ids, handed over for another look (and forgotten here)."""
        parked, self.parked = self.parked, set()
        return parked

    def release(self) -> set:
        """The sequence is over: disarm and return every repaint still owed."""
        self.armed = False
        self._focus_left.clear()
        return self.take_parked()
