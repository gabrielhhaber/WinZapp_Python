"""The pure half of dictation into the message field (ACC-03).

One key starts it, the same key stops it, the text lands at the caret and the
message is never sent. What is here has no wx and touches no hardware, so the
state machine, the refusals and the text arithmetic are tested directly; the
panel's half is ``ui/conversation_panel/dictation.py``, the microphone is
``core/dictation_capture.py`` and the transcription is
``core/transcription/dictation_run.py``.

Decisions this file carries:

* **A press means a different thing in each state.** Idle starts, listening
  stops and transcribes, and anything slower than that (the microphone still
  opening, the transcription running) cancels: a blind user who cannot see a
  progress bar needs the same key to be the way out.
* **A token per session.** The microphone opens and the transcription runs on
  worker threads; a result that arrives after a cancel or after the
  conversation changed must find the token stale and do nothing.
* **The audio is a buffer in memory.** It reaches the disk only as a WAV in a
  private temporary folder for the converter, removed when the run ends
  (``dictation_run``); nothing leaves the machine.
* **The text is inserted as an editor would** (``insertion_piece``): at the
  caret, replacing a selection (WriteText does that, and Ctrl+Z gives it
  back), with a space added only where the text would otherwise run into a
  word, the same rule as "insert into the message".
"""

from __future__ import annotations

import wave

IDLE = "idle"
STARTING = "starting"
LISTENING = "listening"
TRANSCRIBING = "transcribing"
STATES = (IDLE, STARTING, LISTENING, TRANSCRIBING)

ACTION_START = "start"
ACTION_STOP = "stop"
ACTION_CANCEL = "cancel"

#: A hard ceiling on one dictation, so a forgotten one cannot fill the memory.
#: Reaching it stops and transcribes, as if the key had been pressed.
MAX_SECONDS = 300.0
#: Shorter than this is a key bounce or a cough, not speech.
MIN_SECONDS = 0.4
#: Longest text read back whole; a longer one is announced by its start.
SPOKEN_LIMIT = 240

REFUSED_RECORDING_I18N_KEY = "dictation_refused_recording"
REFUSED_CALL_I18N_KEY = "dictation_refused_call"
REFUSED_BY_DICTATION_I18N_KEY = "dictation_refused_voice_recording"

I18N_KEYS = (
    "dictation_inserted",
    "dictation_inserted_long",
    "dictation_transcribing",
    "dictation_cancelled",
    "dictation_not_inserted",
    "dictation_no_speech",
    REFUSED_RECORDING_I18N_KEY,
    REFUSED_CALL_I18N_KEY,
    REFUSED_BY_DICTATION_I18N_KEY,
)


def press_action(state: str) -> str:
    """What the dictation key does in `state`."""
    if state == IDLE:
        return ACTION_START
    if state == LISTENING:
        return ACTION_STOP
    return ACTION_CANCEL


class Dictation:
    """The state of one panel's dictation. Every transition answers whether it
    applied, so a stale callback can be told apart from a live one."""

    def __init__(self):
        self.state = IDLE
        self.token = 0

    def begin(self):
        """IDLE -> STARTING; the new session's token, or None when busy."""
        if self.state != IDLE:
            return None
        self.token += 1
        self.state = STARTING
        return self.token

    def is_current(self, token) -> bool:
        return token == self.token and self.state != IDLE

    def opened(self, token) -> bool:
        if not (self.is_current(token) and self.state == STARTING):
            return False
        self.state = LISTENING
        return True

    def stop(self, token) -> bool:
        if not (self.is_current(token) and self.state == LISTENING):
            return False
        self.state = TRANSCRIBING
        return True

    def end(self, token) -> bool:
        """Back to IDLE, from the failure of the opening or the end of the run."""
        if not self.is_current(token):
            return False
        self.state = IDLE
        return True

    def cancel(self) -> str:
        """Back to IDLE whatever the state, voiding the session in flight;
        the state it was in."""
        previous = self.state
        self.token += 1
        self.state = IDLE
        return previous


def is_active(panel) -> bool:
    """Whether `panel` is starting, listening to or transcribing a dictation;
    False for a panel that never dictated (a stub, or one before its first key)."""
    state = getattr(panel, "_dictation", None)
    return state is not None and state.state != IDLE


def refusal_key(recording: bool, in_call: bool, capture_missing: bool = False):
    """The i18n key of why dictation may not start now, or None.

    A recording or a call owns the microphone, and two readers of one device
    is how a voice message came out half silent, so it is refused with a
    sentence instead of contested. `capture_missing` is "voice_recording_
    unavailable"'s case (no audio library), spoken with that existing key.
    """
    if capture_missing:
        return "voice_recording_unavailable"
    if in_call:
        return REFUSED_CALL_I18N_KEY
    if recording:
        return REFUSED_RECORDING_I18N_KEY
    return None


def capture_seconds(total_bytes: int, rate: int, channels: int) -> float:
    """Seconds of 16-bit PCM in `total_bytes`."""
    frame_bytes = max(1, int(rate)) * max(1, int(channels)) * 2
    return max(0, int(total_bytes)) / frame_bytes


def is_too_short(seconds: float) -> bool:
    return seconds < MIN_SECONDS


def write_wav(path, chunks, rate: int, channels: int) -> None:
    """The captured 16-bit PCM chunks as a WAV file at `path`."""
    with wave.open(path, "wb") as out:
        out.setnchannels(int(channels))
        out.setsampwidth(2)
        out.setframerate(int(rate))
        for chunk in chunks:
            out.writeframes(chunk)


def normalize_transcript(text) -> str:
    """One line, single spaces: Whisper may break a dictation into lines, and
    a newline in the field would read as the user having pressed Enter."""
    return " ".join((text or "").split())


def insertion_piece(text, before: str, after: str) -> str:
    """`text` as it goes into the field between `before` and `after`.

    Only spaces are added, and only where the text would otherwise run into a
    word the user had typed; none before the comma already typed after it.
    """
    text = normalize_transcript(text)
    if not text:
        return ""
    if before and not before.isspace():
        text = " " + text
    if after and after.isalnum():
        text = text + " "
    return text


def spoken_excerpt(text, limit: int = SPOKEN_LIMIT):
    """(what to say, whether the text was cut): the text whole when short,
    else its start cut at a word."""
    text = normalize_transcript(text)
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    if text[limit] != " " and " " in cut:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(), True
