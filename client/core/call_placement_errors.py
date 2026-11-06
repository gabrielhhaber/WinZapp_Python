"""Recognize the bridge's bounded call-placement reason codes."""


_REASONS = frozenset({"rate_limited", "group_too_few_members", "group_too_many_members"})


class CallPlacementError(RuntimeError):
    def __init__(self, reason, status):
        super().__init__(f"HTTP {status}")
        self.reason = reason


def placement_reason(body):
    if not isinstance(body, dict) or body.get("status") != "error":
        return None
    value = body.get("reasonCode")
    return value if isinstance(value, str) and value in _REASONS else None
