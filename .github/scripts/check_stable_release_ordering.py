"""Refuse a stable release tag that already-published alpha builds outrank.

Alpha builds (.github/workflows/alpha-release.yml) are versioned
``<major>.<minor>.<patch>.<commit count>alpha`` off the current stable version,
so they consume the fourth component. A stable release that only bumps that
fourth component therefore sorts BELOW alphas that are already out — and
client/updater.py would never offer it to anyone running one, stranding every
alpha user permanently with no error anywhere.

Run from .github/workflows/release.yml's test job, triggered by pushing the
stable tag, so a rejected tag never gets as far as a build or a draft release.

This lives in a file rather than inline in the workflow because getting it
wrong is expensive in both directions: a false negative strands alpha users, and
a false positive blocks a perfectly good release. tests/test_release_ordering_guard.py
covers it.
"""

from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "client"))

from updater import is_newer, parse_version  # noqa: E402


def comparable_alpha_tags(tags: "list[str]") -> "list[str]":
    """The alpha tags that can meaningfully block a release.

    Only tags the updater can actually parse count. The repository carries
    historical alpha tags from long before this channel existed, some in shapes
    parse_version() rejects outright (a trailing digit after the suffix, say).
    is_newer() returns False whenever either operand is unparseable, so treating
    those as blockers would reject EVERY future stable release forever.
    """
    return [
        t for t in tags
        if "alpha" in t.lower() and parse_version(t.lstrip("vV")) is not None
    ]


def find_blocking_alphas(stable_tag: str, tags: "list[str]") -> "list[str]":
    """Alpha tags that *stable_tag* fails to outrank, sorted for a stable message."""
    version = stable_tag.lstrip("vV")
    return sorted(
        t for t in comparable_alpha_tags(tags)
        if not is_newer(version, t.lstrip("vV"))
    )


def suggest_tag(stable_tag: str, blockers: "list[str]") -> str:
    """The nearest tag that WOULD clear *blockers*, keeping *stable_tag*'s style.

    Alphas consume the fourth component, so the fix is always to advance the
    patch component above the highest blocker and reset the fourth to 0. The
    original suffix is preserved so the suggestion fits whichever scheme the
    project is on — `0.25.1.0beta` while it is still beta, plain `1.1.0.0` once
    the stable line drops the suffix.
    """
    _nums, suffix = parse_version(stable_tag.lstrip("vV"))
    major, minor, patch, _build = max(
        parse_version(b.lstrip("vV"))[0] for b in blockers
    )
    return f"{major}.{minor}.{patch + 1}.0{suffix}"


def _git_tags() -> "list[str]":
    out = subprocess.run(
        ["git", "tag", "--list"], capture_output=True, text=True, check=True
    ).stdout
    return out.split()


def main() -> int:
    stable_tag = os.environ["RELEASE_TAG"]
    version = stable_tag.lstrip("vV")

    if parse_version(version) is None:
        print(f"::error::Release tag {version!r} is not a major.minor.patch.build "
              f"version, so the auto-updater can never compare it against what "
              f"users are running.")
        return 1

    tags = _git_tags()
    blockers = find_blocking_alphas(stable_tag, tags)
    if blockers:
        print(f"::error::Stable release {version} does not sort above already "
              f"published alpha builds: {', '.join(blockers)}. Users on those "
              f"alphas would never be offered it. Alpha builds consume the "
              f"fourth component, so advancing only that one (or only dropping "
              f"a dev/alpha/beta suffix, which leaves the numbers unchanged) is "
              f"never enough. Re-cut the release as v{suggest_tag(stable_tag, blockers)} "
              f"or higher.")
        return 1

    print(f"[INFO] {version} sorts above all "
          f"{len(comparable_alpha_tags(tags))} comparable alpha build(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
