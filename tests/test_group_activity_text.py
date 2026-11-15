"""The sentence naming everyone who is typing or recording in a group."""

from core.group_activity import group_activity_text, join_names

EN = {
    "group_typing_singular": "{names} is typing",
    "group_typing_plural": "{names} are typing",
    "group_recording_singular": "{names} is recording audio",
    "group_recording_plural": "{names} are recording audio",
    "group_activity_and": "{first} and {second}",
    "group_activity_comma": "{first}, {second}",
}
PT = {
    "group_typing_singular": "{names} está digitando",
    "group_typing_plural": "{names} estão digitando",
    "group_recording_singular": "{names} está gravando áudio",
    "group_recording_plural": "{names} estão gravando áudio",
    "group_activity_and": "{first} e {second}",
    "group_activity_comma": "{first}, {second}",
}


def text(entries, catalog=PT):
    return group_activity_text(entries, catalog.__getitem__)


def test_one_person_typing():
    assert text([("Pessoa1", "composing")]) == "Pessoa1 está digitando..."
    assert text([("Person1", "composing")], EN) == "Person1 is typing..."


def test_one_person_recording():
    assert text([("Pessoa1", "recording")]) == "Pessoa1 está gravando áudio..."


def test_two_and_three_people_agree_in_the_plural():
    two = [("Pessoa1", "composing"), ("Pessoa2", "composing")]
    three = two + [("Pessoa3", "composing")]
    assert text(two) == "Pessoa1 e Pessoa2 estão digitando..."
    assert text(three) == "Pessoa1, Pessoa2 e Pessoa3 estão digitando..."


def test_one_typing_and_one_recording():
    entries = [("Pessoa1", "composing"), ("Pessoa2", "recording")]
    assert text(entries) == "Pessoa1 está digitando e Pessoa2 está gravando áudio..."


def test_plural_typing_and_single_recording_uses_and():
    entries = [(f"Pessoa{i}", "composing") for i in (1, 2, 3)] + [("Pessoa4", "recording")]
    assert text(entries) == (
        "Pessoa1, Pessoa2 e Pessoa3 estão digitando e Pessoa4 está gravando áudio...")


def test_two_plural_clauses_are_joined_by_a_comma():
    entries = [(f"Pessoa{i}", "composing") for i in (1, 2, 3)]
    entries += [("Pessoa4", "recording"), ("Pessoa5", "recording")]
    assert text(entries) == (
        "Pessoa1, Pessoa2 e Pessoa3 estão digitando, Pessoa4 e Pessoa5 estão gravando áudio...")


def test_typing_comes_first_whatever_the_arrival_order():
    entries = [("Pessoa2", "recording"), ("Pessoa1", "composing")]
    assert text(entries) == "Pessoa1 está digitando e Pessoa2 está gravando áudio..."


def test_nameless_duplicate_and_unknown_entries_are_skipped():
    entries = [("", "composing"), ("Ana", "composing"), ("Ana", "composing"), ("Bia", "paused")]
    assert text(entries) == "Ana está digitando..."
    assert text([("", "composing")]) == ""
    assert text([]) == ""


def test_join_names():
    t = PT.__getitem__
    assert join_names([], t) == ""
    assert join_names(["A"], t) == "A"
    assert join_names(["A", "B", "C", "D"], t) == "A, B, C e D"
