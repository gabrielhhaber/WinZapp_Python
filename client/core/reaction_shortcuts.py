"""Per-account, bounded history for the twelve quick message reactions."""

from collections import Counter


DEFAULT_QUICK_REACTIONS = (
    "❤️", "👍", "👎", "😂", "😮", "😢", "🙏", "🔥", "🎉", "💯", "😎", "🥰",
)
REACTION_HISTORY_LIMIT = 100
_DEFAULT_HEAD_START = 3


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


def quick_reactions(history, current: str = "") -> list[str]:
    """Rank recent choices; defaults yield a slot only after repeated use.

    The last hundred successful reactions adapt to changing habits. Defaults
    start with three virtual uses, so a new emoji does not displace one after
    a single exploratory pick. Always include the current reaction so its
    checked row remains available for removal, even if it is not in the top 12.
    """
    recent = _valid_history(history)
    counts = Counter(recent)
    defaults = {emoji: index for index, emoji in enumerate(DEFAULT_QUICK_REACTIONS)}
    last_used = {emoji: index for index, emoji in enumerate(recent)}
    choices = set(defaults) | set(counts)
    ranked = sorted(choices, key=lambda emoji: (
        -(counts[emoji] + (_DEFAULT_HEAD_START if emoji in defaults else 0)),
        0 if emoji in defaults else 1,
        defaults.get(emoji, len(defaults)),
        -last_used.get(emoji, -1),
    ))[:len(DEFAULT_QUICK_REACTIONS)]
    if current and current not in ranked:
        ranked[-1] = current
    return ranked
