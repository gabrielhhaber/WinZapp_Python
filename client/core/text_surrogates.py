"""Emoji that reach Python as two halves (UTF-16 surrogates) instead of one character.

On Windows a character outside the Basic Multilingual Plane (almost every
emoji) arrives at a ``wx.EVT_CHAR`` handler as TWO events, one per UTF-16
half: ``chr()`` of each gives a lone surrogate (U+D800-U+DFFF), not the emoji.
A handler that inserts ``chr(event.GetUnicodeKey())`` itself therefore puts
two loose halves into the field's value, and any slice by position (Backspace,
a mention replacing ``@query``) can cut between them. A lone surrogate cannot
be encoded: the Neonize bridge answered 500 to send-mentioned with
``UnicodeEncodeError: surrogates not allowed`` and the message never left.

``repair_surrogates`` turns adjacent halves back into the one character and
drops a half that lost its pair (it is not recoverable, and U+FFFD would reach
the recipient as a question mark). ``SurrogateJoiner`` keeps the first half of
an ``EVT_CHAR`` pair until the second arrives.

Pure functions, tested in tests/test_text_surrogates.py.
"""

_HIGH = range(0xD800, 0xDC00)
_LOW = range(0xDC00, 0xE000)


def has_surrogate(text) -> bool:
    """True when ``text`` holds a surrogate code point (paired or not).

    ASCII text (every base64 body: a status video, a voice note) answers at
    once, in C: a surrogate is never ASCII, and a Python loop over 21 million
    characters held the interpreter lock for about a second per post."""
    if not isinstance(text, str) or text.isascii():
        return False
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)


def repair_surrogates(text):
    """``text`` with each pair of halves joined into its character and each lone
    half removed. Anything that is not a ``str`` is returned unchanged."""
    if not has_surrogate(text):
        return text
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "ignore")


def repair_payload(value):
    """``repair_surrogates`` applied to every string of a JSON-like value (dicts,
    lists and tuples, keys included). Returns the same object when nothing
    needed repairing, so the common case costs one scan and no copy."""
    if isinstance(value, str):
        return repair_surrogates(value)
    if isinstance(value, dict):
        items = [(k, v, repair_payload(k), repair_payload(v)) for k, v in value.items()]
        if all(rk is k and rv is v for k, v, rk, rv in items):
            return value
        return {rk: rv for _, _, rk, rv in items}
    if isinstance(value, (list, tuple)):
        repaired = [repair_payload(v) for v in value]
        if all(a is b for a, b in zip(value, repaired)):
            return value
        return type(value)(repaired)
    return value


class SurrogateJoiner:
    """Turns the code units of ``EVT_CHAR`` events into whole characters.

    ``feed(code)`` returns the text to insert: ``""`` while it holds the first
    half of a pair, the joined character when the second half arrives, and the
    character itself for anything else. A first half followed by something
    that is not its second half is dropped."""

    def __init__(self):
        self._pending = None

    def feed(self, code: int) -> str:
        if code in _HIGH:
            self._pending = code
            return ""
        if code in _LOW:
            high, self._pending = self._pending, None
            if high is None:
                return ""
            return chr(0x10000 + ((high - 0xD800) << 10) + (code - 0xDC00))
        self._pending = None
        return chr(code)
