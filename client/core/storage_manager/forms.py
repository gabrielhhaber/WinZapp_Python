"""Strict form parsing and portable preferences without identities or paths."""
import math

from .policy import Preferences


def number(text, *, integer=False, maximum=10**15):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("missing number")
    value = float(text.strip().replace(",", "."))
    if not math.isfinite(value) or value < 0 or value > maximum:
        raise ValueError("number outside range")
    if integer and not value.is_integer():
        raise ValueError("whole number required")
    return int(value) if integer else value


def portable(preferences):
    raw = preferences.as_dict()
    raw.pop("protected", None)
    raw.pop("protected_chats", None)
    raw.pop("other_accounts", None)
    raw.pop("match_chats", None)
    # Export is a template, not unattended operation consent for a new copy.
    raw.update(scan_on_start=False, interval_minutes=0,
               scan_on_open=False, match_media=False,
               paused_until=0,
               include_other_accounts=False, include_external_models=False)
    for rule in raw["rules"]:
        rule["enabled"] = False
    return raw


def imported(raw, existing):
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw.get("version") != 1:
        raise ValueError("unsupported preference template")
    safe = Preferences.read(portable(Preferences.read(raw)))
    safe.protected = set(existing.protected)
    safe.protected_chats = set(existing.protected_chats)
    safe.protected_categories |= existing.protected_categories
    return safe
