"""The "don't ask again" switches of the chat confirmations.

Each one is a `user_interface` setting that starts True. The confirmation's own
"don't show again" box clears it, only together with Yes; Settings > User
Interface mirrors the same key and is the way back.
"""

CONFIRM_CLEAR_CHAT = "confirm_clear_chat"
CONFIRM_DELETE_CHAT = "confirm_delete_chat"


def confirmation_enabled(main_window, key: str) -> bool:
    return bool(main_window.settings.get("user_interface", {}).get(key, True))


def disable_confirmation(main_window, key: str) -> None:
    main_window.settings.setdefault("user_interface", {})[key] = False
    main_window.save_settings()
