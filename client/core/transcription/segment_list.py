"""The timed segments of a long transcription, as rows a person can walk. Pure.

A transcription of a ten-minute note is one block of text; finding "where did
she say the address" means reading all of it. The backends already hand over
`TranscriptionSegment(start, end, text)` per segment, and the result window
shows them as a list of `[mm:ss] text` rows for a recording long enough to
need one.

Decisions worth keeping:

* **Only for the window that has just transcribed.** `stored.record_from_result`
  deliberately keeps no segments (they would roughly double what is saved with
  every message), so a transcription opened again from the message has the
  text and no list. Nothing here asks storage to change.

* **Only from `MIN_SECONDS`.** A one-minute note is read faster than it is
  navigated, and a list in front of its text is one more thing to tab past.

* **The clean text is untouched.** The rows are a second view; the text field,
  Copy and Save still give the text without a single timestamp, and the list
  has a copy of its own for the rows with their stamps.
"""

from __future__ import annotations

#: A recording shorter than this gets no list (see the module docstring).
MIN_SECONDS = 120


def format_stamp(seconds) -> str:
    """`mm:ss`, or `h:mm:ss` from an hour on. Never negative, never raises."""
    try:
        whole = max(0, int(float(seconds)))
    except (TypeError, ValueError, OverflowError):
        whole = 0
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def usable(segments) -> tuple:
    """The segments that carry words, in order."""
    return tuple(s for s in (segments or ()) if getattr(s, "text", "").strip())


def offers_list(duration_seconds, segments) -> bool:
    """Whether the window gets a list: a long recording with more than one
    segment to move between."""
    try:
        long_enough = float(duration_seconds or 0) > MIN_SECONDS
    except (TypeError, ValueError):
        long_enough = False
    return long_enough and len(usable(segments)) > 1


def rows(segments) -> list:
    """One `[mm:ss] text` row per segment with words."""
    return [f"[{format_stamp(s.start)}] {s.text.strip()}" for s in usable(segments)]


def stamped_text(segments) -> str:
    """All the rows, one per line: what the list's own Copy puts on the clipboard."""
    return "\n".join(rows(segments))


def start_of(segments, index):
    """The second the `index`-th row starts at, or None when there is no such row."""
    shown = usable(segments)
    if not isinstance(index, int) or not 0 <= index < len(shown):
        return None
    return max(0.0, float(shown[index].start))
