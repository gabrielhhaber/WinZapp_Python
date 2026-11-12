"""WhatsApp events: a message with a name, a date, maybe a place and a call
link, that people answer "going", "maybe" or "not going".

Both engines answer the same routes (WPPConnect: api_patches
controller/eventController.ts over WhatsApp Web's own event actions;
Neonize: neonize_bridge/events.py):

* ``event-responses`` {messageIds} -> {id: {responses: [{sender, response,
  extraGuestCount, isMe}], mine}};
* ``event-respond`` {messageId, response} (1 going, 2 not going, 3 maybe);
* ``send-event`` {phone, name, description, location, startTime, endTime,
  callType ("voice", "video", null), extraGuestsAllowed} -> {id};
* ``edit-event`` {messageId, ...the fields that change} or {messageId,
  cancel: true}.

And the ``event-changed`` socket event {id, kind} when an answer arrives or
the event is edited or canceled. A received event is the canonical message
``{"messageType": "eventMessage", "message": {"eventMessage": {...}}}``
(EVENT_FIELDS); its answers are kept in ``eventMessage["responses"]`` the way
a poll keeps its votes.

Pure functions first (tests/test_events.py); the HTTP calls at the bottom.
"""

import datetime

from core.api_client import api_post
from core.locale_format import get_datetime_format, get_time_format

EVENT_MESSAGE_TYPE = "eventMessage"
WPP_TYPE = "event_creation"

GOING, NOT_GOING, MAYBE = 1, 2, 3
RESPONSES = (GOING, MAYBE, NOT_GOING)          # the order they are read in
CALL_TYPES = ("voice", "video")

#: What the canonical eventMessage carries.
EVENT_FIELDS = ("name", "description", "location", "joinLink", "startTime", "endTime",
                "isCanceled", "extraGuestsAllowed", "isScheduleCall", "hasReminder",
                "reminderOffsetSec")


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def location_text(value) -> str:
    """A place as WhatsApp sends it: a string, or {name, address, ...}."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        name = str(value.get("name") or "").strip()
        address = str(value.get("address") or "").strip()
        return ", ".join(p for p in (name, address) if p)
    return ""


def call_type_of(join_link) -> str:
    """"video", "voice" or "" for an event's call link (WhatsApp Web's
    getEventCallLinkType: "/video/" in the link is a video call)."""
    link = str(join_link or "")
    if not link:
        return ""
    return "video" if "/video/" in link else "voice"


def event_from_wpp(wpp_msg) -> dict:
    """The canonical eventMessage of a WhatsApp Web ``event_creation``."""
    m = wpp_msg if isinstance(wpp_msg, dict) else {}
    return {
        "name": str(m.get("eventName") or "").strip(),
        "description": str(m.get("eventDescription") or "").strip(),
        "location": location_text(m.get("eventLocation")),
        "joinLink": str(m.get("eventJoinLink") or ""),
        "startTime": _int(m.get("eventStartTime")),
        "endTime": _int(m.get("eventEndTime")),
        "isCanceled": bool(m.get("isEventCanceled") or m.get("eventInvalidated")),
        "extraGuestsAllowed": bool(m.get("eventExtraGuestsAllowed")),
        "isScheduleCall": bool(m.get("eventIsScheduledCall")),
        "hasReminder": bool(m.get("eventHasReminder")),
        "reminderOffsetSec": _int(m.get("eventReminderOffsetSec")),
    }


def event_content(message) -> dict:
    """The eventMessage of a record's ``message``, or {}."""
    content = (message or {}).get(EVENT_MESSAGE_TYPE) if isinstance(message, dict) else None
    return content if isinstance(content, dict) else {}


def _when(seconds, i18n, today=None) -> str:
    try:
        moment = datetime.datetime.fromtimestamp(seconds)
    except (ValueError, OSError, OverflowError, TypeError):
        return ""
    return moment.strftime(get_datetime_format(i18n.t("datetime_fmt")))


