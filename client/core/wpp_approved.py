"""WPPConnect Server releases the maintainers approved after a WinZapp shipped.

``client/wpp_minimum_version.txt`` (the *pin*) is the server a build was
ported to, and it only moves with a WinZapp release. Most upstream releases
after a pin change dependencies only (security fixes in ``node_modules``) and
leave every file WinZapp overrides untouched, so the patches a build carries
work on them unchanged. Waiting for a WinZapp release to ship those is waiting
for no reason.

``client/wpp_approved_versions.json`` on ``main`` is the list of such
releases, read live by every install (raw.githubusercontent.com, no rate
limit). Each entry says which builds can run it, in terms the maintainers
already think in::

    {"version": "2.10.59", "min_pin": "2.10.58"}

"2.10.59 works with the patches of any build pinned at 2.10.58 or later"; in
practice ``min_pin`` is the last release that changed a file WinZapp
overrides. A build is offered it when ``min_pin <= its pin < version``: an
older build (pinned before the last breaking release) never sees it, and a
newer build already has it or something newer as its pin. An optional
``min_winzapp`` covers a requirement the pin cannot express (a release that
needs the Node a later WinZapp ships).

``.github/scripts/audit_wpp_upgrade.py --approve`` writes an entry after a
clean audit. The worst a wrong or tampered entry can do is name another
upstream tag: versions must be plain ``X.Y.Z``, the download URL is fixed to
the upstream repository, and nothing below the pin is ever offered.

Pure functions plus one fetch, no wx, so the selection is tested directly.
"""

import re

#: Where the list lives in the repository, and the branch every install reads.
MANIFEST_PATH = "client/wpp_approved_versions.json"
RAW_URL = "https://raw.githubusercontent.com/{repo}/main/" + MANIFEST_PATH

SCHEMA = 1

#: Plain release numbers only: no ranges, pre-releases, branches or paths.
_RELEASE = re.compile(r"^\d+\.\d+\.\d+$")


def _release(text):
    """packaging Version of a strict "X.Y.Z" (a leading v is accepted), or None."""
    if not isinstance(text, str):
        return None
    bare = text.strip().lstrip("vV")
    if not _RELEASE.match(bare):
        return None
    from packaging.version import Version
    return Version(bare)


def parse_manifest(data) -> list:
    """The well-formed entries of a manifest, as (version, min_pin, min_winzapp).

    A manifest of another schema answers [] (an old build must not guess at a
    format it does not know), and a malformed entry is skipped rather than
    voiding the others. ``min_winzapp`` is "" when absent; when present it must
    be a non-empty string, which the caller validates against its own scheme.
    """
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return []
    entries = data.get("approved")
    if not isinstance(entries, list):
        return []
    parsed = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        version = _release(entry.get("version"))
        min_pin = _release(entry.get("min_pin"))
        min_winzapp = entry.get("min_winzapp", "")
        if version is None or min_pin is None or min_pin > version:
            continue
        if not isinstance(min_winzapp, str) or ("min_winzapp" in entry and not min_winzapp.strip()):
            continue
        parsed.append((version, min_pin, min_winzapp.strip()))
    return parsed


def best_approved(entries, pin: str, winzapp_satisfies) -> str:
    """The tag ("v2.10.59") of the newest release approved for a build pinned
    at *pin*, when it is newer than the pin; "" otherwise.

    *winzapp_satisfies(min_winzapp) -> bool* answers whether the running
    WinZapp meets an entry's ``min_winzapp``; it is only asked about entries
    that carry one. No usable pin (a dev checkout) answers "".
    """
    floor = _release(pin)
    if floor is None:
        return ""
    best = None
    for version, min_pin, min_winzapp in entries:
        if not (min_pin <= floor < version):
            continue
        if min_winzapp and not winzapp_satisfies(min_winzapp):
            continue
        if best is None or version > best:
            best = version
    return f"v{best}" if best is not None else ""


def fetch_manifest(repo: str, user_agent: str, timeout: float = 15):
    """The manifest as published on *repo*'s main branch.

    A 404 (a fork without the file) is a definite "nothing approved" and
    answers {}; any other failure raises, so the caller can tell an
    unreachable network from an empty list.
    """
    from core import tls_trust
    resp = tls_trust.get(RAW_URL.format(repo=repo),
                         headers={"User-Agent": user_agent}, timeout=timeout)
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    return resp.json()
