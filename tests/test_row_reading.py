"""The reading profiles of a message row (ui/conversation_panel/row_reading.py).

The default profile must give the row WinZapp always read: ``legacy_line()``
below is the assembly as _render_message_line() wrote it before the profiles
existed, and every combination of its pieces is compared against it.
"""

import itertools

import pytest

from ui.conversation_panel import row_reading as rr
from ui.conversation_panel.own_sender import row_lead


def legacy_line(p):
    """The old assembly, step for step (pieces joined by a space)."""
    if p.get("system"):
        pieces = [p["body"]]
    else:
        pieces = [row_lead(p["sender"], p["replying_to"], p["body"], p["hide_sender"])]
    if p.get("pinned"):
        pieces[0] = f"{p['pinned']}, {pieces[0]}"
    if p.get("starred_prefix"):
        pieces[0] = f"{p['starred_prefix']}, {pieces[0]}"
    if p.get("forwarded_prefix"):
        pieces[0] = f"{p['forwarded_prefix']}, {pieces[0]}"
    if p.get("comments"):
        pieces[0] += f", {p['comments']}"
    if p.get("time"):
        pieces.append(f", {p['time']}")
    if p.get("status"):
        pieces[-1] += f", {p['status']}"
    if p.get("edited"):
        pieces[-1] += f", {p['edited']}"
    if p.get("forwarded_suffix"):
        pieces[-1] += f", {p['forwarded_suffix']}"
    if p.get("starred_suffix"):
        pieces[-1] += f", {p['starred_suffix']}"
    if p.get("quoted"):
        pieces.append(p["quoted"])
    if p.get("reactions"):
        pieces.append(p["reactions"])
    if p.get("position"):
        pieces.append(p["position"])
    if p.get("upload"):
        pieces.append(p["upload"])
    return " ".join(pieces)


BASE = {"body": "See you at six", "sender": "Ana", "replying_to": "", "hide_sender": False,
        "system": False, "time": "10:30", "status": "Read"}

OPTIONAL = {
    "replying_to": "replying to Bob",
    "pinned": "\U0001F4CC Pinned",
    "starred_prefix": "Starred",
    "forwarded_prefix": "Forwarded",
    "comments": "3 comments",
    "edited": "Edited",
    "forwarded_suffix": "Forwarded",
    "starred_suffix": "Starred",
    "quoted": ", quoted message: Hello",
    "reactions": ". Reactions \U0001F44D, 1 total.",
    "position": ", 4 of 9",
    "upload": ", uploading 50%",
}


def all_variants():
    names = list(OPTIONAL)
    # Every pair plus each alone and all together: the full 2**12 product adds
    # nothing the pairs do not already show and is slow to read on failure.
    subsets = [()] + [(n,) for n in names] + list(itertools.combinations(names, 2)) + [tuple(names)]
    for subset in subsets:
        for hide, system, time, status in itertools.product(
                (False, True), (False, True), ("10:30", ""), ("Read", "")):
            parts = dict(BASE, hide_sender=hide, system=system, time=time, status=status)
            parts.update({n: OPTIONAL[n] for n in subset})
            yield parts


def test_default_profile_is_the_old_row_in_every_combination():
    count = 0
    for parts in all_variants():
        assert rr.compose_row_line(parts, rr.DEFAULT) == legacy_line(parts), parts
        count += 1
    assert count > 400


def test_default_is_the_row_of_today():
    assert rr.compose_row_line(BASE) == "Ana: See you at six , 10:30, Read"


def test_default_ignores_the_repeat_flags_and_the_limit():
    parts = dict(BASE)
    wanted = legacy_line(parts)
    assert rr.compose_row_line(parts, rr.DEFAULT, repeat_sender=True, repeat_time=True,
                               max_units=5) == wanted


@pytest.mark.parametrize("value", [None, "", "x", 3])
def test_unknown_profile_values_read_as_default(value):
    assert rr.reading_profile({"user_interface": {"message_reading_profile": value}}) == rr.DEFAULT
    assert rr.reading_profile({}) == rr.DEFAULT
    assert rr.reading_profile(None) == rr.DEFAULT


