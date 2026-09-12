"""CLAUDE.md's factual claims about this repo, made executable.

CLAUDE.md is the first thing anyone (human or agent) reads before touching this
codebase, and parts of it are not prose but *measurements*: how big the god
files are, how many modules patch node_modules, which modules exist at all.
Measurements rot silently. Nothing imports CLAUDE.md, so nothing fails when it
stops being true — it just keeps being read, and quoted.

Two real instances, both found by hand and fixed on the same day:

  * `client/main.py` was documented as "~15,500 lines" when it was 22,321, and
    `client/ui/conversations.py` as "~9,200" when it was 13,494 — both ~45%
    low. By then the figures had already been copied into other documents as
    fact, which is the actual cost: a stale number in the one file everybody
    trusts does not stay in that file.
  * The node_modules patch mechanism was documented as "two files" naming two
    modules, when there were four (`sender` and `welcome` had been added
    since). Anyone taking the doc at its word would have believed they had seen
    the whole mechanism.

Same idea as tests/test_language_files_in_sync.py: a rule CLAUDE.md states in
prose ("add the key to all five files") is worth nothing until something fails
when it is broken. Here the rule is "what CLAUDE.md says about the code is
true".

The claims are parsed *out of CLAUDE.md itself* rather than restated here, so
the document stays the single source of the claim and this file only checks it.
That also means a reformat which stops the parser from matching has to fail
loudly rather than quietly turn these tests into a green no-op — hence the
guards asserting the parse found anything at all.

Where these run
---------------
Marked ``docs`` and deselected by release.yml's test step (``pytest -m "not
docs"``). A failure there stops the stable build, and these tests have no
tolerance band: rename a module, forget to grep CLAUDE.md, and a stable cut
would be blocked over a Markdown edit.

The first version of this file argued that risk was covered because
alpha-release.yml runs the suite on every push to main. That argument was
wrong: alpha-release.yml carries ``paths-ignore: "**/*.md"``, so a push
touching only CLAUDE.md — the dominant way three of these six tests break,
since they parse its prose — triggers no alpha build at all, and main goes
green by absence rather than by testing.

What covers them is ci.yml, which runs the full suite on every pull_request
against main with no paths-ignore, together with the ruleset that forbids
pushing to main directly. Every doc-only change reaches main through a PR that
runs these tests, and none of them can delete a release.
"""

import pathlib
import re

import pytest

pytestmark = pytest.mark.docs

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLAUDE_MD = ROOT / "CLAUDE.md"

# How far a documented line count may sit from the real one before it counts as
# wrong. This number is the whole design of the file-size tests, so both things
# it protects against are worth writing down:
#
#   Too tight (an exact count, or a few percent) and the test fails on almost
#   every commit that touches main.py: it grows by roughly a thousand lines per
#   twenty merges to main. Recompute rather than trusting that figure — it
#   swings by 4x depending on how merge commits are counted, which is the same
#   rot this file exists to catch:
#
#       git log -100 --numstat --format='' origin/main -- client/main.py #         | awk '{a+=$1; d+=$2} END {print a-d}'
#
#   The fix for a too-tight band would always be
#   "edit a number in a document", i.e. a chore, and a test whose only failure
#   mode is a chore gets reflexively silenced and then deleted — at which point
#   the drift it existed to catch rides in unopposed. Note also that the doc
#   writes "~22,300", with a tilde and a rounding to the hundred: asserting
#   more precisely than the claim's own stated precision asserts something
#   CLAUDE.md never said.
#
#   Too loose (say, a factor of two) and the incident this file exists for
#   stays green. The doc sat 45% below reality long enough to be propagated
#   elsewhere; a band that tolerates that protects nothing.
#
# 20% catches that incident with room to spare (15,500 vs 22,321 is 44% off)
# while putting a doc refresh on the order of once every few thousand lines
# rather than once a commit. It is deliberately symmetric: these files are
# under active extraction work, and a doc still claiming 22,300 lines after
# main.py has been cut to 16,000 is wrong in exactly the direction that matters
# most to a reader deciding whether the file is too big to read straight
# through.
LINE_COUNT_TOLERANCE = 0.20

# 42 client/ paths are backticked today. 30 leaves room to legitimately drop a
# few while still failing if a whole section stops being parsed.
PATH_FLOOR = 30

# "`client/main.py` (~22,300 lines)" and
# "`client/ui/conversations.py` (`ConversationsPanel`, ~13,500 lines)" — the
# count does not sit immediately after the path, so allow a bounded run of
# same-line text between them rather than trying to spell out every shape.
_LINE_CLAIM = re.compile(r"`(client/[\w./-]+\.py)`[^\n]{0,60}?~([\d,]+)\s+lines")

