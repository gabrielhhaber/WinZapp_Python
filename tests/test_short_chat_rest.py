"""Short chats that keep coming back empty rest between retries (issue #766)."""

from core import short_chat_rest as rest_mod
from core.short_chat_rest import FIRST_REST, MAX_REST, due_chats, note_fetch

NEVER_OPENED = lambda jid: False  # noqa: E731


def test_an_empty_fetch_rests_the_chat_longer_each_time():
    rest = {}
    note_fetch(rest, "a", False, 0, opened=NEVER_OPENED)
    assert rest["a"] == (1, FIRST_REST)
    note_fetch(rest, "a", False, 10, opened=NEVER_OPENED)
    assert rest["a"] == (2, 10 + 2 * FIRST_REST)
    for _ in range(20):
        note_fetch(rest, "a", False, 100, opened=NEVER_OPENED)
    assert rest["a"][1] == 100 + MAX_REST


def test_a_resting_chat_skips_turns_until_its_rest_ends():
    rest = {}
    note_fetch(rest, "a", False, 0, opened=NEVER_OPENED)
    assert due_chats(["a", "b"], rest, 1, opened=NEVER_OPENED, landing=False) == ["b"]
    assert due_chats(["a", "b"], rest, FIRST_REST, opened=NEVER_OPENED, landing=False) == ["a", "b"]


def test_a_fetch_that_brings_something_starts_over():
    rest = {}
    note_fetch(rest, "a", False, 0, opened=NEVER_OPENED)
    note_fetch(rest, "a", True, 5, opened=NEVER_OPENED)
    assert "a" not in rest
    assert due_chats(["a"], rest, 6, opened=NEVER_OPENED, landing=False) == ["a"]


def test_nothing_rests_while_history_lands_or_once_the_chat_is_opened():
    rest = {}
    note_fetch(rest, "a", False, 0, opened=NEVER_OPENED)
    assert due_chats(["a"], rest, 1, opened=NEVER_OPENED, landing=True) == ["a"]
    assert due_chats(["a"], rest, 1, opened=lambda jid: True, landing=False) == ["a"]
    note_fetch(rest, "a", False, 2, opened=lambda jid: True)
    assert "a" not in rest


def test_resting_reads_the_clock():
    rest = {"a": (1, 50.0)}
    assert rest_mod.resting(rest, "a", 49)
    assert not rest_mod.resting(rest, "a", 50)
    assert not rest_mod.resting(rest, "b", 0)
