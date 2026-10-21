"""What the installed @wppconnect/wa-version catalogue says about calls.

Calls depend on the WhatsApp Web build, and the build is picked from the
catalogue in client/api/node_modules/@wppconnect/wa-version/versions.json.
core/wa_version_refresh.py stages a newer package at startup and applies it
the next time Node is spawned; the in-app reinstall preserves a newer compatible
copy and explicitly checks it after npm (npm alone can select stale metadata).
docs/traps/voice-calls.md measured the line: an
install whose newest entry was 2.3000.1046948731-alpha never initialised VoIP;
after a reinstall its catalogue reached 2.3000.1047835881-alpha and calls
worked.

The one-time "reinstall WPPConnect" notice for pre-2.0 accounts
(MainWindow._show_wpp_reinstall_notice_if_pending) asks this before showing
itself, so an install that was already reinstalled — every alpha tester was
told to do it by hand — is not told again that it may lack calls.
"""

from __future__ import annotations

import json
import re

#: The first build measured to bring calls up (docs/traps/voice-calls.md).
CALLS_MINIMUM_BUILD = "2.3000.1047835881-alpha"

_BUILD = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def build_key(version: str):
    """(major, minor, build) of a WhatsApp Web version string, or None."""
    m = _BUILD.match(str(version or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def newest_build(catalogue) -> str | None:
    """The newest version listed in a parsed versions.json, or None when it
    lists nothing recognisable."""
    if not isinstance(catalogue, dict):
        return None
    entries = catalogue.get("versions")
    names = []
    if isinstance(entries, list):
        names = [e.get("version") for e in entries if isinstance(e, dict)]
    names += [catalogue.get(k) for k in ("currentVersion", "currentAlpha", "currentBeta")]
    keyed = [(build_key(n), n) for n in names if build_key(n)]
    return max(keyed)[1] if keyed else None


def catalogue_supports_calls(catalogue, minimum: str = CALLS_MINIMUM_BUILD) -> bool | None:
    """True when the catalogue already reaches the calls build, False when it
    does not, None when it cannot tell (unreadable, empty). Callers treat None
    like False: the notice it gates is the safe side to err on."""
    newest = newest_build(catalogue)
    if newest is None:
        return None
    return build_key(newest) >= build_key(minimum)


def read_catalogue(path: str):
    """Parsed versions.json, or None when it is missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None