@pytest.mark.parametrize("profile", rr.PROFILES)
def test_a_known_profile_is_kept(profile):
    assert rr.reading_profile({"user_interface": {"message_reading_profile": profile}}) == profile


# ── Content first ────────────────────────────────────────────────────────────

def test_content_first_says_the_message_before_who_when_and_state():
    assert rr.compose_row_line(BASE, rr.CONTENT_FIRST) == "See you at six. Ana, 10:30, Read"


def test_content_first_does_not_double_the_end_of_a_sentence():
    parts = dict(BASE, body="Are you coming?")
    assert rr.compose_row_line(parts, rr.CONTENT_FIRST) == "Are you coming? Ana, 10:30, Read"


def test_content_first_keeps_the_reply_clause_with_the_sender():
    parts = dict(BASE, replying_to="replying to Bob")
    assert rr.compose_row_line(parts, rr.CONTENT_FIRST) == (
        "See you at six. Ana, replying to Bob, 10:30, Read")


def test_content_first_without_a_sender_keeps_the_reply_clause():
    parts = dict(BASE, hide_sender=True, replying_to="replying to Bob")
    assert rr.compose_row_line(parts, rr.CONTENT_FIRST) == (
        "See you at six. replying to Bob, 10:30, Read")
    assert rr.compose_row_line(dict(BASE, hide_sender=True), rr.CONTENT_FIRST) == (
        "See you at six. 10:30, Read")


def test_content_first_system_event_is_its_text_then_the_time():
    parts = dict(BASE, system=True, body="Carlos left the group", status="")
    assert rr.compose_row_line(parts, rr.CONTENT_FIRST) == "Carlos left the group. 10:30"


def test_content_first_honours_the_prefixes_the_user_asked_for():
    parts = dict(BASE, forwarded_prefix="Forwarded", starred_prefix="Starred",
                 pinned="\U0001F4CC Pinned", comments="2 comments")
    assert rr.compose_row_line(parts, rr.CONTENT_FIRST) == (
        "Forwarded, Starred, \U0001F4CC Pinned, See you at six, 2 comments. Ana, 10:30, Read")


def test_content_first_trailing_clauses_follow_the_time():
    parts = dict(BASE, edited="Edited", forwarded_suffix="Forwarded", starred_suffix="Starred")
    assert rr.compose_row_line(parts, rr.CONTENT_FIRST) == (
        "See you at six. Ana, 10:30, Read, Edited, Forwarded, Starred")


def test_content_first_keeps_quote_reactions_position_and_upload_after():
    parts = dict(BASE, quoted=", quoted message: Hello", reactions=". Reactions 1.",
                 position=", 4 of 9", upload=", uploading 50%")
    assert rr.compose_row_line(parts, rr.CONTENT_FIRST) == (
        "See you at six. Ana, 10:30, Read , quoted message: Hello . Reactions 1. "
        ", 4 of 9 , uploading 50%")


def test_content_first_message_alone_has_no_dangling_separator():
    parts = {"body": "Hi", "system": False, "sender": "", "replying_to": "", "hide_sender": True}
    assert rr.compose_row_line(parts, rr.CONTENT_FIRST) == "Hi"


def test_content_first_long_row_in_the_classic_list_keeps_the_default_order():
    """Past the 511 units the control reports, the sender, time and status
    would be cut off the end: the row keeps the default order instead."""
    parts = dict(BASE, body="x" * 600)
    line = rr.compose_row_line(parts, rr.CONTENT_FIRST, max_units=rr.CLASSIC_ROW_LIMIT)
    assert line == legacy_line(parts)
    assert line.startswith("Ana: ")


def test_content_first_long_row_in_the_listbox_has_no_limit():
    parts = dict(BASE, body="x" * 600)
    line = rr.compose_row_line(parts, rr.CONTENT_FIRST, max_units=None)
    assert line == "x" * 600 + ". Ana, 10:30, Read"


