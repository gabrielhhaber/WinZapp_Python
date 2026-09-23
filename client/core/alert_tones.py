"""Discovery and resolution of notification alert-tone files."""

import os
import re


ALERT_TONE_COUNT = 10
ALERT_TONE_EXTENSIONS = {".ogg", ".wav", ".mp3"}


def alert_tone_choice_keys() -> list[str]:
    """Return the legacy fixed keys retained for API compatibility."""
    return ["default"] + [f"alert_{i}" for i in range(1, ALERT_TONE_COUNT + 1)] + ["custom"]


def _pack_relative_file(pack: dict, rel_path) -> str:
    if not rel_path or not isinstance(rel_path, str) or os.path.isabs(rel_path):
        return ""
    pack_dir = os.path.abspath(pack["dir"])
    candidate = os.path.abspath(os.path.join(pack_dir, rel_path))
    try:
        if os.path.commonpath((pack_dir, candidate)) != pack_dir:
            return ""
    except ValueError:
        return ""
    return candidate


def _natural_sort_key(value: str) -> list:
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", value)]


def _alert_tones_from_pack(pack: "dict | None") -> list[tuple[str, str]]:
    """Return available (choice_key, display_name) pairs for one sound pack."""
    if not pack:
        return []

    found: list[tuple[str, str, str]] = []
    seen_paths: set[str] = set()
    for key, rel_path in pack.get("alerts", {}).items():
        path = _pack_relative_file(pack, rel_path)
        if not path or not os.path.isfile(path):
            continue
        normalized = os.path.normcase(os.path.normpath(rel_path))
        seen_paths.add(normalized)
        found.append((str(key), os.path.splitext(os.path.basename(rel_path))[0], rel_path))

    alerts_dir = os.path.join(pack.get("dir", ""), "alerts")
    try:
        names = os.listdir(alerts_dir) if os.path.isdir(alerts_dir) else []
    except OSError:
        names = []
    for name in names:
        path = os.path.join(alerts_dir, name)
        if not os.path.isfile(path) or os.path.splitext(name)[1].casefold() not in ALERT_TONE_EXTENSIONS:
            continue
        rel_path = os.path.join("alerts", name)
        normalized = os.path.normcase(os.path.normpath(rel_path))
        if normalized in seen_paths:
            continue
        found.append((f"file:{rel_path.replace(os.sep, '/')}", os.path.splitext(name)[0], rel_path))

    found.sort(key=lambda item: _natural_sort_key(item[1]))
    return [(key, label) for key, label, _rel_path in found]


def discover_alert_tone_choices(active_pack, default_pack) -> list[tuple[str, str]]:
    """Discover active choices and add missing choices from the default pack."""
    choices = _alert_tones_from_pack(active_pack)
    if active_pack is default_pack:
        return choices
    defaults = _alert_tones_from_pack(default_pack)
    if not choices:
        return defaults
    seen_keys = {key for key, _label in choices}
    return choices + [(key, label) for key, label in defaults if key not in seen_keys]


def alert_tone_previewable(choice: str, custom_path: str = "") -> bool:
    """Whether a "preview" button has something to play for this choice.

    Only "custom" can point at nothing: an empty or mistyped path used to
    leave the button showing, and pressing it opened an error box.
    """
    if choice != "custom":
        return True
    path = (custom_path or "").strip()
    return bool(path) and os.path.isfile(path)


def resolve_alert_tone_path(active_pack, default_pack, choice: str, custom_path: str = "") -> str:
    """Resolve a saved alert choice through the active/default pack chain."""
    if choice == "custom":
        return custom_path or ""
    key = "message_background" if choice == "default" else choice
    for pack in (active_pack, default_pack if default_pack is not active_pack else None):
        if not pack:
            continue
        if key.startswith("file:"):
            rel_path = key[len("file:"):]
        else:
            source = pack.get("events", {}) if key == "message_background" else pack.get("alerts", {})
            rel_path = source.get(key, "")
        path = _pack_relative_file(pack, rel_path)
        if path and os.path.isfile(path):
            return path
    return ""