# The files whose size CLAUDE.md's central navigational advice depends on
# ("grep this file first — the method you need very likely already exists
# here"). If a claim for one of these stops being found, the doc dropped it or
# reworded it past the parser; either way this file is no longer watching what
# it was written to watch.
GOD_FILES = ["client/main.py", "client/ui/conversations.py"]

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def _doc():
    return CLAUDE_MD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def line_claims():
    """{documented path: claimed line count} as CLAUDE.md states them."""
    return {path: int(n.replace(",", "")) for path, n in _LINE_CLAIM.findall(_doc())}


def test_the_line_count_claims_are_still_being_found(line_claims):
    missing = [f for f in GOD_FILES if f not in line_claims]
    assert missing == [], (
        f"CLAUDE.md no longer states a '~N lines' figure for {missing} in a shape this "
        f"test can read, so their size is no longer checked. Either restore the figure "
        f"or update _LINE_CLAIM to match the new wording. Found: {line_claims}"
    )


def test_documented_file_sizes_are_roughly_true(line_claims):
    wrong = {}
    for path, claimed in line_claims.items():
        real = len((ROOT / path).read_text(encoding="utf-8").splitlines())
        if abs(real - claimed) > claimed * LINE_COUNT_TOLERANCE:
            wrong[path] = (claimed, real, f"{(real - claimed) / claimed:+.0%}")
    assert wrong == {}, (
        "CLAUDE.md's line counts have drifted from the files (path: documented, "
        "actual, error). Round the real number to the nearest hundred and update "
        "CLAUDE.md — then check whether the stale figure was copied into other docs, "
        f"which is how the last one spread: {wrong}"
    )


# ── node_modules patch modules ───────────────────────────────────────────────

# The bullet opens "**node_modules patches**: four files patch ..." and then
# names each module in backticks. Both halves are claims — the count and the
# list — and they can disagree with the code and with each other.
_PATCH_BULLET = re.compile(r"\*\*node_modules patches\*\*: (\w+) files? patch")
_PATCH_MODULE = re.compile(r"`(client/core/wppconnect_\w+_layer_patch\.py)`")

# Where the real answer lives — the same set of modules the bullet describes.
PATCH_GLOB = "wppconnect_*_layer_patch.py"


@pytest.fixture(scope="module")
def real_patch_modules():
    return sorted(f"client/core/{p.name}" for p in (ROOT / "client" / "core").glob(PATCH_GLOB))


def test_the_node_modules_patch_bullet_is_still_being_found():
    doc = _doc()
    assert _PATCH_BULLET.search(doc), (
        "CLAUDE.md's '**node_modules patches**: N files patch ...' sentence no longer "
        "matches, so neither the count nor the module list is being checked any more."
    )
    assert _PATCH_MODULE.search(doc), (
        "CLAUDE.md no longer names any client/core/wppconnect_*_layer_patch.py module."
    )


def test_documented_patch_modules_are_the_ones_on_disk(real_patch_modules):
    documented = sorted(set(_PATCH_MODULE.findall(_doc())))
    # Both directions matter and they fail for different reasons: a module on
    # disk but undocumented means someone reading CLAUDE.md believes they have
    # seen the whole mechanism when they have not (that is how sender and
    # welcome went unmentioned); a documented module with no file sends them
    # looking for something that was renamed or deleted.
    assert documented == real_patch_modules, (
        f"CLAUDE.md's node_modules patch list does not match client/core/{PATCH_GLOB}. "
        f"Undocumented: {sorted(set(real_patch_modules) - set(documented))}; documented "
        f"but absent: {sorted(set(documented) - set(real_patch_modules))}"
    )


def test_the_patch_count_word_matches_the_modules_listed(real_patch_modules):
    word = _PATCH_BULLET.search(_doc()).group(1).lower()
    assert word in _NUMBER_WORDS, (
        f"CLAUDE.md says '{word} files patch ...' — not a number word this test knows. "
        f"Add it to _NUMBER_WORDS if the wording is intentional."
    )
    assert _NUMBER_WORDS[word] == len(real_patch_modules), (
        f"CLAUDE.md says '{word} files' patch node_modules, but there are "
        f"{len(real_patch_modules)}: {real_patch_modules}"
    )


# ── documented modules exist ─────────────────────────────────────────────────

# Every backticked client/… path CLAUDE.md names — the ones a reader is sent to
# open or grep, where a renamed or deleted one turns the doc's navigation advice
# into a dead end. Deliberately not restricted to .py: the two claims that were
# actually stale when this was written were `client/WinZapp.spec` (removed and
# git-ignored in c752124, while the prose still called it "the checked-in" one)
# and `client/api2/` (deleted in af250f7, still described as if it shipped).
_DOC_PATH = re.compile(r"`(client/[\w./-]+)`")

