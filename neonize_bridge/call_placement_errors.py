"""Safe classifications for the pinned native call layer's known refusals.

Native exceptions can contain identities. Only fixed, recognized messages are
classified; their original text never crosses the HTTP boundary.
"""
import re


def placement_failure(error):
    """Return (HTTP status, reason code), without exposing the native message."""
    text = str(error)
    if text == "meowcaller: get group info: info query returned status 429: rate-overlimit":
        return 429, "rate_limited"
    if text == "meowcaller: group call by ID requires at least two remote members":
        return 400, "group_too_few_members"
    if re.fullmatch(r"meowcaller: group call by ID has [0-9]+ remote members; at most 31 can be called", text):
        return 400, "group_too_many_members"
    return 500, "placement_failed"
