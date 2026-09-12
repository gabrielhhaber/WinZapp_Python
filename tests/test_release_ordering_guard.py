"""Tests for .github/scripts/check_stable_release_ordering.py.

That guard runs in release.yml's test job, and a failure there stops the
release from being built at all. Getting it wrong is expensive in both
directions: a false negative strands every alpha user on the alpha channel
forever, a false positive blocks a perfectly good release. Hence tests.

The bug that motivated them: this repo carries historical alpha tags predating
the alpha channel, including `v0.3.4.0alpha1` — whose trailing digit
parse_version() rejects. is_newer() returns False for an unparseable operand,
so counting it as a blocker rejected every conceivable stable release.
"""

import importlib.util
import os
import sys

import pytest

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".github", "scripts", "check_stable_release_ordering.py",
)


@pytest.fixture(scope="module")
def guard():
    spec = importlib.util.spec_from_file_location("_release_ordering_guard", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Tags this repository actually carries, from before the alpha channel existed.
HISTORICAL = [
    "v0.3.4.0alpha1",  # unparseable: trailing digit after the suffix
    "v0.3.5.1alpha",
    "v0.8.0.0alpha",
    "v0.24.2.0beta",
    "v0.25.0.0beta",
]


def test_unparseable_alpha_tags_are_not_comparable(guard):
    assert "v0.3.4.0alpha1" not in guard.comparable_alpha_tags(HISTORICAL)
    assert "v0.3.5.1alpha" in guard.comparable_alpha_tags(HISTORICAL)


def test_only_alpha_tags_are_considered(guard):
    comparable = guard.comparable_alpha_tags(HISTORICAL)
    assert "v0.25.0.0beta" not in comparable
    assert "v0.24.2.0beta" not in comparable


def test_historical_alpha_tags_never_block_a_release(guard):
    """The regression: `v0.3.4.0alpha1` used to block every stable release."""
    for tag in ["v0.26.0.0beta", "v0.25.1.0beta", "v0.25.0.1beta", "v1.0.0.0"]:
        assert guard.find_blocking_alphas(tag, HISTORICAL) == [], tag


def test_a_stable_tag_that_alphas_outrank_is_blocked(guard):
    tags = HISTORICAL + ["v0.25.0.2155alpha"]
    assert guard.find_blocking_alphas("v0.25.0.1beta", tags) == ["v0.25.0.2155alpha"]


def test_bumping_patch_or_higher_clears_the_block(guard):
    tags = HISTORICAL + ["v0.25.0.2155alpha"]
    assert guard.find_blocking_alphas("v0.25.1.0beta", tags) == []
    assert guard.find_blocking_alphas("v0.26.0.0beta", tags) == []


def test_a_stable_tag_equal_to_an_alpha_is_blocked(guard):
    """is_newer is strict: same numbers means the stable one does NOT outrank
    the alpha on version numbers alone... except the suffix breaks the tie in
    the stable release's favour (alpha < beta < final), so this must pass."""
    tags = ["v0.25.0.2155alpha"]
    assert guard.find_blocking_alphas("v0.25.0.2155beta", tags) == []
    assert guard.find_blocking_alphas("v0.25.0.2155", tags) == []


def test_blockers_are_reported_sorted(guard):
    tags = ["v0.25.0.2157alpha", "v0.25.0.2155alpha", "v0.25.0.2156alpha"]
    assert guard.find_blocking_alphas("v0.25.0.1beta", tags) == [
        "v0.25.0.2155alpha", "v0.25.0.2156alpha", "v0.25.0.2157alpha",
    ]


def test_no_alpha_tags_at_all_is_fine(guard):
    assert guard.find_blocking_alphas("v0.26.0.0beta", ["v0.25.0.0beta"]) == []
    assert guard.find_blocking_alphas("v0.26.0.0beta", []) == []


# ── The stable line without a suffix (a.b.c.d), once WinZapp leaves beta ──────

def test_guard_works_on_a_suffixless_stable_line(guard):
    """The alpha channel outlives the beta suffix: once releases are plain
    a.b.c.d, the same rule has to keep holding."""
    tags = ["v1.0.0.0", "v1.0.0.2159alpha"]
    assert guard.find_blocking_alphas("v1.1.0.0", tags) == []
    assert guard.find_blocking_alphas("v2.0.0.0", tags) == []
    assert guard.find_blocking_alphas("v1.0.0.1", tags) == ["v1.0.0.2159alpha"]


def test_dropping_the_suffix_alone_does_not_advance_past_alphas(guard):
    """"Now it's stable" by deleting `beta` keeps the numbers identical, so the
    alphas published since still outrank it. This is the transition case most
    likely to be attempted by hand."""
    tags = ["v0.25.0.2159alpha"]
    assert guard.find_blocking_alphas("v0.25.0.0", tags) == ["v0.25.0.2159alpha"]
    # Advancing the numbers is what actually works.
    assert guard.find_blocking_alphas("v0.26.0.0", tags) == []
    assert guard.find_blocking_alphas("v1.0.0.0", tags) == []


# ── suggest_tag ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("stable_tag,expected", [
    ("v0.25.0.1beta", "0.25.1.0beta"),   # still on the beta line
    ("v1.0.0.1",      "1.0.1.0"),        # suffixless stable line
    ("v0.25.0.0",     "0.25.1.0"),       # dropped the suffix, same numbers
])
def test_suggestion_keeps_the_projects_current_scheme(guard, stable_tag, expected):
    blockers = ["v0.25.0.2159alpha"] if stable_tag.startswith("v0.25") else ["v1.0.0.2159alpha"]
    assert guard.suggest_tag(stable_tag, blockers) == expected


