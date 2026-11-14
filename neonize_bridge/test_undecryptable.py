"""The summary of undecryptable messages and of the phone's answers (undecryptable.py)."""
import undecryptable


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_one_summary_per_window_with_what_happened_in_it():
    clock = Clock()
    tally = undecryptable.Tally(clock)
    tally.failed(own=True, unavailable=False)
    tally.failed(own=True, unavailable=False)
    tally.failed(own=False, unavailable=True)
    assert tally.due() == ""                      # the window is not over
    clock.now = undecryptable.WINDOW
    tally.recovered()
    assert tally.due() == ("undecryptable messages in 30s: 2 from this account, 1 from "
                           "others (1 without the sender's key); 1 given back by the phone")
    clock.now = 2 * undecryptable.WINDOW
    assert tally.due() == ""                      # nothing new: no line
    tally.recovered()
    assert "0 from this account" in tally.due() and tally.due() == ""
