"""Per-account, bounded history for the twelve quick message reactions."""

from collections import Counter


DEFAULT_QUICK_REACTIONS = (
    "❤️", "👍", "👎", "😂", "😮", "😢", "🙏", "🔥", "🎉", "💯", "😎", "🥰",
)
REACTION_HISTORY_LIMIT = 100


def _valid_history(history) -> list[str]:
    if not isinstance(history, list):
        return []
    return [emoji for emoji in history[-REACTION_HISTORY_LIMIT:]
            if isinstance(emoji, str) and emoji and len(emoji) <= 32
            and not any(char.isspace() for char in emoji)]


def remember_reaction(history, emoji: str) -> list[str]:
    """Record a successful, non-removal reaction without unbounded growth."""
    recent = _valid_history(history)
    if isinstance(emoji, str) and emoji:
        recent.append(emoji)
    return recent[-REACTION_HISTORY_LIMIT:]


def fixed_quick_reactions(slots) -> list[str]:
    """The twelve rows chosen in Settings > Reactions, in order.

    Anything unusable (a hand-edited file, a wrong length, a repeated emoji)
    falls back to the defaults as a whole rather than half-applying, so the
    rows a user counts on are either all theirs or all the shipped ones.
    """
    if not isinstance(slots, list) or len(slots) != len(DEFAULT_QUICK_REACTIONS):
        return list(DEFAULT_QUICK_REACTIONS)
    valid = _valid_history(slots)
    if len(valid) != len(slots) or len(set(valid)) != len(valid):
        return list(DEFAULT_QUICK_REACTIONS)
    return valid


def assign_quick_reaction(slots, position: int, emoji: str) -> list[str]:
    """Put *emoji* on row *position*; if another row already holds it, the
    two rows swap, so every row stays a distinct reaction."""
    rows = fixed_quick_reactions(slots)
    if not (0 <= position < len(rows)) or not _valid_history([emoji]):
        return rows
    if emoji in rows:
        other = rows.index(emoji)
        rows[other] = rows[position]
    rows[position] = emoji
    return rows


def _ranked_by_use(history) -> list[str]:
    recent = _valid_history(history)
    counts = Counter(recent)
    defaults = {emoji: index for index, emoji in enumerate(DEFAULT_QUICK_REACTIONS)}
    last_used = {emoji: index for index, emoji in enumerate(recent)}
    choices = set(defaults) | set(counts)
    # Any reaction already used outranks a default never used, so a custom
    # reaction joins the list the first time it is sent (issue #852); among
    # equal counts the more recent one is first, and unused defaults keep
    # their shipped order at the tail.
    return sorted(choices, key=lambda emoji: (
        -counts[emoji],
        -last_used.get(emoji, -1),
        defaults.get(emoji, len(defaults)),
    ))[:len(DEFAULT_QUICK_REACTIONS)]


def quick_reaction_at(slots, digit: int) -> str:
    """The reaction a quick-reaction shortcut sends: digit 1..9 is the
    configured row 1..9 and 0 the tenth, whatever the usage order."""
    rows = fixed_quick_reactions(slots)
    return rows[9 if digit == 0 else digit - 1]


def reaction_to_send(chosen: str, current: str) -> str:
    """What activating a row sends: the emoji, or "" (remove my reaction) when
    it is exactly the one already sent. Another skin tone of it is another
    reaction, and replaces it."""
    return "" if chosen == current else chosen


def quick_reactions(history, current: str = "", *, fixed_slots=None) -> list[str]:
    """Rank recent choices; a reaction used even once is listed.

    The last hundred successful reactions adapt to changing habits. A custom
    reaction takes the place of the last default nobody has used. Always include the current reaction so its
    checked row remains available for removal, even if it is not in the top 12
    (with fixed rows it gets a thirteenth row instead, so no configured row
    is ever replaced).

    The twelve are listed most-used first. With ``fixed_slots`` (Settings >
    Reactions, for users who pick a reaction by counting arrow presses) the
    rows are exactly the configured ones and history is ignored.
    """
    if fixed_slots is not None:
        ranked = fixed_quick_reactions(fixed_slots)
        if current and current not in ranked:
            ranked.append(current)
        return ranked
    ranked = _ranked_by_use(history)
    if current and current not in ranked:
        ranked[-1] = current
    return ranked