def time_text(event, i18n) -> str:
    """"start" or "start until end"; the end shows only the hour on the same day."""
    start, end = _int(event.get("startTime")), _int(event.get("endTime"))
    if not start:
        return ""
    first = _when(start, i18n)
    if not end or end <= start:
        return first
    try:
        same_day = (datetime.datetime.fromtimestamp(start).date()
                    == datetime.datetime.fromtimestamp(end).date())
        last = (datetime.datetime.fromtimestamp(end).strftime(get_time_format(i18n.t("time_fmt")))
                if same_day else _when(end, i18n))
    except (ValueError, OSError, OverflowError):
        return first
    return i18n.t("event_time_range").format(start=first, end=last)


def counts(responses) -> dict:
    """{GOING: n, MAYBE: n, NOT_GOING: n}; "going" also counts the guests."""
    out = {GOING: 0, MAYBE: 0, NOT_GOING: 0}
    for r in responses or ():
        kind = _int((r or {}).get("response"))
        if kind in out:
            out[kind] += 1 + (_int(r.get("extraGuestCount")) if kind == GOING else 0)
    return out


def my_response(event) -> int:
    for r in event.get("responses") or ():
        if (r or {}).get("isMe"):
            return _int(r.get("response"))
    return _int(event.get("mine"))


def is_past(event, now) -> bool:
    """WhatsApp Web's shouldShowEventAsPassed: past its end, or its start
    when it has none."""
    end = _int(event.get("endTime")) or _int(event.get("startTime"))
    return bool(end) and end <= now


def event_text(event, i18n, now) -> str:
    """The message row of an event: what, when, where, how, and who goes."""
    t = i18n.t
    parts = [f"{t('event_label')}: {event.get('name') or t('event_unnamed')}"]
    if event.get("isCanceled"):
        parts.append(t("event_canceled"))
    elif is_past(event, now):
        parts.append(t("event_passed"))
    when = time_text(event, i18n)
    if when:
        parts.append(when)
    if event.get("location"):
        parts.append(t("event_location").format(location=event["location"]))
    call = call_type_of(event.get("joinLink"))
    if call:
        parts.append(t("event_call_video" if call == "video" else "event_call_voice"))
    if event.get("description"):
        parts.append(event["description"])
    if "responses" in event:
        c = counts(event.get("responses"))
        parts.append(t("event_responses_summary").format(
            going=c[GOING], maybe=c[MAYBE], not_going=c[NOT_GOING]))
        mine = my_response(event)
        if mine:
            parts.append(t(RESPONSE_MINE_KEYS[mine]))
    return ". ".join(p.rstrip(".") for p in parts if p) + "."


RESPONSE_KEYS = {GOING: "event_response_going", MAYBE: "event_response_maybe",
                 NOT_GOING: "event_response_not_going"}
RESPONSE_GROUP_KEYS = {GOING: "event_group_going", MAYBE: "event_group_maybe",
                       NOT_GOING: "event_group_not_going"}
RESPONSE_MINE_KEYS = {GOING: "event_mine_going", MAYBE: "event_mine_maybe",
                      NOT_GOING: "event_mine_not_going"}


def apply_responses(event, data) -> bool:
    """Keep an ``event-responses`` answer in the eventMessage; True when it
    changed what the row says."""
    if not isinstance(data, dict):
        return False
    responses = [r for r in data.get("responses") or () if isinstance(r, dict)
                 and _int(r.get("response")) in RESPONSES]
    before = (event.get("responses"), event.get("mine"))
    event["responses"] = responses
    event["mine"] = _int(data.get("mine"))
    return before != (event["responses"], event["mine"])