# client/api/ and client/node/ are git-ignored and deliberately absent from a
# fresh checkout — the fast CI test job never has either (hence the skips in
# tests/test_api_patches_in_sync.py), so nothing under them can be asserted to
# exist here.
UNCHECKABLE_PREFIXES = ("client/api/", "client/node/")


def test_every_path_claude_md_names_exists():
    documented = {
        p for p in _DOC_PATH.findall(_doc())
        if not p.startswith(UNCHECKABLE_PREFIXES)
    }
    # A floor, not an anchor. Anchoring on the god files does not work: they are
    # backticked in five separate places, so `set(GOD_FILES) <= documented`
    # still holds after every other path loses its backticks — verified by
    # stripping the "Multi-conta" section, which alone contributes 15 of the
    # paths parsed here, and watching this stay green with a provably dangling
    # path inside it. A floor is the same trade-off LINE_COUNT_TOLERANCE
    # reasons about: high enough that a section going unchecked fails, low
    # enough that ordinary editing does not.
    assert len(documented) >= PATH_FLOOR, (
        f"CLAUDE.md now yields only {len(documented)} backticked client/ paths "
        f"(floor {PATH_FLOOR}) — a section probably lost its backticks and its "
        f"claims stopped being checked. Lower the floor only if they really left."
    )

    missing = sorted(p for p in documented if not (ROOT / p).exists())
    assert missing == [], (
        f"CLAUDE.md points at paths that do not exist. If they were renamed, "
        f"update the doc; if they were deleted, delete the claim: {missing}"
    )


# ── the same claims, restated under .claude/ ─────────────────────────────────

# CLAUDE.md is not the only prose steering work in this repo any more: the
# skills and agents under .claude/ restate a good deal of the same
# architecture, and they name the same paths. Nothing watched them, so the
# guarantee this file provides stopped at one file while the surface it
# describes grew past it — the identical hazard CLAUDE.md's own "one document,
# one place to update it" note exists for.
#
# Only path existence is checked here, not counts or prose: a path is a claim
# with exactly one right answer, and a dangling one turns "go read
# client/foo.py" into a dead end for whichever agent loads that skill. The
# looser claims stay CLAUDE.md's alone, which is where they belong.
CLAUDE_DIR = ROOT / ".claude"

#: Low deliberately. This is a floor against the glob silently finding nothing
#: (a directory rename, a layout change), not a count of the docs that happen
#: to exist today — a skill being retired must not turn this red.
CLAUDE_DOC_FLOOR = 5


#: Agent worktrees are full clones of the repo living under .claude/worktrees/,
#: created and destroyed by the harness while work is in flight. Their copies of
#: these same docs are not a second thing to verify: they inflate the
#: parametrized case count (54 extra cases with four worktrees open, enough to
#: make "did my change add tests?" unanswerable), and a doc inside one is read
#: relative to the real ROOT, so a worktree mid-edit can turn this red for a
#: path that is perfectly fine.
_EXCLUDED_DIRS = {"worktrees"}


def _claude_docs():
    return sorted(
        p for p in CLAUDE_DIR.rglob("*.md")
        if not _EXCLUDED_DIRS.intersection(p.relative_to(CLAUDE_DIR).parts)
    )


def test_the_claude_directory_docs_are_still_being_found():
    """Guards the glob the way PATH_FLOOR guards the regex above: with no
    files matched, the parametrized test below runs zero cases and passes."""
    found = _claude_docs()
    assert len(found) >= CLAUDE_DOC_FLOOR, (
        f"only {len(found)} Markdown file(s) found under {CLAUDE_DIR} (floor "
        f"{CLAUDE_DOC_FLOOR}) — the skills/agents layout probably moved and "
        f"their paths stopped being checked. Found: {[str(p) for p in found]}"
    )


@pytest.mark.parametrize(
    "doc", _claude_docs(), ids=lambda p: str(p.relative_to(ROOT)).replace("\\", "/")
)
def test_every_path_a_claude_doc_names_exists(doc):
    documented = {
        p for p in _DOC_PATH.findall(doc.read_text(encoding="utf-8"))
        if not p.startswith(UNCHECKABLE_PREFIXES)
    }
    missing = sorted(p for p in documented if not (ROOT / p).exists())
    assert missing == [], (
        f"{doc.relative_to(ROOT)} points at paths that do not exist. A skill or "
        f"agent that sends the reader to a deleted file is worse than one that "
        f"says nothing: {missing}"
    )