def test_content_first_counts_utf16_units_not_characters():
    # 200 emoji outside the BMP are 400 units: they fit with the tail; 300 do not.
    fits = dict(BASE, body="\U0001F600" * 200)
    assert rr.compose_row_line(fits, rr.CONTENT_FIRST, max_units=511).endswith(". Ana, 10:30, Read")
    over = dict(BASE, body="\U0001F600" * 300)
    assert rr.compose_row_line(over, rr.CONTENT_FIRST, max_units=511) == legacy_line(over)


def test_content_first_short_row_fitting_the_limit_is_reordered():
    line = rr.compose_row_line(BASE, rr.CONTENT_FIRST, max_units=rr.CLASSIC_ROW_LIMIT)
    assert line == "See you at six. Ana, 10:30, Read"


# ── Concise ──────────────────────────────────────────────────────────────────

def test_concise_without_repeats_is_the_default_row():
    for parts in all_variants():
        assert rr.compose_row_line(parts, rr.CONCISE) == legacy_line(parts)


def test_concise_drops_a_repeated_sender():
    assert rr.compose_row_line(BASE, rr.CONCISE, repeat_sender=True) == (
        "See you at six , 10:30, Read")


def test_concise_drops_a_repeated_time_and_keeps_the_status():
    assert rr.compose_row_line(BASE, rr.CONCISE, repeat_time=True) == "Ana: See you at six, Read"


def test_concise_drops_both():
    assert rr.compose_row_line(BASE, rr.CONCISE, repeat_sender=True, repeat_time=True) == (
        "See you at six, Read")


def test_concise_keeps_the_reply_clause_without_the_sender():
    parts = dict(BASE, replying_to="replying to Bob")
    assert rr.compose_row_line(parts, rr.CONCISE, repeat_sender=True) == (
        "replying to Bob: See you at six , 10:30, Read")


def test_concise_matches_the_old_row_with_the_repeats_applied_by_hand():
    for parts in all_variants():
        hand = dict(parts)
        hand["hide_sender"] = True
        hand["time"] = ""
        assert rr.compose_row_line(parts, rr.CONCISE, repeat_sender=True,
                                   repeat_time=True) == legacy_line(hand)


# ── Same sender ──────────────────────────────────────────────────────────────

def _msg(from_me=False, participant="", remote="5511@s.whatsapp.net"):
    key = {"fromMe": from_me, "remoteJid": remote}
    if participant:
        key["participant"] = participant
    return {"key": key}


def test_own_messages_are_the_same_sender():
    assert rr.same_sender(_msg(True), _msg(True))


def test_me_and_the_other_side_are_not():
    assert not rr.same_sender(_msg(True), _msg(False))


def test_one_to_one_other_side_is_the_same_sender():
    assert rr.same_sender(_msg(), _msg())


def test_group_participants_compare_by_jid():
    group = "1@g.us"
    a1 = _msg(participant="1@s.whatsapp.net", remote=group)
    a2 = _msg(participant="1@s.whatsapp.net", remote=group)
    b = _msg(participant="2@s.whatsapp.net", remote=group)
    assert rr.same_sender(a1, a2)
    assert not rr.same_sender(a1, b)


def test_group_message_without_participant_is_never_the_same():
    group = "1@g.us"
    assert not rr.same_sender(_msg(remote=group), _msg(remote=group))


def test_sentinels_and_non_messages_are_never_the_same():
    assert not rr.same_sender({"_type": "unread_separator"}, _msg())
    assert not rr.same_sender(_msg(), {"_type": "empty_placeholder"})
    assert not rr.same_sender(None, _msg())
    assert not rr.same_sender(_msg(), "x")


# ── The previous row, as the panel finds it ──────────────────────────────────

class _Panel:
    """Just what MessageRenderingMixin._repeats_previous_row() reads."""

    def __init__(self, rows):
        self._sorted_messages = rows

    @staticmethod
    def _extract_timestamp(msg):
        return msg.get("ts", 0)

    @staticmethod
    def _format_date(ts):
        return f"t{ts // 60}"

    @staticmethod
    def _is_system_event(msg):
        return msg.get("messageType") == "groupNotification"