def apply_edit(event, fresh) -> bool:
    """Put an edited event's fields (event_from_wpp of the message read
    again) into the kept eventMessage, its answers untouched; True when the
    row changes."""
    if not isinstance(fresh, dict) or not fresh.get("name"):
        return False
    changed = False
    for field in EVENT_FIELDS:
        if field in fresh and event.get(field) != fresh[field]:
            event[field] = fresh[field]
            changed = True
    return changed


def parse_moment(date_text, time_text, date_fmt, time_fmt):
    """Seconds since the epoch of a local date and time typed in the app's
    formats (i18n "date_fmt", "time_fmt"), or None when either does not read.
    An empty time is midnight only when *time_text* is None (no field)."""
    date_text, time_text = str(date_text or "").strip(), str(time_text or "").strip()
    if not date_text:
        return None
    try:
        day = datetime.datetime.strptime(date_text, date_fmt)
        hour = datetime.datetime.strptime(time_text, time_fmt) if time_text else None
    except ValueError:
        return None
    if hour is None:
        return None
    moment = day.replace(hour=hour.hour, minute=hour.minute, second=0, microsecond=0)
    try:
        return int(moment.timestamp())
    except (OverflowError, OSError, ValueError):
        return None


def format_moment(seconds, date_fmt, time_fmt):
    """(date text, time text) of *seconds* in the app's formats; ("", "") for 0."""
    if not seconds:
        return "", ""
    try:
        moment = datetime.datetime.fromtimestamp(seconds)
    except (ValueError, OSError, OverflowError):
        return "", ""
    return moment.strftime(date_fmt), moment.strftime(time_fmt)


def default_start(now) -> int:
    """The start a new event is offered with: the next full hour."""
    return (int(now) // 3600 + 1) * 3600


def validate(name, start, end, now) -> str:
    """The i18n key of what is wrong with a new or edited event, or ""."""
    if not str(name or "").strip():
        return "event_name_required"
    if not start:
        return "event_start_required"
    if end and end < start:
        return "event_end_before_start"
    return ""


def can_answer(event, from_me, now) -> bool:
    """WhatsApp Web's shouldDisableInteractionForEvent: not a canceled one,
    and not a past one unless it is mine."""
    if event.get("isCanceled"):
        return False
    return from_me or not is_past(event, now)


# ------------------------------------------------------------------ HTTP
def _post(connection, endpoint, payload, timeout=30):
    server, port, token = connection
    response = api_post(f"{server}:{port}/api/{token}/{endpoint}", json=payload,
                        headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or not isinstance(body, dict):
        code = body.get("code") if isinstance(body, dict) else ""
        raise ValueError(code or f"http_{response.status_code}")
    return body


def read_event(connection, message_id) -> dict:
    """The eventMessage of the event message read again (after an edit), or {}."""
    from urllib.parse import quote
    from core.api_client import api_get
    server, port, token = connection
    response = api_get(f"{server}:{port}/api/{token}/message-by-id/{quote(message_id, safe='@_.:')}",
                       token=token, timeout=15)
    if response.status_code >= 400:
        return {}
    try:
        body = response.json()
    except ValueError:
        return {}
    data = ((body or {}).get("response") or {}).get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or data.get("type") != WPP_TYPE:
        return {}
    return event_from_wpp(data)


def read_responses(connection, message_ids) -> dict:
    body = _post(connection, "event-responses", {"messageIds": list(message_ids)[:50]}, 15)
    data = body.get("response")
    return data if isinstance(data, dict) else {}


def respond(connection, message_id, response) -> None:
    _post(connection, "event-respond", {"messageId": message_id, "response": int(response)})


def create(connection, chat_jid, fields) -> str:
    body = _post(connection, "send-event", {"phone": chat_jid, **fields}, 45)
    return str((body.get("response") or {}).get("id") or "")


def edit(connection, message_id, fields) -> None:
    _post(connection, "edit-event", {"messageId": message_id, **fields}, 45)


def cancel(connection, message_id) -> None:
    _post(connection, "edit-event", {"messageId": message_id, "cancel": True}, 45)
