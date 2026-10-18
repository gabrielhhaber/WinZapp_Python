"""Tests for .github/scripts/audit_wpp_upgrade.py.

The audit decides whether a WPPConnect Server bump is a one-line pin change or
real porting work, and it is wrong in both directions at a cost: a missed
intersection silently discards upstream work on the next rebuild, while
flagging a dependency that merely flows through sends someone chasing a
non-problem — the exact reasoning error CLAUDE.md warns is the trap here.

Every test drives the pure functions with literal payloads, so nothing reaches
the network. The two real ranges below are the ones CLAUDE.md documents, which
is what makes them worth pinning: 2.10.18 -> 2.10.21 touched two files WinZapp
overrides, and 2.10.21 -> 2.10.24 touched none.
"""

import importlib.util
import os

import pytest

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".github", "scripts", "audit_wpp_upgrade.py",
)


@pytest.fixture(scope="module")
def audit():
    spec = importlib.util.spec_from_file_location("_wpp_upgrade_audit", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The upstream file list of the real v2.10.18...v2.10.21 compare.
CHANGED_2_10_18_TO_21 = [
    ".dockerignore", ".env.example", ".github/workflows/docker.yml", ".gitignore",
    ".npmignore", "CHANGELOG.md", "README.md", "docker-compose.yml", "package.json",
    "scripts/check-compose-config.cjs", "src/config.test.ts", "src/config.ts",
    "src/index.ts", "yarn.lock",
]

# The real v2.10.21...v2.10.24 compare: four dependency bumps, three releases.
CHANGED_2_10_21_TO_24 = ["CHANGELOG.md", "package.json", "yarn.lock"]


class TestDiscardedUpstreamChanges:
    def test_it_names_the_files_the_restore_overwrites(self, audit):
        assert audit.discarded_upstream_changes(CHANGED_2_10_18_TO_21) == [
            "src/config.ts", "src/index.ts",
        ]

    def test_a_dependency_only_range_discards_nothing(self, audit):
        assert audit.discarded_upstream_changes(CHANGED_2_10_21_TO_24) == []

    def test_a_sibling_test_file_is_not_the_patched_file(self, audit):
        """src/config.test.ts is upstream's own and flows through untouched;
        reading it as src/config.ts would invent porting work every time."""
        assert "src/config.test.ts" not in audit.discarded_upstream_changes(
            CHANGED_2_10_18_TO_21
        )

    def test_it_reads_the_real_lists_rather_than_a_copy(self, audit):
        """The audit must never answer against a stale restatement of the
        very lists it is checking."""
        import setup_api

        assert audit.patched_paths() == (
            set(setup_api.CUSTOM_ROOT_FILES) | set(setup_api.CUSTOM_SRC_FILES)
        )
        assert "src/controller/deviceController.ts" in audit.patched_paths()
        assert "start.js" in audit.patched_paths()


class TestDependencyChanges:
    def test_a_moved_range_is_reported_per_block(self, audit):
        old = {"dependencies": {"multer": "^2.2.0"}, "peerDependencies": {"mongoose": "^8.23.0"}}
        new = {"dependencies": {"multer": "^2.4.0"}, "peerDependencies": {"mongoose": "^8.24.4"}}

        assert audit.dependency_changes(old, new) == {
            ("dependencies", "multer"): ("^2.2.0", "^2.4.0"),
            ("peerDependencies", "mongoose"): ("^8.23.0", "^8.24.4"),
        }

    def test_an_unchanged_dependency_is_absent(self, audit):
        pkg = {"dependencies": {"express": "4.22.2"}}
        assert audit.dependency_changes(pkg, pkg) == {}

    def test_a_dropped_dependency_reports_none_on_the_new_side(self, audit):
        """A key leaving `dependencies` matters as much as a moved range:
        WinZapp's own patches import several of them at runtime."""
        old = {"dependencies": {"qrcode": "^1.5.4"}}
        assert audit.dependency_changes(old, {"dependencies": {}}) == {
            ("dependencies", "qrcode"): ("^1.5.4", None),
        }

    def test_the_version_field_is_not_a_dependency(self, audit):
        """Every one of these bumps moves "version"; reporting it would make
        every audit look like it changed something."""
        old = {"version": "2.10.21", "dependencies": {"express": "4.22.2"}}
        new = {"version": "2.10.24", "dependencies": {"express": "4.22.2"}}
        assert audit.dependency_changes(old, new) == {}


class TestSplitByOverride:
    def test_the_security_bumps_are_the_benign_half(self, audit):
        """multer and sharp are NOT in _PATCHED_DEPENDENCY_KEYS, which is how
        upstream's security bumps arrive without WinZapp doing anything."""
        changes = {
            ("dependencies", "multer"): ("^2.2.0", "^2.4.0"),
            ("dependencies", "sharp"): ("^0.34.5", "^0.35.0"),
        }
        overridden, flowing = audit.split_by_override(changes)

        assert overridden == {}
        assert set(flowing) == set(changes)

    def test_an_overridden_key_is_separated_out(self, audit):
        changes = {
            ("dependencies", "qrcode"): ("^1.5.4", "^1.6.0"),
            ("dependencies", "multer"): ("^2.2.0", "^2.4.0"),
        }
        overridden, flowing = audit.split_by_override(changes)

        assert list(overridden) == [("dependencies", "qrcode")]
        assert list(flowing) == [("dependencies", "multer")]

    def test_nothing_is_lost_or_duplicated(self, audit):
        changes = {
            ("dependencies", "zod"): ("^3.25.0", "^3.26.0"),
            ("devDependencies", "mongoose"): ("^8.23.0", "^8.24.4"),
            ("dependencies", "prom-client"): ("^14.2.0", "^15.0.0"),
        }
        overridden, flowing = audit.split_by_override(changes)

        assert set(overridden) | set(flowing) == set(changes)
        assert not set(overridden) & set(flowing)


class TestHomologatedPair:
    def test_a_moved_pin_is_flagged(self, audit):
        changes = {("dependencies", "@wppconnect-team/wppconnect"): ("^2.3.3", "^2.4.0")}
        assert audit.homologated_pair_moved(changes) == changes

    def test_wa_js_counts_too(self, audit):
        changes = {("dependencies", "@wppconnect/wa-js"): ("^4.6.0", "^4.7.0")}
        assert audit.homologated_pair_moved(changes) == changes

    def test_wa_version_is_deliberately_not_part_of_the_pair(self, audit):
        """@wppconnect/wa-version is the expiring WhatsApp HTML catalogue, not
        an API surface — it stays updateable and must not raise a flag."""
        changes = {("dependencies", "@wppconnect/wa-version"): ("^1.5.4490", "^1.5.4834")}
        assert audit.homologated_pair_moved(changes) == {}

    def test_the_real_bump_moved_neither(self, audit):
        changes = {
            ("dependencies", "express-rate-limit"): ("^8.2.0", "^8.7.0"),
            ("dependencies", "multer"): ("^2.2.0", "^2.4.0"),
        }
        assert audit.homologated_pair_moved(changes) == {}


class TestRenamesAreNotABlindSpot:
    """A rename reports only the new path in "filename". Reading just that
    would print "flows through <new path>" and exit clean while setup_api.py
    restored WinZapp's copy at the old path and the patch became dead code."""

    def test_both_sides_of_a_rename_are_compared(self, audit):
        files = [{
            "filename": "src/util/tokenStore/fileTokenStore.ts",
            "previous_filename": "src/util/tokenStore/fileTokenStory.ts",
        }]
        assert audit.discarded_upstream_changes(audit.compared_paths(files)) == [
            "src/util/tokenStore/fileTokenStory.ts",
        ]

    def test_an_ordinary_change_carries_no_previous_name(self, audit):
        files = [{"filename": "README.md"}, {"filename": "src/config.ts"}]
        assert audit.compared_paths(files) == ["README.md", "src/config.ts"]


class TestDroppedDependencies:
    def test_a_removal_is_separated_from_the_benign_half(self, audit):
        """multer is imported by WinZapp's own patched controllers and is not
        in _PATCHED_DEPENDENCY_KEYS, so "upstream's range governs it" is the
        wrong reading of it disappearing."""
        changes = {("dependencies", "multer"): ("^2.2.0", None)}
        assert audit.dropped_dependencies(changes) == changes

    def test_a_moved_range_is_not_a_removal(self, audit):
        changes = {("dependencies", "multer"): ("^2.2.0", "^2.4.0")}
        assert audit.dropped_dependencies(changes) == {}

    def test_a_newly_added_dependency_is_not_a_removal(self, audit):
        changes = {("dependencies", "undici"): (None, "^6.0.0")}
        assert audit.dropped_dependencies(changes) == {}


class TestEnginesNode:
    def test_a_matching_pin_is_silent(self, audit):
        from client.node_download_config import NODE_VERSION

        assert audit.engines_node_mismatch({"engines": {"node": NODE_VERSION}}) == ()

    def test_a_moved_pin_is_reported_against_the_node_we_ship(self, audit):
        from client.node_download_config import NODE_VERSION

        assert audit.engines_node_mismatch({"engines": {"node": "24.0.0"}}) == (
            "24.0.0", NODE_VERSION,
        )

    def test_no_declaration_is_not_a_mismatch(self, audit):
        assert audit.engines_node_mismatch({}) == ()


def _pkg(deps=None, engines_node=None):
    from client.node_download_config import NODE_VERSION

    return {
        "version": "2.10.24",
        "dependencies": dict(deps or {"express": "4.22.2"}),
        "engines": {"node": engines_node or NODE_VERSION},
    }


class TestTheVerdictItself:
    """main()'s composition is where a mislabelled finding does its damage,
    so the decision is tested, not only the helpers feeding it."""

    def test_the_real_clean_bump_reports_no_problems(self, audit):
        result = audit.audit(
            CHANGED_2_10_21_TO_24,
            _pkg({"multer": "^2.2.0"}),
            _pkg({"multer": "^2.4.0"}),
        )
        assert result["problems"] == []
        assert any("No source changes at all" in line for line in result["lines"])

    def test_release_noise_is_not_reported_as_a_source_change(self, audit):
        result = audit.audit(CHANGED_2_10_21_TO_24, _pkg(), _pkg())
        for noisy in ("CHANGELOG.md", "package.json", "yarn.lock"):
            assert not any(line.strip().endswith(noisy) for line in result["lines"])

    def test_a_discarded_file_is_a_problem(self, audit):
        result = audit.audit(CHANGED_2_10_18_TO_21, _pkg(), _pkg())
        assert len(result["problems"]) == 1
        assert "src/config.ts" in result["problems"][0]

    def test_a_moved_pair_is_a_problem(self, audit):
        result = audit.audit(
            CHANGED_2_10_21_TO_24,
            _pkg({"@wppconnect-team/wppconnect": "^2.3.3"}),
            _pkg({"@wppconnect-team/wppconnect": "^2.4.0"}),
        )
        assert len(result["problems"]) == 1
        assert "node_modules patches" in result["problems"][0]

    def test_a_dropped_dependency_is_a_problem_not_a_flow_through(self, audit):
        result = audit.audit(
            CHANGED_2_10_21_TO_24, _pkg({"multer": "^2.2.0"}), _pkg({})
        )
        assert any("Upstream removed" in p for p in result["problems"])
        assert any("REMOVED UPSTREAM" in line for line in result["lines"])

    def test_an_engines_bump_is_a_problem(self, audit):
        result = audit.audit(
            CHANGED_2_10_21_TO_24, _pkg(), _pkg(engines_node="24.0.0")
        )
        assert any("Node 24.0.0" in p for p in result["problems"])

    def test_every_reported_line_survives_a_legacy_console(self, audit):
        """cp850 is cmd.exe's default codepage on a pt-BR Windows, and it was
        the CLEAN message that crashed there while a flagged one printed."""
        result = audit.audit(CHANGED_2_10_18_TO_21, _pkg(), _pkg(engines_node="24.0.0"))
        for line in result["lines"] + result["problems"]:
            line.encode("cp850")


class TestApproval:
    """--approve: a release recorded in client/wpp_approved_versions.json is
    offered to installs without a WinZapp release, so only a clean audit with
    no compiled source change may write it."""

    def test_a_dependency_only_release_can_be_approved(self, audit):
        result = audit.audit(CHANGED_2_10_21_TO_24, _pkg(), _pkg())
        assert audit.approval_blockers(result, CHANGED_2_10_21_TO_24) == []

    def test_a_problem_blocks_the_approval(self, audit):
        result = audit.audit(CHANGED_2_10_18_TO_21, _pkg(), _pkg())
        blockers = audit.approval_blockers(result, CHANGED_2_10_18_TO_21)
        assert any("src/config.ts" in b for b in blockers)

    def test_source_that_flows_through_still_blocks_it(self, audit):
        """A WinZapp update copies the pin's dist/ back, so a src/ change
        WinZapp does not override would come and go with each release."""
        changed = CHANGED_2_10_21_TO_24 + ["src/controller/labelsController.ts"]
        result = audit.audit(changed, _pkg(), _pkg())
        assert result["problems"] == []
        blockers = audit.approval_blockers(result, changed)
        assert len(blockers) == 1 and "labelsController.ts" in blockers[0]

    @pytest.mark.parametrize("path", ["tsconfig.json", "babel.config.js", "decrypt.js"])
    def test_root_build_files_block_it_too(self, audit, path):
        assert audit.built_source_changes(CHANGED_2_10_21_TO_24 + [path]) == [path]

    def test_non_source_files_do_not_block_it(self, audit):
        changed = CHANGED_2_10_21_TO_24 + [
            "README.md", ".github/workflows/docker.yml", "scripts/check-compose-config.cjs"]
        result = audit.audit(changed, _pkg(), _pkg())
        assert audit.approval_blockers(result, changed) == []

    def test_a_moved_node_requirement_blocks_it_without_min_winzapp(self, audit):
        """Shipped builds bundle the old Node even when this checkout's
        NODE_VERSION already matches the candidate."""
        from client.node_download_config import NODE_VERSION
        old, new = _pkg(engines_node="20.0.0"), _pkg(engines_node=NODE_VERSION)
        result = audit.audit(CHANGED_2_10_21_TO_24, old, new)
        assert result["problems"] == []
        blockers = audit.approval_blockers(result, CHANGED_2_10_21_TO_24, old, new)
        assert len(blockers) == 1 and "--min-winzapp" in blockers[0]
        assert audit.approval_blockers(
            result, CHANGED_2_10_21_TO_24, old, new, "2.2.0.0") == []

    def test_min_winzapp_is_written_when_given(self, audit):
        updated = audit.with_approval({"schema": 1, "approved": []},
                                      "2.10.59", "2.10.58", "2.2.0.0")
        assert updated["approved"] == [
            {"version": "2.10.59", "min_pin": "2.10.58", "min_winzapp": "2.2.0.0"}]

    def test_an_entry_is_added_newest_first_keeping_the_others(self, audit):
        manifest = {"schema": 1, "approved": [
            {"version": "2.10.51", "min_pin": "2.10.50", "min_winzapp": "2.1.0.0"},
        ]}
        updated = audit.with_approval(manifest, "v2.10.59", "2.10.58")
        assert updated["approved"] == [
            {"version": "2.10.59", "min_pin": "2.10.58"},
            {"version": "2.10.51", "min_pin": "2.10.50", "min_winzapp": "2.1.0.0"},
        ]
        assert manifest["approved"][0]["version"] == "2.10.51"   # input untouched

    def test_re_approving_a_version_can_widen_who_gets_it(self, audit):
        manifest = {"schema": 1, "approved": [{"version": "2.10.59", "min_pin": "2.10.58"}]}
        updated = audit.with_approval(manifest, "2.10.59", "2.10.55")
        assert updated["approved"] == [{"version": "2.10.59", "min_pin": "2.10.55"}]

    def test_re_approving_keeps_the_node_requirement_of_the_older_pin(self, audit):
        manifest = {"schema": 1, "approved": [
            {"version": "2.10.61", "min_pin": "2.10.58", "min_winzapp": "2.2.0.0"}]}
        updated = audit.with_approval(manifest, "2.10.61", "2.10.60")
        assert updated["approved"] == [
            {"version": "2.10.61", "min_pin": "2.10.58", "min_winzapp": "2.2.0.0"}]

    def test_re_approving_keeps_the_stricter_requirement(self, audit):
        manifest = {"schema": 1, "approved": [
            {"version": "2.10.61", "min_pin": "2.10.58", "min_winzapp": "2.2.0.0"}]}
        assert audit.with_approval(manifest, "2.10.61", "2.10.58", "2.1.0.0")[
            "approved"][0]["min_winzapp"] == "2.2.0.0"
        assert audit.with_approval(manifest, "2.10.61", "2.10.58", "2.3.0.0")[
            "approved"][0]["min_winzapp"] == "2.3.0.0"

    def test_re_approving_a_version_never_narrows_who_gets_it(self, audit):
        """The default old tag is the current pin, which moves."""
        manifest = {"schema": 1, "approved": [{"version": "2.10.61", "min_pin": "2.10.58"}]}
        updated = audit.with_approval(manifest, "2.10.61", "2.10.60")
        assert updated["approved"] == [{"version": "2.10.61", "min_pin": "2.10.58"}]

    def test_the_written_file_is_what_installs_parse(self, audit, tmp_path):
        from core.wpp_approved import parse_manifest
        path = tmp_path / "wpp_approved_versions.json"
        audit.write_approval("2.10.59", "2.10.58", str(path))
        audit.write_approval("2.10.60", "2.10.58", str(path))
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        assert [str(v) for v, _p, _w in parse_manifest(data)] == ["2.10.60", "2.10.59"]
