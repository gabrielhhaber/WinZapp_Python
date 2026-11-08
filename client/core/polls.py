"""Polls (enquetes): validation of a new poll, and reading a received one.

Pure logic with no wx, so the dialog, the sender and the message list share one
copy and tests call it directly.

WhatsApp Web keeps a poll as a message of type ``poll_creation`` with
``pollName``, ``pollOptions`` (``[{name, localId}]``) and
``pollSelectableOptionsCount`` (0 = any number of options, 1 = a single one);
``WebSocketClient._normalize_wpp_message`` turns it into
``{"pollCreationMessage": {"name", "options", "selectableOptionsCount"}}``.
"""

MIN_OPTIONS = 2
MAX_OPTIONS = 12

POLL_MESSAGE_TYPES = (
    "pollCreationMessage",
    "pollCreationMessageV2",
    "pollCreationMessageV3",
)


def poll_message_from_wpp(wpp_msg: dict) -> dict:
    """The ``pollCreationMessage`` payload for a WPPConnect ``poll_creation``."""
    options = []
    for opt in wpp_msg.get("pollOptions") or []:
        name = opt.get("name") if isinstance(opt, dict) else opt
        if name:
            option = {"name": str(name)}
            if isinstance(opt, dict) and isinstance(opt.get("localId"), int):
                option["localId"] = opt["localId"]
            options.append(option)
    poll = {
        "name": wpp_msg.get("pollName") or wpp_msg.get("body") or "",
        "options": options,
    }
    if wpp_msg.get("pollSelectableOptionsCount") is not None:
        try:
            poll["selectableOptionsCount"] = int(wpp_msg["pollSelectableOptionsCount"])
        except (TypeError, ValueError):
            pass
    return poll


def clean_poll_input(question: str, options) -> tuple[str, list[str]]:
    """The question and options as they are sent: trimmed, blanks dropped."""
    cleaned = [str(opt).strip() for opt in (options or [])]
    return (question or "").strip(), [opt for opt in cleaned if opt]


def poll_input_error(question: str, options) -> str | None:
    """The i18n key of what is wrong with a new poll, or None when it can go.

    Duplicate options are compared ignoring case, because WhatsApp refuses a
    poll whose options are not distinct and a reader cannot tell "Sim" from
    "sim" apart anyway.
    """
    question, options = clean_poll_input(question, options)
    if not question:
        return "poll_error_no_question"
    if len(options) < MIN_OPTIONS:
        return "poll_error_too_few_options"
    if len(options) > MAX_OPTIONS:
        return "poll_error_too_many_options"
    if len({opt.casefold() for opt in options}) != len(options):
        return "poll_error_duplicate_option"
    return None


def selectable_count(multiple: bool) -> int:
    """``selectableCount`` for the send: 1 for a single answer, 0 for any."""
    return 0 if multiple else 1


def poll_content(msg_obj: dict) -> dict:
    """The poll payload of a message's ``message`` dict, or {}."""
    if not isinstance(msg_obj, dict):
        return {}
    for key in POLL_MESSAGE_TYPES:
        poll = msg_obj.get(key)
        if isinstance(poll, dict):
            return poll
    return {}


def poll_options(poll: dict) -> list[dict]:
    """The options as ``[{"name": str, "votes": int | None}]``, blanks dropped.

    ``votes`` is None when no vote data reached WinZapp for the option: that is
    "unknown", never 0, because a spoken "votes: 0" on a poll that has votes is
    a false count a screen-reader user cannot notice. An option may be a bare
    string.
    """
    result = []
    for opt in (poll or {}).get("options") or []:
        votes = None
        if isinstance(opt, dict):
            name = str(opt.get("name") or opt.get("optionName") or "").strip()
            local_id = opt.get("localId")
            mine = bool(opt.get("mine"))
            if opt.get("votes") is not None:
                try:
                    votes = max(0, int(opt["votes"]))
                except (TypeError, ValueError):
                    votes = None
        else:
            name = str(opt or "").strip()
            local_id, mine = None, False
        if name:
            result.append({
                "name": name, "votes": votes, "mine": mine,
                # WhatsApp numbers options 0..n-1 in the order they were given.
                "localId": local_id if isinstance(local_id, int) else len(result),
            })
    return result


def allows_multiple(poll: dict):
    """True when a voter may pick several options, False for one, None when
    the poll did not say (the field is absent)."""
    value = (poll or {}).get("selectableOptionsCount")
    if value is None:
        return None
    try:
        return int(value) != 1
    except (TypeError, ValueError):
        return None


