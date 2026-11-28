"""Understanding a whole video: what is seen and what is said, in one text (pure, no wx).

The mosaic of frames (video_frame.py) is what is seen; what is said is the
video's own sound, transcribed on this computer by the local Whisper
(core/transcription). The sound never leaves the machine; only its *text* does,
and only after a consent of its own (ui/dialogs/ai_full_video.py), because a
chat summary deliberately does not send a video's speech and the ordinary
description's consent says nothing about it.

This module holds the decisions that need no window: which speech text goes,
how much of it, which way the action takes after the transcription step, and
what the request carries. Nothing here logs: the text is a private conversation.
"""
from core.transcription import errors
from core.transcription import stored

#: Longest speech text sent. About three thousand tokens: room for several
#: minutes of speech without letting a long video's text swamp the request.
MAX_SPEECH_CHARS = 12_000

#: The application's own opening request (never shown as a user question).
QUESTION = "Describe this video, joining what the frames show with what is said in it."

#: How the transcription step ended.
SPEECH_OK = "ok"
SPEECH_NONE = "none"            # no speech, or no sound track at all
SPEECH_FAILED = "failed"
SPEECH_CANCELLED = "cancelled"
SPEECH_BUSY = "busy"            # another transcription holds the local model

#: What the action does next, from how the transcription step ended.
NEXT_SEND = "send"              # frames and text, after the consent
NEXT_IMAGES_ONLY = "images"     # nothing to say: the ordinary description
NEXT_ASK = "ask"                # failed: offer the ordinary description
NEXT_STOP = "stop"


def saved_speech(msg, selection=None) -> str:
    """The transcription saved with ``msg`` (what is said, never the AI
    description kept beside it), or "". It is the local Whisper's own record,
    independent of any description."""
    record = stored.saved_transcription(msg, selection)
    text = record.get("text") if isinstance(record, dict) else None
    return text.strip() if isinstance(text, str) else ""


def clip_speech(text, limit=MAX_SPEECH_CHARS):
    """``(text, truncated)``: ``text`` stripped and, when longer than ``limit``,
    cut at the last word break near the limit (the start of the speech is
    kept)."""
    text = str(text or "").strip()
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    space = max(cut.rfind(" "), cut.rfind("\n"))
    if space > limit - 200:
        cut = cut[:space]
    return cut.rstrip(), True


def speech_payload(text):
    """What a request carries of the speech, ``{"text", "truncated"}``, or None
    when there is nothing to send."""
    clipped, truncated = clip_speech(text)
    return {"text": clipped, "truncated": truncated} if clipped else None


def classify(text, code=None, cancelled=False) -> str:
    """How a transcription run ended: its ``text`` and the error ``code`` it
    reported (None for none). A video without a sound track, or with nothing
    said, is not a failure: there is just nothing to add to the pictures."""
    if cancelled or code == errors.CANCELLED:
        return SPEECH_CANCELLED
    if code == errors.NO_AUDIO_TRACK:
        return SPEECH_NONE
    if code is not None:
        return SPEECH_FAILED
    return SPEECH_OK if str(text or "").strip() else SPEECH_NONE


def next_step(status) -> str:
    if status == SPEECH_OK:
        return NEXT_SEND
    if status == SPEECH_NONE:
        return NEXT_IMAGES_ONLY
    if status == SPEECH_FAILED:
        return NEXT_ASK
    return NEXT_STOP
