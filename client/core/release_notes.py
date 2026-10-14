"""What the updater's "What's new?" says about the version it offers (#488).

It used to read the changelog **installed**, the one of the version being
replaced, and keep the entries between the two version numbers. An alpha's
number (2.1.0.4391) is past every heading of that file (V2.1.0.0), so nothing
matched and the whole old file was shown; and the release notes GitHub keeps
for each alpha were shown only when no changelog shipped at all, which never
happens. Testers had no way to know what an alpha changed.

Now, for the release on offer:

1. The changelog of the user's language **at that release's tag**, compared
   with the installed one in words (core/changelog_diff.py): the items that
   are new, changed or removed since the version in use.
2. For an alpha, the notes GitHub keeps for every alpha between the installed
   version and the offered one, newest first, made readable: a pull request's
   title and number, without its link, its author's @handle or Markdown.

The download is the caller's (`fetch(url) -> str`), so this stays testable;
anything that fails leaves that part out and the caller keeps the old text.
"""

import re

from core import changelog_diff

#: raw.githubusercontent.com, not the contents API: no rate limit to spend.
RAW_URL = "https://raw.githubusercontent.com/{repo}/{tag}/client/changelog_{locale}.txt"

_PULL = re.compile(
    r"^\s*[*-]\s+(?P<title>.+?)\s+by\s+@[\w.-]+(?:\[bot\])?\s+in\s+\S*/pull/(?P<number>\d+)\s*$")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")


def changelog_at(fetch, repo: str, tag: str, locale: str):
    """(locale, text) of the changelog at `tag`, in `locale` or else en-US;
    None when neither could be fetched."""
    for code in dict.fromkeys((locale, "en-US")):
        try:
            text = fetch(RAW_URL.format(repo=repo, tag=tag, locale=code))
        except Exception:
            continue
        if isinstance(text, str) and text.strip():
            return code, text
    return None


def readable_body(body: str, t) -> str:
    """A GitHub release body as lines a screen reader can read."""
    lines = []
    # "New Contributors" lists @handles welcomed to the project: nothing about
    # what changed, and nothing a screen reader should spell out.
    skipping = False
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            skipping = line.lstrip("#").strip().lower() == "new contributors"
        if skipping:
            continue
        if line.startswith("**Full Changelog**") or line.lower().startswith("full changelog"):
            continue
        pull = _PULL.match(line)
        if pull:
            lines.append("- " + t("whats_new_pull_request").format(
                title=pull.group("title").strip(), number=pull.group("number")))
            continue
        line = line.lstrip("#").strip()
        if line.lower() in ("what's changed", "new contributors"):
            continue
        if line.startswith(("* ", "- ")):
            line = "- " + line[2:].strip()
        line = _LINK.sub(r"\1", line)
        line = _URL.sub("", line).replace("**", "").strip()
        if line and line != "-":
            lines.append(line)
    return "\n".join(lines)


def github_notes(releases, is_alpha, newer_than, up_to, t, order) -> str:
    """The readable notes of every alpha in `releases` past `newer_than` and
    up to `up_to`, newest first by `order(version)`, each under its version.
    The three version functions are the updater's own, so this ranks versions
    exactly as the update check does."""
    parts = []
    for release in releases or ():
        if not isinstance(release, dict) or not is_alpha(release):
            continue
        version = str(release.get("tag_name", "")).lstrip("vV")
        if not version or not newer_than(version) or not up_to(version):
            continue
        text = readable_body(release.get("body", ""), t)
        if text:
            parts.append((version, t("changelog_diff_version").format(version=version) + "\n" + text))
    parts.sort(key=lambda part: order(part[0]), reverse=True)
    return "\n\n".join(text for _version, text in parts)


def compose(changelog_part: str, github_part: str, t) -> str:
    """The two parts under their headings; "" when both are empty."""
    sections = []
    if changelog_part:
        sections.append(t("whats_new_changelog_heading") + "\n\n" + changelog_part)
    if github_part:
        sections.append(t("whats_new_github_heading") + "\n\n" + github_part)
    return "\n\n\n".join(sections)


def changelog_changes(installed: str, offered: str, t) -> str:
    """The offered changelog compared with the installed one, in words."""
    if not installed or not offered:
        return ""
    return changelog_diff.describe(*changelog_diff.compare(installed, offered), t)
