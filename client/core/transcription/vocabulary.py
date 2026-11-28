"""The user's own vocabulary, handed to Whisper as its initial prompt. Pure.

Names, acronyms and jargon are what Whisper gets wrong most (a colleague's
name comes back as the nearest common word). Both backends accept a short text
the decoder reads *as if it were the speech just before the audio*, which tips
the spelling of those words; that is all this is — a hint, never a rule.

Decisions worth keeping:

* **It stays on the machine and out of the log.** It lives only in the
  account's own settings (`transcription.vocabulary`), is read right before a
  run and is passed in memory (faster-whisper) or on whisper-cli's command
  line; no module logs it, and nothing here reaches a network. The names in it
  are often those of real people.

* **Short.** Whisper reads at most 224 tokens of prompt (the rest is dropped
  from the front), and a long list makes the decoder repeat it. `MAX_CHARS`
  keeps well inside that, and terms are cut whole rather than mid-word.

* **It is not part of what a saved transcription is keyed on.** A stored text
  belongs to a backend and model (`stored.with_result`); a different vocabulary
  does not make a second result. It is a hint to the same model, and keying on
  it would make every edit of the list orphan the texts already kept. A user
  who wants a message redone with new terms deletes its saved transcription.

* **whisper.cpp gets the plain-ASCII terms only.** whisper-cli takes the prompt
  as a command-line argument, which reaches the program in the system code
  page, and whether it decodes that as UTF-8 was not verified on a real run
  (the same doubt `whisper_cpp_cli.cli_paths()` hedges for paths). A garbled
  prompt is worse than none, so a term with an accent or other non-ASCII
  character is left out of that backend's prompt and kept for faster-whisper's.
"""

from __future__ import annotations

import re

#: The key inside settings["transcription"]. Absent means "no vocabulary":
#: it is deliberately not in `preferences.DEFAULTS`, which a test pins to the
#: stored-automatic strings and the installs' defaults.
SETTING_VOCABULARY = "vocabulary"

#: The longest list the settings field accepts, and the longest prompt built.
MAX_CHARS = 300

_SEPARATORS = re.compile(r"[\r\n,;]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_JOINER = ", "


def terms_of(text) -> list:
    """The terms in what the user typed: one per line or separated by commas
    or semicolons, whitespace collapsed, empty ones and repeats (ignoring
    case) dropped, in the order typed."""
    terms = []
    seen = set()
    for piece in _SEPARATORS.split(str(text or "")):
        term = " ".join(_CONTROL.sub(" ", piece).split())
        if term and term.casefold() not in seen:
            seen.add(term.casefold())
            terms.append(term)
    return terms


def prompt_from_text(text) -> str:
    """The initial prompt for what the user typed, at most `MAX_CHARS` long,
    made of whole terms. "" when there is nothing."""
    kept = []
    length = 0
    for term in terms_of(text):
        added = len(term) + (len(_JOINER) if kept else 0)
        if length + added > MAX_CHARS:
            break
        kept.append(term)
        length += added
    return _JOINER.join(kept)


def prompt_from_settings(settings) -> str:
    """`prompt_from_text()` of the stored vocabulary; tolerant of a missing
    section or a value that is not text."""
    section = settings.get("transcription") if isinstance(settings, dict) else None
    value = section.get(SETTING_VOCABULARY) if isinstance(section, dict) else None
    return prompt_from_text(value) if isinstance(value, str) else ""


def ascii_prompt(prompt) -> str:
    """`prompt` with the terms that are not plain ASCII left out (see the
    module docstring: whisper.cpp's command line)."""
    terms = (term.strip() for term in str(prompt or "").split(","))
    return _JOINER.join(term for term in terms if term and term.isascii())