def options_by_votes(options: list[dict]) -> list[dict]:
    """Most voted first; equal counts keep the order the author gave them."""
    return sorted(options, key=lambda opt: -(opt["votes"] or 0))


def option_label(i18n, option: dict, percent: int | None = None) -> str:
    """One option as spoken: its name, then its votes when they are known and,
    when *percent* is given, its share of all votes.

    Only exactly 1 is singular: "0 votes", "1 vote", "2 votes".
    """
    if option["votes"] is None:
        return option["name"]
    one = option["votes"] == 1
    if percent is None:
        key = "poll_option_vote_one" if one else "poll_option_votes"
    else:
        key = "poll_option_vote_one_percent" if one else "poll_option_votes_percent"
    return i18n.t(key).format(name=option["name"], count=option["votes"], percent=percent)


def poll_row_text(i18n, poll: dict) -> str:
    """The message-list row: the question first, then every option and votes.

    Same order as WhatsApp's own desktop: question, then the options with the
    most votes first. A poll that arrived without a question or options still
    reads as a poll rather than as an unsupported message.
    """
    name = (poll or {}).get("name") or ""
    options = options_by_votes(poll_options(poll))
    if not name and not options:
        return i18n.t("notif_poll_no_name")
    head = i18n.t("poll_row_question").format(name=name)
    if not options:
        return head
    listed = "; ".join(option_label(i18n, opt) for opt in options)
    return i18n.t("poll_row_options").format(question=head, options=listed)


def poll_send_payload(phone: str, poll: dict, *, is_group: bool, is_lid: bool) -> dict:
    """The ``/send-poll-message`` body for one poll to one destination."""
    payload = {
        "phone": [phone],
        "isGroup": is_group,
        "isLid": is_lid,
        "name": poll["name"],
        "choices": list(poll["options"]),
        "options": {"selectableCount": poll.get("selectableCount", 1)},
    }
    if str(phone).endswith("@newsletter"):
        payload["isNewsletter"] = True      # a channel (contactToArray)
    return payload


def vote_percent(option: dict, options: list[dict]) -> int | None:
    """The option's share of all votes, 0-100; None while votes are unknown."""
    if option["votes"] is None:
        return None
    total = sum(opt["votes"] or 0 for opt in options)
    return round(100 * option["votes"] / total) if total else 0


def apply_vote_result(poll: dict, result: dict) -> bool:
    """Write the votes WhatsApp Web reported for *poll* into it, in place.

    *result* is one entry of ``/poll-votes``: ``{"voters", "options": [{name,
    localId, votes, mine}]}``. Returns whether anything changed, so callers
    repaint only then. Options are matched by name, so a result for another
    poll can never overwrite this one.
    """
    reported = [
        opt for opt in (result or {}).get("options") or []
        if isinstance(opt, dict) and isinstance(opt.get("votes"), int)
    ]
    if not poll or [o.get("name") for o in reported] != [o["name"] for o in poll_options(poll)]:
        return False
    new = [
        {"name": o["name"], "localId": o.get("localId"), "votes": o["votes"],
         "mine": bool(o.get("mine"))}
        for o in reported
    ]
    if new == poll.get("options"):
        return False
    poll["options"] = new
    return True


def vote_poll_ids(options: list[dict], checked_names: list[str]) -> list[int]:
    """The ``localId`` of every checked option, for ``/vote-poll``."""
    chosen = set(checked_names)
    return [opt["localId"] for opt in options if opt["name"] in chosen]


def voter_rows(i18n, voters: list, options: list, name_of) -> list[str]:
    """The "View votes" lines, ``"<person>: <option>[, <option>]"``, one per
    person who has a vote, ordered by name.

    *voters* is ``[{"sender", "mine", "localIds"}]`` as ``/poll-voters``
    returns it, *options* ``[{"name", "localId"}]``, and *name_of(sender)* gives
    the name a person is shown under (never a raw JID). The user's own vote
    reads as "You".
    """
    names = {o.get("localId"): str(o.get("name") or "") for o in options or []}
    rows = []
    for voter in voters or []:
        chosen = [names[i] for i in voter.get("localIds") or [] if names.get(i)]
        if not chosen:
            continue
        who = i18n.t("sender_you") if voter.get("mine") else (
            name_of(voter.get("sender", "")) or str(voter.get("name") or "").strip()
            or i18n.t("unknown_contact"))
        rows.append((who, i18n.t("poll_voter_row").format(name=who, options=", ".join(chosen))))
    rows.sort(key=lambda pair: pair[0].casefold())
    return [text for _who, text in rows]