def test_the_suggested_tag_actually_clears_the_guard(guard):
    """The suggestion has to be right, not just plausible — feed it back in."""
    tags = HISTORICAL + ["v0.25.0.2159alpha", "v0.25.0.2160alpha"]
    for rejected in ["v0.25.0.1beta", "v0.25.0.0", "v0.25.0.9beta"]:
        blockers = guard.find_blocking_alphas(rejected, tags)
        assert blockers, rejected
        suggestion = "v" + guard.suggest_tag(rejected, blockers)
        assert guard.find_blocking_alphas(suggestion, tags) == [], suggestion


def test_suggestion_is_based_on_the_highest_blocker(guard):
    blockers = ["v1.0.0.2159alpha", "v1.2.0.5alpha"]
    assert guard.suggest_tag("v1.0.0.1", blockers) == "1.2.1.0"


# ── main(): exit codes and messages ───────────────────────────────────────────

def _run_main(guard, monkeypatch, tag, tags, capsys):
    monkeypatch.setenv("RELEASE_TAG", tag)
    monkeypatch.setattr(guard, "_git_tags", lambda: tags)
    code = guard.main()
    return code, capsys.readouterr().out


def test_main_accepts_a_good_tag(guard, monkeypatch, capsys):
    code, out = _run_main(guard, monkeypatch, "v0.26.0.0beta", HISTORICAL, capsys)
    assert code == 0
    assert "sorts above" in out


def test_main_rejects_an_unparseable_release_tag(guard, monkeypatch, capsys):
    """A tag the updater can't parse would ship a release nobody is ever
    offered — fail loudly at build time instead of silently at update time."""
    code, out = _run_main(guard, monkeypatch, "v2026.08.22-pre", HISTORICAL, capsys)
    assert code == 1
    assert "::error::" in out


def test_main_rejects_a_tag_alphas_outrank_and_says_which(guard, monkeypatch, capsys):
    tags = HISTORICAL + ["v0.25.0.2155alpha"]
    code, out = _run_main(guard, monkeypatch, "v0.25.0.1beta", tags, capsys)
    assert code == 1
    assert "::error::" in out
    assert "v0.25.0.2155alpha" in out
    # The message has to say what to do about it, not just that it failed.
    assert "0.25.1.0" in out
