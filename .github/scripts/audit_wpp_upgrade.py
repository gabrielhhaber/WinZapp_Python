"""Answer, for a proposed WPPConnect Server bump, the three questions that decide it.

Bumping `client/wpp_minimum_version.txt` is a real change, not a floor:
`setup_api.py` checks that exact tag out, so every fresh install and every
in-app "reinstall API" gets it. The check that matters is not the release
notes — those are usually empty for these bumps — but the upstream diff read
against what WinZapp overrides:

1. **Source files upstream changed that WinZapp's restore discards.** Anything
   in `CUSTOM_ROOT_FILES + CUSTOM_SRC_FILES` that also moved upstream is work
   thrown away silently, because `setup_api.py` copies `client/api_patches/`
   over the checkout. Anything outside that intersection flows through
   untouched and needs no thought at all.
2. **Dependencies WinZapp overrides.** `_PATCHED_DEPENDENCY_KEYS` is
   deliberately narrow so upstream's own ranges govern everything else — which
   is how upstream's security bumps arrive without us doing anything. So the
   reasoning here is the *opposite* of (1), and getting it backwards is the
   trap: a changed dependency we do NOT override is good news, and upstream
   moving a caret we DO override is not by itself a reason to move our pin.
3. **Whether the homologated pair moved.** `@wppconnect-team/wppconnect` and
   `@wppconnect/wa-js` are pinned exact against the five `node_modules` patch
   modules. Upstream moving its own declaration does not move ours, but it is
   the one dependency change worth a human's eyes.
4. **Whether `engines.node` still matches the Node WinZapp ships.** Upstream
   pins it exactly, and both installers run against `client/node/`, built from
   `node_download_config.NODE_VERSION`. A disagreement is the shape of the
   documented sharp/Node 18 EBADENGINE breakage, which surfaced as a report
   about something else entirely.

The lists are imported from `setup_api.py` rather than restated here, so this
audit cannot answer against a stale copy of the very thing it is checking.

Reads GitHub through `gh`, which every maintainer running this already has.
Exit 1 means "a human has to look", never "the bump is wrong".

Usage:
    python .github/scripts/audit_wpp_upgrade.py 2.10.21 2.10.24
    python .github/scripts/audit_wpp_upgrade.py 2.10.24   # from the committed pin

tests/test_wpp_upgrade_audit.py covers the pure functions.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

ROOT_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
sys.path.insert(0, ROOT_DIR)

from setup_api import (  # noqa: E402
    CUSTOM_ROOT_FILES,
    CUSTOM_SRC_FILES,
    _PATCHED_DEPENDENCY_KEYS,
)
from client.node_download_config import NODE_VERSION  # noqa: E402

# A clean audit is the one that must survive a legacy console: cp850 is the
# default codepage of cmd.exe on a pt-BR Windows, and an em dash in the
# success message crashed the run with UnicodeEncodeError while a flagged one
# printed fine. Exit 1 then meant "needs a human" and "the terminal cannot
# spell", which is backwards. The output below is deliberately ASCII, and this
# is the belt for anything that slips through.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

UPSTREAM_REPO = "wppconnect-team/wppconnect-server"

# The two keys that are one homologated pair, not two independent pins — see
# the send-compatibility contract in CLAUDE.md.
HOMOLOGATED_PAIR = ("@wppconnect-team/wppconnect", "@wppconnect/wa-js")

_DEPENDENCY_BLOCKS = ("dependencies", "devDependencies", "peerDependencies")


def patched_paths() -> set:
    """Every upstream path WinZapp restores its own copy of after a checkout."""
    return set(CUSTOM_ROOT_FILES) | set(CUSTOM_SRC_FILES)


def discarded_upstream_changes(changed_paths) -> list:
    """Upstream changes that WinZapp's restore silently overwrites.

    This is the whole source-side audit: a non-empty result is upstream work
    that will not reach the built server until it is ported into
    client/api_patches/ by hand.
    """
    return sorted(set(changed_paths) & patched_paths())


def dependency_changes(old_pkg: dict, new_pkg: dict) -> dict:
    """{name: (old_range, new_range)} for every dependency that moved.

    Covers all three blocks, and reports a dependency that appeared or was
    dropped as None on the missing side — a key leaving `dependencies` matters
    as much as one changing range, since WinZapp imports several of them at
    runtime through its own patches.
    """
    def flatten(pkg):
        out = {}
        for block in _DEPENDENCY_BLOCKS:
            for name, spec in (pkg.get(block) or {}).items():
                out[(block, name)] = spec
        return out

    old, new = flatten(old_pkg), flatten(new_pkg)
    changes = {}
    for key in sorted(set(old) | set(new)):
        if old.get(key) != new.get(key):
            changes[key] = (old.get(key), new.get(key))
    return changes


def dropped_dependencies(changes: dict) -> dict:
    """The subset of *changes* upstream removed outright.

    Separated from the benign half deliberately. WinZapp's own patched
    controllers import several dependencies that are NOT in
    _PATCHED_DEPENDENCY_KEYS (multer, merge-deep, bcrypt, axios, cors,
    winston, swagger-ui-express, express-query-boolean, @aws-sdk/client-s3),
    so upstream dropping one is not "upstream's range governs it" — it is an
    import with nothing behind it. `npm run build` catches it downstream, but
    the whole value of this script is not being wrong in that direction.
    """
    return {k: v for k, v in changes.items() if v[1] is None}


def split_by_override(changes: dict) -> tuple:
    """Partition *changes* into the ones WinZapp overrides and the rest.

    Returns (overridden, flowing). "flowing" is the benign half: upstream's own
    range governs it, so the bump carries it for free. "overridden" is where
    api_patches/package.json's value wins regardless, so upstream moving is
    informational — it never changes what gets installed on its own.

    Keyed on the name alone, not the block: _merge_package_json_dependencies()
    only ever writes into the live `dependencies` block, so an upstream move of
    an overridden name inside devDependencies is labelled as overridden while
    it does in fact flow through. Erring toward "look at this" is the right
    direction for a name on that list, and the block is printed either way.
    """
    overridden, flowing = {}, {}
    for key, value in changes.items():
        target = overridden if key[1] in _PATCHED_DEPENDENCY_KEYS else flowing
        target[key] = value
    return overridden, flowing


def homologated_pair_moved(changes: dict) -> dict:
    """The subset of *changes* touching the exact-pinned runtime pair."""
    return {k: v for k, v in changes.items() if k[1] in HOMOLOGATED_PAIR}


def compared_paths(compare_files) -> list:
    """Every path a compare entry touches, both sides of a rename included.

    A rename reports only the NEW path in "filename"; the old one lives in
    "previous_filename". Reading just the first is a blind spot aimed squarely
    at this audit: upstream renaming a file WinZapp patches would print as
    "flows through <new path>" and exit clean, while setup_api.py restored
    WinZapp's copy at the old path, upstream's renamed module sat beside it,
    and the patch became dead code that still compiles. The repo does rename
    files (src/config/upload.js -> .ts, among four in its history).
    """
    paths = []
    for entry in compare_files:
        paths.append(entry["filename"])
        previous = entry.get("previous_filename")
        if previous:
            paths.append(previous)
    return paths


def engines_node_mismatch(new_pkg: dict) -> tuple:
    """(upstream_pin, ours) when they disagree, else ().

    Upstream pins engines.node exactly, and both installers run npm against
    client/node/, built from node_download_config.NODE_VERSION. A silent
    disagreement is the shape of the sharp/Node 18 EBADENGINE breakage.
    """
    upstream = (new_pkg.get("engines") or {}).get("node")
    if upstream and upstream.strip() != NODE_VERSION:
        return (upstream.strip(), NODE_VERSION)
    return ()


class AuditUnavailable(RuntimeError):
    """The audit could not be performed — distinct from a finding."""


def _gh_json(path: str):
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if result.returncode != 0:
        raise AuditUnavailable(
            f"gh api {path} failed ({result.returncode}): "
            f"{(result.stderr or '').strip() or 'no output'}"
        )
    return json.loads(result.stdout)


def fetch_changed_paths(old_tag: str, new_tag: str) -> list:
    payload = _gh_json(
        f"repos/{UPSTREAM_REPO}/compare/v{_bare(old_tag)}...v{_bare(new_tag)}"
    )
    return compared_paths(payload.get("files", []))


def fetch_package_json(tag: str) -> dict:
    import base64

    payload = _gh_json(
        f"repos/{UPSTREAM_REPO}/contents/package.json?ref=v{_bare(tag)}"
    )
    return json.loads(base64.b64decode(payload["content"]).decode("utf-8"))


def _bare(tag: str) -> str:
    """Accept 2.10.24 and v2.10.24 alike — the URLs add their own 'v'."""
    return tag.strip().lstrip("vV")


def committed_pin() -> str:
    path = os.path.join(ROOT_DIR, "client", "wpp_minimum_version.txt")
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


# Upstream moves these on every release; listing them as "source changes"
# would make every audit look like it touched something.
_RELEASE_NOISE = ("CHANGELOG.md", "package.json", "yarn.lock")


def audit(changed_paths, old_pkg: dict, new_pkg: dict) -> dict:
    """The whole verdict, as {"lines": [...], "problems": [...]}, no I/O.

    Split out of main() so the composition is testable: the decision of what
    counts as a problem is the behaviour that matters here, and it is where
    a mislabelled finding would do its damage.
    """
    lines, problems = [], []

    changed = sorted(set(changed_paths))
    discarded = discarded_upstream_changes(changed)
    source_changes = [p for p in changed if p not in _RELEASE_NOISE]

    lines.append(f"Files changed upstream: {len(changed)}")
    if not source_changes:
        lines.append("  No source changes at all - dependency/release commits only.")
    for path in source_changes:
        marker = "DISCARDED BY RESTORE" if path in discarded else "flows through     "
        lines.append(f"  {marker} {path}")
    lines.append("")

    changes = dependency_changes(old_pkg, new_pkg)
    dropped = dropped_dependencies(changes)
    overridden, flowing = split_by_override(changes)
    pair = homologated_pair_moved(changes)

    lines.append(f"Dependencies changed: {len(changes)}")
    for (block, name), (was, now) in sorted(changes.items()):
        if (block, name) in dropped:
            marker = "REMOVED UPSTREAM  "
        elif (block, name) in overridden:
            marker = "WINZAPP OVERRIDES "
        else:
            marker = "flows through     "
        lines.append(f"  {marker} {name} ({block}): {was} -> {now}")
    lines.append("")

    if discarded:
        problems.append(
            f"{len(discarded)} upstream change(s) land in files WinZapp restores "
            f"over: {', '.join(discarded)}. Port them into client/api_patches/ "
            f"or accept losing them, deliberately."
        )
    if dropped:
        problems.append(
            "Upstream removed "
            + ", ".join(f"{name} (was {was})" for (_b, name), (was, _n) in sorted(dropped.items()))
            + ". WinZapp's own patched controllers import several dependencies "
            "upstream declares, so check nothing in client/api_patches/ still "
            "requires these before trusting the build."
        )
    if pair:
        problems.append(
            "The homologated runtime pair moved upstream: "
            + ", ".join(f"{name} {was} -> {now}" for (_b, name), (was, now) in sorted(pair.items()))
            + ". WinZapp's exact pin still wins, so nothing changes by itself, but "
            "re-homologating means running all five node_modules patches against "
            "the candidate first (see the wppconnect-patch skill)."
        )
    engines = engines_node_mismatch(new_pkg)
    if engines:
        problems.append(
            f"Upstream now requires Node {engines[0]}, but WinZapp ships "
            f"{engines[1]} (client/node_download_config.NODE_VERSION). That "
            f"mismatch is what the sharp/Node 18 EBADENGINE breakage looked like."
        )

    return {"lines": lines, "problems": problems}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old_tag", nargs="?", help="current tag (default: the committed pin)")
    parser.add_argument("new_tag", help="candidate tag")
    args = parser.parse_args()

    old_tag = args.old_tag or committed_pin()
    new_tag = args.new_tag
    if args.old_tag is None:
        print(f"[INFO] Comparing against the committed pin {old_tag}.")
    print(f"=== WPPConnect Server {old_tag} -> {new_tag} ===\n")

    try:
        result = audit(
            fetch_changed_paths(old_tag, new_tag),
            fetch_package_json(old_tag),
            fetch_package_json(new_tag),
        )
    except AuditUnavailable as exc:
        # Deliberately not 1: "the audit could not run" and "the audit found
        # something" are opposite situations, and a typo'd tag reading as a
        # finding is how a real one gets dismissed.
        print(f"[ERROR] The audit could not be performed: {exc}")
        return 2

    for line in result["lines"]:
        print(line)

    if result["problems"]:
        print("NEEDS A HUMAN:")
        for problem in result["problems"]:
            print(f"  - {problem}")
        return 1

    print("Clean: nothing upstream changed that WinZapp overrides, the "
          "homologated pair is untouched, and engines.node still matches the "
          "Node WinZapp ships.")
    print("Still do both: bump client/wpp_minimum_version.txt, then re-run "
          "setup_api.py and confirm all five node_modules patches report "
          "applied. A plain npm install can resolve differently even when "
          "nothing upstream moved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
