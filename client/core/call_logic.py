"""Pure call-state helpers shared by the wx UI and headless tests."""


def incoming_call_can_answer(details: dict | None) -> bool:
    """Return whether this one-to-one incoming call can be answered."""
    return not bool((details or {}).get("is_group"))


def active_call_label_key(active: dict | None) -> str:
    """Return the label for an active individual call."""
    return "video_call_active_label" if (active or {}).get("is_video") else "voice_call_active_label"
