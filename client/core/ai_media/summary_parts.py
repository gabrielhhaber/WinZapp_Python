"""Hierarchical (map-reduce) summary of a long backlog, opt-in.

The ordinary summary (summary.py) keeps the newest ``MAX_MESSAGES`` /
``MAX_CHARS`` and drops the older messages. With the preference
``summary_hierarchy`` on, a backlog that does not fit is cut into parts of about
``PART_CHARS`` characters, in chronological order and never inside a message
line; each part is summarized by its own request and one last request joins the
partial summaries. The ceiling is ``MAX_PARTS`` parts plus that final request
(``MAX_CALLS``), whatever the backlog: older parts beyond it are left out and
the window says how many messages that is.

Every request is a full ``run_chain`` (the fallback chain, the retry, the usage
counters) under an Operation of its own, so a slow part cannot starve the next
one; the window's Operation is told it has ``MAX_CALLS`` steps (its watchdog
outlasts them all) and a cancel of it reaches the request in flight at once.

If a part fails after the chain was tried, no summary is shown: with a part
missing it would read as complete and is not (``PartFailed`` names the part). If
only the final join fails, every part did succeed, so their summaries are
offered one after the other under "Part N of M" and the window says so.

Wx-free, and the transcript is a private conversation: nothing here logs.
"""
from typing import NamedTuple

from . import prompts
from .errors import DescriptionError
from .service import Operation
from .summary import MAX_CHARS, MAX_MESSAGES, VOICE_MARKER, is_voice_message

#: Characters of transcript per request.
PART_CHARS = 12_000
#: Parts summarized; with the final join that is ``MAX_CALLS`` requests.
MAX_PARTS = 6
MAX_CALLS = MAX_PARTS + 1
#: What a message line adds to its text (date, time, sender), for the estimate
#: made before the lines exist.
LINE_OVERHEAD = 60


class PartPlan(NamedTuple):
    #: The transcript of each part, oldest first.
    parts: tuple
    #: Messages in ``parts``.
    included: int
    #: Usable older messages that did not fit ``MAX_PARTS`` parts.
    left_out: int
    #: Voice messages in ``parts`` that are only the marker, with no words.
    voice_markers: int


class PartsResult(NamedTuple):
    answer: str
    provider: str
    #: The partial summaries as the final request got them: what a follow-up
    #: question is answered from.
    joined: str
    #: False when the final join failed and ``answer`` is the partial summaries.
    merged: bool


class PartFailed(DescriptionError):
    """Part ``part`` of ``total`` failed after every provider was tried.
    Carries the cause's category and attempts, so the window can say why."""

    def __init__(self, part, total, cause):
        super().__init__(cause.category)
        self.part = part
        self.total = total
        self.attempts = tuple(getattr(cause, "attempts", ()))

    @property
    def key(self):
        key = super().key
        key.attempts = self.attempts
        key.part = self.part
        key.total = self.total
        return key


def _group(items, limit, size=len):
    """``items`` in order as groups whose sizes plus one separator each fit
    ``limit``; an item larger than ``limit`` is a group of its own."""
    groups, current, used = [], [], 0
    for item in items:
        need = size(item) + (1 if current else 0)
        if current and used + need > limit:
            groups.append(current)
            current, used, need = [], 0, size(item)
        current.append(item)
        used += need
    if current:
        groups.append(current)
    return groups


def chunk_lines(lines, limit=PART_CHARS) -> list:
    """``lines`` joined with newlines into chunks of at most ``limit``
    characters, in order, never cutting a line: a line longer than ``limit``
    is a chunk of its own, whole."""
    return ["\n".join(group) for group in _group(lines, limit)]


def plan_parts(messages, format_line, text_of, part_chars=PART_CHARS, max_parts=MAX_PARTS) -> PartPlan:
    """The parts of ``messages`` (oldest first): one ``format_line(msg, text)``
    per usable message, grouped by ``chunk_lines``' rule, the newest
    ``max_parts`` groups kept. Same arguments as ``build_unread_transcript``."""
    usable = []
    for msg in messages:
        text = text_of(msg)
        if text:
            usable.append((msg, text))
    items = []
    for msg, text in usable:
        try:
            line = format_line(msg, text)
        except Exception:
            line = text
        if len(line) > part_chars:
            marker = " [truncated]"
            line = (line[:part_chars - len(marker)] + marker
                    if part_chars >= len(marker) else marker[:part_chars])
        items.append((line, text == VOICE_MARKER and is_voice_message(msg)))
    groups = _group(items, part_chars, size=lambda item: len(item[0]))
    kept = groups[-max_parts:] if max_parts > 0 else []
    flat = [item for group in kept for item in group]
    return PartPlan(tuple("\n".join(line for line, _marker in group) for group in kept),
                    len(flat), len(usable) - len(flat), sum(marker for _line, marker in flat))


def may_need_parts(messages, text_of) -> bool:
    """Whether ``messages`` may not fit the ordinary summary. Judged before the
    voice messages are transcribed and the lines exist (the consent comes
    first), so it is an estimate that leans toward yes."""
    if len(messages) > MAX_MESSAGES:
        return True
    size = sum(len(text_of(msg) or "") + LINE_OVERHEAD for msg in messages)
    return size > MAX_CHARS * 2 // 3


def merge_input(partials) -> str:
    """What the final request is given: each partial summary under its part."""
    total = len(partials)
    return "\n\n".join(f"Part {number} of {total}:\n{text}" for number, text in enumerate(partials, 1))


def joined_partials(partials, heading) -> str:
    """The partial summaries as one text, in order, each under
    ``heading(number, total)`` (the answer when the final join failed)."""
    total = len(partials)
    return "\n\n".join(f"{heading(number, total)}\n{text}" for number, text in enumerate(partials, 1))


def _step(parent, local):
    """A fresh Operation for one request, reached by the parent's cancel."""
    child = Operation("text", local=local)
    parent.adopt(child)
    return child


def run_parts(parent, parts, call, progress=None, local=False, heading=None) -> PartsResult:
    """Summarize each of ``parts`` and join the results.

    ``call(operation, merging, question, text)`` makes one request (a full
    provider chain) and returns ``(answer, provider)``; ``progress(step, total)``
    hears each part as it starts and then ``(len(parts) + 1, len(parts))`` for the
    final join. ``parent`` is the window's Operation, built with
    ``steps=len(parts) + 1``: ``parent.check()`` between requests ends the
    action at once on a cancel or its deadline, and its error is raised as it is.
    """
    total = len(parts)
    partials = []
    provider = ""
    for number, text in enumerate(parts, 1):
        parent.check()
        if progress:
            progress(number, total)
        try:
            answer, provider = call(_step(parent, local), False,
                                    prompts.part_question(number, total), text)
        except DescriptionError as exc:
            parent.check()  # a cancel or the deadline is not this part's failure
            raise PartFailed(number, total, exc) from None
        partials.append(answer)
    parent.check()
    if progress:
        progress(total + 1, total)
    joined = merge_input(partials)
    try:
        answer, provider = call(_step(parent, local), True, prompts.MERGE_QUESTION, joined)
    except DescriptionError:
        parent.check()
        return PartsResult(joined_partials(partials, heading), provider, joined, False)
    return PartsResult(answer, provider, joined, True)
