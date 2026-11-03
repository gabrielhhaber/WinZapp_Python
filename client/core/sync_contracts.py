"""Response contracts for the sync endpoints, checked on arrival in Python.

This is the mirror of `client/api_patches/src/dto/sync.ts`. The Node side
observes what it is about to send; this side observes what actually arrived —
and the two are not the same check. Between them sit an HTTP hop, a JSON
round-trip and the `@lid`/phone bridging, so a payload can leave the API valid
and reach here missing the one field the caller needed.

WHY THIS EXISTS

Every consumer in WinZapp reads these payloads with `.get(name, default)`. That
is the right way to survive a chaotic upstream, but it has one cost: a field
that stops arriving is indistinguishable from a field that was never there, and
the code carries on with the default. The failure surfaces much later, wearing
a different face — the canonical case being WhatsApp Web renaming MsgKey's
`_serialized` getter, which arrived here as `id: None`, normalised to `""`, and
made DatabaseManager drop 100% of messages as id-less. Nothing logged anything;
the visible symptom was a chat list with unread counts over an empty database.

WHAT IT DOES, AND DELIBERATELY DOES NOT DO

Observation only. `observe_payload()` returns its input **unchanged** and never
raises: nothing is rejected, coerced, filled in or stripped. The single effect
is a `[contract]` line in log.log naming the endpoint and the field. Turning a
mismatch into a failure would let a new optional field from WhatsApp Web break
sync for people who depend on this app daily, which costs far more than the
stale value it would prevent.

Unknown fields are never reported. These contracts describe the minimum WinZapp
reads, never the full shape WhatsApp Web sends — the same reason every schema in
sync.ts is `.passthrough()`.

REPEATS ARE SUPPRESSED

A sync walks thousands of messages. If a field is missing from one it is almost
certainly missing from all of them, so each distinct (endpoint, field, problem)
is logged once per run and counted thereafter — a log the user is expected to
send us must stay readable. `contract_report()` returns the tally.
"""

import logging
import threading

# (name -> accepted types). A field is "required" when its absence is what
# silently destroys the record downstream; everything else degrades into a
# less useful record rather than a discarded one, and is optional here even
# when it is almost always present.
_MESSAGE_REQUIRED = {"id": (str, dict)}
_MESSAGE_OPTIONAL = {
    "from": (str,),
    "to": (str,),
    "fromMe": (bool, str),
    "isStatus": (bool,),
    "type": (str,),
    "t": (int,),
    "timestamp": (int,),
    "body": (str,),
    "caption": (str,),
    "mimetype": (str,),
    "mediaKey": (str,),
    "clientUrl": (str,),
    "isPtt": (bool,),
    "isGif": (bool,),
}

_CHAT_REQUIRED = {"id": (str, dict)}
_CHAT_OPTIONAL = {
    "name": (str,),
    "isGroup": (bool,),
    "unreadCount": (int,),
    "t": (int,),
    "archive": (bool,),
    # Polymorphic in practice, which is why both consumers
    # (get_remote_chats() in main.py and WebSocketClient.on_chats_update)
    # parse it three ways: WhatsApp Web sends the pin TIMESTAMP in ms
    # (1783718891426, confirmed on a live list-chats), older/other paths send a
    # bool, and some send the string "true"/"false". Typing it bool — as this
    # contract first did, under the wrong name "pinned" — described a field
    # that never arrives, and a modelled field that never arrives is never
    # checked. It cost nothing and told us nothing.
    "pin": (bool, int, float, str),
    "msgs": (list,),
}

_CONTACT_REQUIRED = {"id": (str, dict)}
_CONTACT_OPTIONAL = {
    "name": (str,),
    "pushname": (str,),
    "shortName": (str,),
    "isMyContact": (bool,),
}

_STATUS_REQUIRED = {"status": (str,)}
_STATUS_OPTIONAL = {
    "syncing": (bool,),
    "qrcode": (str,),
    "urlcode": (str,),
    "session": (str,),
}

