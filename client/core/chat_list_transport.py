"""One list mutation, then one read. Never retry an ambiguous write."""

from dataclasses import dataclass

from core.api_client import api_get, api_post
from core.chat_lists import ListSnapshot, list_change_verified, parse_list_snapshot


@dataclass(frozen=True)
class ListResult:
    snapshot: ListSnapshot | None
    outcome: str


def request_lists(url, headers, command=None):
    acknowledgement, outcome = None, "loaded"
    if command is not None:
        outcome = "unconfirmed"
        try:
            response = api_post(url, headers=headers, json=command, timeout=20)
            body = response.json()
            if response.status_code == 200 and body.get("status") == "success":
                acknowledgement = body.get("response")
            elif response.status_code in (400, 403, 404, 409, 422, 501):
                outcome = "refused"
        except Exception:
            # The server may already have applied it; a second POST is unsafe.
            pass
    try:
        response = api_get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            if command is None:
                outcome = "unavailable" if response.status_code in (404, 501) else "failed"
            return ListResult(None, outcome)
        snapshot = parse_list_snapshot(response.json())
    except Exception:
        return ListResult(None, "failed" if command is None else outcome)
    if command is not None and list_change_verified(command, acknowledgement, snapshot):
        outcome = "changed"
    return ListResult(snapshot, outcome)
