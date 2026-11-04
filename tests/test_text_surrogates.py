"""core/text_surrogates.py: emoji that arrive as two UTF-16 halves."""

import pytest

from core.text_surrogates import (
    SurrogateJoiner,
    has_surrogate,
    repair_payload,
    repair_surrogates,
)

LAPTOP = "\U0001F4BB"            # U+1F4BB, "\ud83d\udcbb" in UTF-16


def test_two_loose_halves_become_the_emoji():
    assert repair_surrogates("a\ud83d\udcbbb") == f"a{LAPTOP}b"


@pytest.mark.parametrize("text, expected", [
    ("\ud83d", ""),                       # a first half whose pair was cut off
    ("\udcbb", ""),                       # the mention query that was logged
    ("ab\ud83d cd", "ab cd"),             # what reached send-mentioned
    ("\udcbb\ud83d", ""),                 # the halves in the wrong order
])
def test_a_half_without_its_pair_is_dropped(text, expected):
    assert repair_surrogates(text) == expected


@pytest.mark.parametrize("value", ["", "texto comum", f"ok {LAPTOP}", None, 3, b"x"])
def test_valid_text_and_non_strings_are_untouched(value):
    assert repair_surrogates(value) is value


def test_has_surrogate_only_sees_surrogate_code_points():
    assert has_surrogate("\ud83d")
    assert not has_surrogate(LAPTOP)
    assert not has_surrogate(None)


def test_every_string_of_a_payload_is_repaired():
    payload = {"message": "x\ud83d\udcbb", "list": ["\ud83d", "ok"], "n": 1,
               "nested": {"caption": "\udcbb"}}
    assert repair_payload(payload) == {"message": f"x{LAPTOP}", "list": ["", "ok"], "n": 1,
                                       "nested": {"caption": ""}}


def test_a_payload_with_nothing_to_repair_is_the_same_object():
    payload = {"message": "ok", "list": ["a", 1], "nested": {"b": None}}
    assert repair_payload(payload) is payload


def test_the_joiner_waits_for_the_second_half():
    joiner = SurrogateJoiner()
    assert joiner.feed(0xD83D) == ""
    assert joiner.feed(0xDCBB) == LAPTOP
    assert joiner.feed(ord("a")) == "a"


def test_the_joiner_drops_a_half_left_without_its_pair():
    joiner = SurrogateJoiner()
    assert joiner.feed(0xD83D) == ""
    assert joiner.feed(ord("a")) == "a"          # the pending first half is gone
    assert joiner.feed(0xDCBB) == ""             # a second half with no first one


def test_an_emoji_typed_in_the_mention_list_lands_whole_in_the_field(monkeypatch):
    """The mention list redirected each EVT_CHAR with chr(): an emoji (two
    events, one per UTF-16 half) went into the field as two loose halves."""
    import wx
    from ui.conversation_panel import composer

    monkeypatch.setattr(wx, "CallAfter", lambda *args, **kwargs: None)
    # Windows counts an emoji as two caret positions
    monkeypatch.setattr(composer, "platform_counts_utf16", lambda: True)

    class Field:
        def __init__(self):
            self.value, self.point = "@", 1

        def GetInsertionPoint(self):
            return self.point

        def GetValue(self):
            return self.value

        def ChangeValue(self, value):
            self.value = value

        def SetInsertionPoint(self, point):
            self.point = point

    class Event:
        def __init__(self, code):
            self.code = code

        def GetUnicodeKey(self):
            return self.code

        def Skip(self):
            raise AssertionError("a printable char is not passed on")

    stub = type("Stub", (), {})()
    stub.message_field = Field()
    stub._on_mention_list_after_char = lambda: None
    handler = composer.ComposerMixin._on_mention_list_char
    handler(stub, Event(0xD83D))
    assert stub.message_field.value == "@"          # waits for the second half
    handler(stub, Event(0xDCBB))
    assert stub.message_field.value == "@" + LAPTOP
    assert stub.message_field.point == 3            # after both halves, never between them