CONTRACTS = {
    "get-messages": (_MESSAGE_REQUIRED, _MESSAGE_OPTIONAL),
    "list-chats": (_CHAT_REQUIRED, _CHAT_OPTIONAL),
    "all-contacts": (_CONTACT_REQUIRED, _CONTACT_OPTIONAL),
    "status-session": (_STATUS_REQUIRED, _STATUS_OPTIONAL),
}

# (endpoint, field, problem) already written to the log, and how many times it
# has been seen since. Guarded because messages are normalized on the Socket.IO
# thread while the sync walks chats on its own workers.
_seen_lock = threading.Lock()
_seen: dict[tuple[str, str, str], int] = {}


def _type_names(types_) -> str:
    return "/".join(t.__name__ for t in types_)


def _check(record: dict, endpoint: str) -> list[tuple[str, str]]:
    """(field, problem) pairs for one record. Empty when it satisfies the contract."""
    required, optional = CONTRACTS[endpoint]
    problems: list[tuple[str, str]] = []

    for name, types_ in required.items():
        if name not in record or record[name] is None:
            problems.append((name, f"missing (expected {_type_names(types_)})"))
        elif not isinstance(record[name], types_):
            problems.append(
                (name, f"expected {_type_names(types_)}, got "
                       f"{type(record[name]).__name__}"))

    for name, types_ in optional.items():
        value = record.get(name)
        # `None` is how WhatsApp Web says "not set" for almost every optional
        # field, and is never a finding. `bool` is a subclass of `int`, so an
        # int-typed field would silently accept True without the explicit
        # exclusion below.
        if value is None:
            continue
        if isinstance(value, bool) and bool not in types_:
            problems.append((name, f"expected {_type_names(types_)}, got bool"))
        elif not isinstance(value, types_):
            problems.append(
                (name, f"expected {_type_names(types_)}, got "
                       f"{type(value).__name__}"))

    return problems


def observe_payload(payload, endpoint: str, *, sample: int = 25, where: str = ""):
    """Log how *payload* departs from its contract; return it untouched.

    *payload* is either one record (status-session) or a list of them. Only the
    first *sample* records of a list are inspected: the point is to notice a
    shape change, and a shape change shows up in the first handful — walking
    5,000 messages on the Socket.IO thread to re-learn the same fact would cost
    more than it tells us.

    *where* names the source in the log when it is not the endpoint itself. A
    WPPConnect-serialized message has the same shape whether it was fetched
    from get-messages or pushed over Socket.IO, so both are checked against the
    same contract — but a log line blaming get-messages for a live event would
    send the next person reading it to the wrong place.
    """
    try:
        if endpoint not in CONTRACTS:
            return payload
        label = where or endpoint
        records = payload if isinstance(payload, list) else [payload]
        for index, record in enumerate(records[:sample]):
            if not isinstance(record, dict):
                _report(label, "<record>",
                        f"expected dict, got {type(record).__name__}", index)
                continue
            for field, problem in _check(record, endpoint):
                _report(label, field, problem, index)
    except Exception:
        # A contract check must never be the reason a sync fails.
        logging.exception("[contract] check for %s failed to run", endpoint)
    return payload


def _report(label: str, field: str, problem: str, index: int) -> None:
    key = (label, field, problem)
    with _seen_lock:
        count = _seen.get(key, 0)
        _seen[key] = count + 1
    if count == 0:
        logging.warning(
            "[contract] %s: %s %s (first seen at item %d; further occurrences "
            "counted, not logged)", label, field, problem, index)


def contract_report() -> dict[tuple[str, str, str], int]:
    """Every mismatch seen this run, and how many times. For end-of-sync logging."""
    with _seen_lock:
        return dict(_seen)


def reset_contract_report() -> None:
    """Forget everything seen so far. Exists for tests."""
    with _seen_lock:
        _seen.clear()
