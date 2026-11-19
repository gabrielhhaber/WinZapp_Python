"""Whether a message's text calls on the whole group (@todos, @all, @everyone).

WhatsApp expands a mention of everyone into the full participant list in
``mentionedJid``, with nothing in the list telling it apart from naming each
person. The text still says it: the word stays a word ("@todos"), while a
person named on their own is written as ``@<digits>``. Reading the text
works before the group's participant list has been fetched, which the
count-based test in the conversation panel needs.
"""

import re

# Every locale's mention_all_keyword, plus the words WhatsApp itself writes.
KEYWORDS = frozenset({
    "all", "everyone", "todos", "tutti", "tutto", "tous", "alle",
    "wszyscy", "toți", "toti", "tümü", "herkes", "الكل",
})

_WORD = re.compile(r"(?<![\w@])@([^\W\d_][\w]*)")


def says_everyone(text: str, keyword: str = "") -> bool:
    """True when ``text`` has an @<word> that means the whole group.

    ``keyword`` is the current locale's own word, in case it is not listed.
    """
    if not text or "@" not in text:
        return False
    words = KEYWORDS | ({keyword.casefold()} if keyword else set())
    return any(word.casefold() in words for word in _WORD.findall(text))
