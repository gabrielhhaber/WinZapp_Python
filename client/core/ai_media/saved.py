"""The AI description already stored with a message (pure, no wx).

Opening the AI window on a picture, sticker or video that already has a saved
description shows that text at once instead of paying for a new request; the
person can still ask again ("Run again") or ask a follow-up question.
"""
from core.transcription import stored

#: Kinds whose saved text is an AI description (a transcription of audio is the
#: offline Whisper's business, and a PDF's text is never kept in the line).
DESCRIBABLE_KINDS = ("image", "sticker", "video")


def saved_description(msg, kind) -> str:
    """The description saved on ``msg``, or "" when there is none to reuse.

    Only a record made by an AI description (``kind == "description"``) counts:
    a transcription of a video's sound stored on the same message is not what
    the person asked to be described. A video holds both side by side, so the
    description is found whichever of the two was saved last.
    """
    if kind not in DESCRIBABLE_KINDS:
        return ""
    value = stored.saved_description_record(msg)
    if not isinstance(value, dict):
        return ""
    text = value.get("text")
    return text.strip() if isinstance(text, str) else ""
