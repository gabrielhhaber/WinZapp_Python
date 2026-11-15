"""Who is typing or recording audio in a group, as one sentence.

Pure text: no wx, no window. The sentence is built from catalog keys instead of
a plural entry, because the translation API (``I18n.t()``) is singular-key only
and every locale decides its own verb agreement and list punctuation:

* ``group_typing_singular`` / ``group_typing_plural`` — "{names} is typing" /
  "{names} are typing";
* ``group_recording_singular`` / ``group_recording_plural`` — the same for
  "recording audio";
* ``group_activity_and`` — "{first} and {second}", ``group_activity_comma`` —
  "{first}, {second}": the two joiners, used both between names and between
  the typing clause and the recording clause.

"A, B and C are typing, D and E are recording": names joined by commas and a
final "and"; the two clauses joined by "and" only when the second one names a
single person, otherwise by a comma, so the sentence never reads "and ... and".
Typing always comes first. The one trailing ellipsis closes the whole sentence,
as ``typing_text`` does for a single person.
"""

COMPOSING = "composing"
RECORDING = "recording"


def _join(first: str, second: str, t, key: str) -> str:
    return t(key).format(first=first, second=second)


def join_names(names, t) -> str:
    """"A", "A and B", "A, B and C" in the locale's own words."""
    names = list(names)
    if not names:
        return ""
    result = names[0]
    for name in names[1:-1]:
        result = _join(result, name, t, "group_activity_comma")
    if len(names) > 1:
        result = _join(result, names[-1], t, "group_activity_and")
    return result


def _unique_named(entries, action):
    names = []
    for name, entry_action in entries:
        if entry_action == action and name and name not in names:
            names.append(name)
    return names


def group_activity_text(entries, t) -> str:
    """The sentence for *entries* — ``[(name, action), ...]``, action being
    "composing" or "recording" — or '' when nobody (with a name) is active."""
    typing = _unique_named(entries, COMPOSING)
    recording = _unique_named(entries, RECORDING)
    clauses = []
    for names, singular, plural in (
        (typing, "group_typing_singular", "group_typing_plural"),
        (recording, "group_recording_singular", "group_recording_plural"),
    ):
        if names:
            key = plural if len(names) > 1 else singular
            clauses.append((t(key).format(names=join_names(names, t)), len(names)))
    if not clauses:
        return ""
    text = clauses[0][0]
    if len(clauses) == 2:
        joiner = "group_activity_comma" if clauses[1][1] > 1 else "group_activity_and"
        text = _join(text, clauses[1][0], t, joiner)
    return text + "..."
