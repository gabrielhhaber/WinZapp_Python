"""How a message row is put together, and the reading profile that orders it
(Settings > Interface > Messages, ``user_interface.message_reading_profile``).

Pure, no wx: ``_render_message_line()`` gathers the pieces of a row (sender,
body, time, status, ...) and ``compose_row_line()`` joins them.

* ``default`` is the row WinZapp always read: sender, body, time, status.
  Nothing about it changes, and tests/test_row_reading.py keeps a copy of the
  old assembly to prove it.
* ``content_first`` puts the message before who sent it, when and its state, so
  the reader says the content at once. The "starred / forwarded / pinned at the
  start" settings are still honoured: the user asked to hear those first. A
  classic ListView row is cut at 511 UTF-16 units by SysListView32; a long row
  would lose its sender, time and status in that cut, so when the content-first
  line does not fit the row keeps the default order (the sender survives, and
  "Ler mais" reads what was cut, as for any long row).
* ``concise`` drops the sender when the previous message came from the same
  person and the time when it equals the previous message's, to spare the
  listener the repetition.
"""

from ui.conversation_panel.own_sender import row_lead

DEFAULT = "default"
CONTENT_FIRST = "content_first"
CONCISE = "concise"
#: In the order the Settings choice lists them.
PROFILES = (DEFAULT, CONTENT_FIRST, CONCISE)

SETTING = "message_reading_profile"

# Same unit the messages list uses (see message_rows._LIST_CTRL_TEXT_UNITS).
CLASSIC_ROW_LIMIT = 511


def reading_profile(settings) -> str:
    """The chosen profile; anything missing or unknown reads as the default."""
    ui = settings.get("user_interface") if isinstance(settings, dict) else None
    value = ui.get(SETTING, DEFAULT) if isinstance(ui, dict) else DEFAULT
    return value if value in PROFILES else DEFAULT


def same_sender(previous, msg) -> bool:
    """Whether two message records come from the same person, by identity.

    Conservative: a sender known under two JID forms (@lid and phone) counts
    as two, which only keeps the name in the row. In a group a message with no
    participant is never "the same"; in a one-to-one chat the other side is.
    """
    if not isinstance(previous, dict) or not isinstance(msg, dict):
        return False
    if previous.get("_type") or msg.get("_type"):
        return False
    key_p, key_m = previous.get("key") or {}, msg.get("key") or {}
    if bool(key_p.get("fromMe")) != bool(key_m.get("fromMe")):
        return False
    if key_m.get("fromMe"):
        return True
    who_p = key_p.get("participant") or previous.get("participant") or ""
    who_m = key_m.get("participant") or msg.get("participant") or ""
    if who_p or who_m:
        return who_p == who_m
    return not str(key_m.get("remoteJid") or "").endswith("@g.us")


def _units(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def selected_reserve(marker: str, position: str, is_selected: bool) -> int:
    """Units the "selected" marker (and its space) adds to a row after it is
    composed, to be taken off the row limit so the marker cannot push the
    time and status past the cut."""
    if not is_selected:
        return 0
    return _units(f"{marker} " if position == "start" else f" {marker}")


def _end_of_sentence(text: str) -> bool:
    return text.rstrip().endswith((".", "!", "?", "…", ":"))


def _wrap_prefixes(first: str, parts: dict) -> str:
    """The words the user asked to hear at the very start, outermost last:
    pinned, then starred, then forwarded (the order the row always used)."""
    if parts.get("pinned"):
        first = f"{parts['pinned']}, {first}"
    if parts.get("starred_prefix"):
        first = f"{parts['starred_prefix']}, {first}"
    if parts.get("forwarded_prefix"):
        first = f"{parts['forwarded_prefix']}, {first}"
    if parts.get("comments"):
        first += f", {parts['comments']}"
    return first


def _trailing(parts: dict) -> list:
    """What follows the time, as separate clauses."""
    return [parts.get(k) for k in ("status", "edited", "forwarded_suffix", "starred_suffix")
            if parts.get(k)]


def _tail_pieces(parts: dict) -> list:
    """The pieces after the lead: quoted preview, reactions, position, upload."""
    return [parts[k] for k in ("quoted", "reactions", "position", "upload") if parts.get(k)]


def _default_line(parts: dict, hide_sender: bool, time_str: str) -> str:
    if parts.get("system"):
        first = parts["body"]
    else:
        first = row_lead(parts.get("sender", ""), parts.get("replying_to", ""),
                         parts["body"], hide_sender)
    # The prefixes wrap the lead, the comment count follows it.
    first = _wrap_prefixes(first, parts)
    pieces = [first]
    if time_str:
        pieces.append(f", {time_str}")
    suffix = "".join(f", {clause}" for clause in _trailing(parts))
    pieces[-1] += suffix
    return " ".join(pieces + _tail_pieces(parts))


def _content_first_line(parts: dict, time_str: str) -> str:
    first = _wrap_prefixes(parts["body"], parts)
    header = ""
    if not parts.get("system") and not parts.get("hide_sender"):
        header = parts.get("sender", "")
        if parts.get("replying_to"):
            header = f"{header}, {parts['replying_to']}" if header else parts["replying_to"]
    elif not parts.get("system") and parts.get("replying_to"):
        header = parts["replying_to"]
    tail = ", ".join(x for x in [header, time_str, *_trailing(parts)] if x)
    if tail:
        first += (" " if _end_of_sentence(first) else ". ") + tail
    return " ".join([first] + _tail_pieces(parts))


def compose_row_line(parts: dict, profile: str = DEFAULT, *, repeat_sender: bool = False,
                     repeat_time: bool = False, max_units: int | None = None) -> str:
    """The row text, before the "selected" marker.

    *parts* holds finished strings, empty when absent: ``body`` (always),
    ``sender``, ``replying_to``, ``hide_sender`` (bool), ``system`` (bool: the
    body alone leads), ``pinned``, ``starred_prefix``, ``forwarded_prefix``,
    ``comments``, ``time``, ``status``, ``edited``, ``forwarded_suffix``,
    ``starred_suffix``, ``quoted``, ``reactions``, ``position``, ``upload``.
    ``repeat_sender`` / ``repeat_time`` say the previous message had the same
    sender / time (only the concise profile acts on them). *max_units* is the
    classic list's row limit, or None for the listbox.
    """
    hide = bool(parts.get("hide_sender"))
    time_str = parts.get("time", "")
    if profile == CONTENT_FIRST:
        line = _content_first_line(parts, time_str)
        if max_units is None or _units(line) <= max_units:
            return line
        return _default_line(parts, hide, time_str)
    if profile == CONCISE:
        hide = hide or repeat_sender
        if repeat_time:
            time_str = ""
    return _default_line(parts, hide, time_str)
