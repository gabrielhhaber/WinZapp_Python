"""Pure-logic tests for the small message-field spell-check adapter."""

import core.spell_checker as spell_checker
from core.spell_checker import WindowsSpellChecker, _word_ended


def test_word_ended_only_on_transition_to_whitespace():
    assert _word_ended("ktury", "ktury ")
    assert not _word_ended("ktury ", "ktury  ")
    assert not _word_ended("ktury", "ktur")
    assert not _word_ended("ktury drugi", "ktury ")
    assert not _word_ended("ktury", "ktury  ")


def test_checker_tracks_text_and_returns_errors_after_word_boundary():
    played = []
    checker = WindowsSpellChecker(on_error=lambda: played.append(True))
    checker.errors_for_text = lambda text: [(0, 5)] if text.startswith("ktury") else []
    assert checker.text_changed("ktury") == []
    assert checker.text_changed("ktury ") == [(0, 5)]
    assert played == [True]
    # A second space must not retrigger the same word.
    assert checker.text_changed("ktury  ") == []
    assert played == [True]


def test_deleting_a_misspelled_word_does_not_replay_the_sound():
    played = []
    checker = WindowsSpellChecker(on_error=lambda: played.append(True))
    checker.errors_for_text = lambda text: [(0, 5), (6, 11)]

    checker.text_changed("ktury drugi")
    assert checker.text_changed("ktury ") == []
    assert played == []


def test_language_candidates_prefer_winzapp_then_use_windows(monkeypatch):
    monkeypatch.setattr(
        spell_checker,
        "_windows_language_names",
        lambda: ["de-DE", "pl-PL"],
    )

    assert spell_checker._language_candidates("pl") == ["pl-PL", "de-DE"]


def test_changing_language_reopens_the_checker_lazily():
    checker = WindowsSpellChecker(language="pl")
    checker._initialized = True
    checker._checker = object()
    checker.language = "pl-PL"

    checker.set_language("en-US")

    assert checker.preferred_language == "en-US"
    assert checker.language == ""
    assert checker._checker is None
    assert checker._initialized is False