def _repeats(rows, msg, index=None, system=False):
    from ui.conversation_panel.message_rendering import MessageRenderingMixin
    time_str = _Panel._format_date(msg["ts"]) if msg.get("ts") else ""
    return MessageRenderingMixin._repeats_previous_row(
        _Panel(rows), msg, index, time_str, system)


def _row(ts, from_me=False, **extra):
    row = _msg(from_me)
    row.update(ts=ts, **extra)
    return row


def test_previous_row_same_sender_and_minute():
    rows = [_row(60), _row(90)]
    assert _repeats(rows, rows[1], 1) == (True, True)
    assert _repeats(rows, rows[1]) == (True, True)          # index found by search


def test_previous_row_other_minute_keeps_the_time():
    rows = [_row(60), _row(130)]
    assert _repeats(rows, rows[1], 1) == (True, False)


def test_first_row_has_no_previous():
    rows = [_row(60)]
    assert _repeats(rows, rows[0], 0) == (False, False)


def test_row_after_the_unread_separator_keeps_sender_and_time():
    rows = [_row(60), {"_type": "unread_separator"}, _row(70)]
    assert _repeats(rows, rows[2], 2) == (False, False)


def test_row_after_a_group_notice_keeps_its_sender():
    rows = [_row(60, messageType="groupNotification"), _row(70)]
    assert _repeats(rows, rows[1], 1) == (False, True)


def test_a_message_not_listed_yet_follows_the_last_row():
    rows = [_row(60)]
    pending = _row(65)
    assert _repeats(rows, pending) == (True, True)


def test_a_system_event_never_repeats_a_sender():
    rows = [_row(60), _row(70, messageType="groupNotification")]
    assert _repeats(rows, rows[1], 1, system=True) == (False, True)


def test_previous_row_is_found_by_identity_not_dict_equality():
    class Boom(dict):
        def __eq__(self, other):
            raise AssertionError("rows were compared by equality")
        __hash__ = None

    rows = [Boom(_row(60)) for _ in range(5)]
    assert _repeats(rows, rows[3]) == (True, True)
    assert _repeats(rows, Boom(_row(65))) == (True, True)    # unlisted: after the last


# ── Rows outside the message list never lose their sender ────────────────────

def test_isolated_callers_ask_for_the_standalone_row():
    import inspect
    from ui.conversation_panel import (
        links, message_accels, message_rendering,
    )
    from ui.dialogs import conversation_data_dialog, pinned_messages_dialog
    for module in (links, message_accels, pinned_messages_dialog, conversation_data_dialog):
        assert "standalone=True" in inspect.getsource(module)
    src = inspect.getsource(message_rendering.MessageRenderingMixin._render_message_line)
    assert "profile == CONCISE and not standalone" in src


def test_list_appends_hand_over_their_real_index():
    import inspect
    from ui.conversation_panel import list_refresh
    src = inspect.getsource(list_refresh)
    assert "self._render_message_line(msg)" not in src
    assert "self._render_message_line(m))" not in src


# ── The "selected" marker fits inside the classic row limit ──────────────────

@pytest.mark.parametrize("position", ["start", "end"])
def test_selected_row_stays_within_the_classic_limit(position):
    from core.utils import append_selected_marker
    marker = "selecionada"
    parts = dict(BASE)
    parts["body"] = "x" * 400
    parts["sender"] = "Ana"
    parts["time"] = "10:00"
    parts["status"] = "Lida"
    # A body that fits bare (<= 511) but not with the marker added.
    for n in range(480, 511):
        parts["body"] = "y" * n
        limit = rr.CLASSIC_ROW_LIMIT - rr.selected_reserve(marker, position, True)
        line = rr.compose_row_line(parts, rr.CONTENT_FIRST, max_units=limit)
        full = append_selected_marker(line, marker, position, True)
        assert rr._units(full) <= rr.CLASSIC_ROW_LIMIT or line == rr.compose_row_line(
            parts, rr.DEFAULT)
        assert parts["time"] in line and parts["status"] in line


def test_selected_reserve_is_zero_when_not_selected():
    assert rr.selected_reserve("selecionada", "end", False) == 0
    assert rr.selected_reserve("selecionada", "end", True) == len(" selecionada")
