"""What changed between two copies of a changelog, said in words.

A changelog_<locale>.txt is a list of versions, newest first:

    V2.1.0.0
    <a paragraph or two summing the version up>
    WHAT'S NEW
    - an item
    FIXES
    - an item

A heading is a line with no lowercase letter; an item starts with "- ". The
comparison is item by item, not line by line, and the answer is sentences a
screen reader can read: "New in WHAT'S NEW:", "Changed in FIXES, as it now
reads:", "Removed from FIXES:", each followed by the items as the changelog
writes them. Never the + and - of a developer's diff. A version the old copy
does not have is shown whole, as the changelog shows it.

An item reworded keeps its place as one change (paired by similarity, as a
person would read it) instead of reading as one removal and one addition. An
item moved to another heading of the same version, unchanged, is not news.

Pure: no wx, no file, no network. Used by Help > "What's new since you last
looked" (core/changelog_seen.py) and by the updater's "What's new?"
(core/release_notes.py).
"""

import difflib
import re
from dataclasses import dataclass

_VERSION_RE = re.compile(r"^V(\d+(?:\.\d+)+[A-Za-z]*)\s*$")
#: The heading of a version's opening paragraphs, which come before any heading.
SUMMARY = ""

ADDED = "added"
CHANGED = "changed"
REMOVED = "removed"

#: How alike an old and a new item must be to read as one item reworded.
SIMILAR = 0.6


@dataclass(frozen=True)
class Version:
    number: str
    #: ((heading, (item, ...)), ...) in the changelog's order; SUMMARY first.
    sections: tuple
    #: The version's lines as written, without its "V" line.
    text: str


@dataclass(frozen=True)
class Change:
    version: str
    heading: str
    kind: str
    #: The item as it reads now; for REMOVED, as it read.
    text: str


def _is_heading(line: str) -> bool:
    return bool(line) and not line.startswith("- ") and line == line.upper() and any(
        ch.isalpha() for ch in line)


def parse(text: str) -> list:
    """The versions of a changelog, in its order."""
    versions = []
    current = None

    def close():
        if current is None:
            return
        number, lines = current
        sections, heading, items, paragraph = [], SUMMARY, [], []

        def flush_paragraph():
            if paragraph:
                items.append(" ".join(paragraph))
                paragraph.clear()

        for raw in lines:
            line = raw.strip()
            if not line:
                flush_paragraph()
            elif _is_heading(line):
                flush_paragraph()
                if items or heading != SUMMARY:
                    sections.append((heading, tuple(items)))
                heading, items = line, []
            elif line.startswith("- "):
                flush_paragraph()
                items.append(line[2:].strip())
            elif items and heading != SUMMARY and not paragraph:
                # A wrapped continuation of the item above.
                items[-1] = f"{items[-1]} {line}"
            else:
                paragraph.append(line)
        flush_paragraph()
        if items or heading != SUMMARY:
            sections.append((heading, tuple(items)))
        versions.append(Version(number, tuple(sections), "\n".join(lines).strip()))

    for raw in (text or "").splitlines():
        match = _VERSION_RE.match(raw.strip())
        if match:
            close()
            current = (match.group(1), [])
        elif current is not None:
            current[1].append(raw.rstrip())
    close()
    return versions


def _by_heading(version):
    found = {}
    for heading, items in version.sections:
        found.setdefault(heading, []).extend(items)
    return found


def _compare_version(old, new) -> list:
    old_items, new_items = _by_heading(old), _by_heading(new)
    headings = list(new_items) + [h for h in old_items if h not in new_items]
    changes = []
    for heading in headings:
        before = list(old_items.get(heading, ()))
        after = list(new_items.get(heading, ()))
        for item in list(after):
            if item in before:
                before.remove(item)
                after.remove(item)
        for item in list(after):
            best, score = None, SIMILAR
            for candidate in before:
                ratio = difflib.SequenceMatcher(None, candidate, item).ratio()
                if ratio >= score:
                    best, score = candidate, ratio
            if best is not None:
                before.remove(best)
                after.remove(item)
                changes.append(Change(new.number, heading, CHANGED, item))
        changes += [Change(new.number, heading, ADDED, item) for item in after]
        changes += [Change(new.number, heading, REMOVED, item) for item in before]
    # The same item under another heading of this version: moved, not news.
    added = [c for c in changes if c.kind == ADDED]
    for change in added:
        twin = next((c for c in changes if c.kind == REMOVED and c.text == change.text), None)
        if twin is not None:
            changes.remove(twin)
            changes.remove(change)
    return changes


def compare(old_text: str, new_text: str) -> tuple:
    """(new versions, changes): the versions `new_text` has and `old_text`
    does not, whole, and the item changes of the versions both have."""
    old = {version.number: version for version in parse(old_text)}
    fresh, changes = [], []
    for version in parse(new_text):
        if version.number not in old:
            fresh.append(version)
        else:
            changes += _compare_version(old[version.number], version)
    return fresh, changes


def first_version(text: str):
    """The newest version of a changelog, or None."""
    versions = parse(text)
    return versions[0] if versions else None


_KIND_KEYS = {
    ADDED: "changelog_diff_added",
    CHANGED: "changelog_diff_changed",
    REMOVED: "changelog_diff_removed",
}


def describe(fresh, changes, t) -> str:
    """The comparison as text to read, or "" when nothing changed. `t` is
    I18n.t."""
    parts = []
    for version in fresh:
        parts.append(t("changelog_diff_new_version").format(version=version.number)
                     + "\n\n" + version.text)
    order = []
    for change in changes:
        if change.version not in order:
            order.append(change.version)
    for number in order:
        lines = [t("changelog_diff_version").format(version=number)]
        mine = [c for c in changes if c.version == number]
        headings = []
        for change in mine:
            if change.heading not in headings:
                headings.append(change.heading)
        for heading in headings:
            section = heading or t("changelog_diff_summary")
            for kind in (ADDED, CHANGED, REMOVED):
                items = [c.text for c in mine if c.heading == heading and c.kind == kind]
                if items:
                    lines.append("")
                    lines.append(t(_KIND_KEYS[kind]).format(section=section))
                    lines += [f"- {item}" for item in items]
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
