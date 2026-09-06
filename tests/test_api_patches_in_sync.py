"""client/api_patches/ and client/api/ must never drift apart.

client/api/ is almost entirely git-ignored, but .gitignore deliberately
un-ignores exactly WinZapp's patched files, because a fresh checkout (including
the CI release build) otherwise has nothing to restore and silently ships
vanilla, unpatched WPPConnect Server.

That leaves two copies of every patch on disk, and setup_api.py restores
client/api/ *from* client/api_patches/. So editing only the client/api/ copy
looks like it works — until the next setup_api.py run silently reverts it.
That is exactly what happened to start.js: commit daf2d352 added the npx-cli.js
resolution fallback (needed on machines with no system-wide Node) to
client/api/start.js only, and re-running setup_api.py threw it away.

These tests compare the two copies byte for byte so the same mistake fails
here instead of in a user's install.

package.json is deliberately NOT compared: setup_api.py merges only
_PATCHED_DEPENDENCY_KEYS into whatever the clone produced (so WPPConnect's own
"version" field keeps reflecting the tag actually built) and re-serializes the
file, so the two copies legitimately differ. Its one patched dependency
(@ffmpeg-installer/ffmpeg) is checked instead — and, separately,
@wppconnect-team/wppconnect is checked to confirm it is deliberately NOT
patched: that pin used to be forced to an exact version ("2.2.4") that went
stale within days, because this dependency releases multiple times a week —
wppconnect-server's own package.json had already moved on to requiring a newer
one than what WinZapp had frozen, silently running an incompatible pairing.
fluent-ffmpeg used to be patched too, and was never imported anywhere by
anything — it is checked to confirm it stays gone from every file.
"""

import importlib.util
import re
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
API = ROOT / "client" / "api"
PATCHES = ROOT / "client" / "api_patches"


def _setup_api_module():
    """setup_api.py lives at the repo root, which is not on pytest's pythonpath
    (pytest.ini puts client/ there), so load it by path."""
    spec = importlib.util.spec_from_file_location("winzapp_setup_api", ROOT / "setup_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

# Mirrors setup_api.py's CUSTOM_ROOT_FILES + CUSTOM_SRC_FILES exactly — the
# test below asserts that equality against the real constants, in both
# directions, so a file added to setup_api.py can no longer stay uncompared
# here (which is what happened to auth.ts and statusController.ts: both were
# restored by setup_api.py and documented in CLAUDE.md, but drift in either
# went undetected because only this list was checked, and only one way round).
MIRRORED_FILES = [
    "start.js",
    "config.json",
    ".eslintrc.json",
    ".prettierrc",
    ".prettierignore",
    "jest.config.js",
    "decrypt.js",
    "src/config.ts",
    "src/index.ts",
    "src/util/createSessionUtil.ts",
    "src/util/sessionUtil.ts",
    "src/util/functions.ts",
    "src/util/tokenStore/fileTokenStory.ts",
    "src/middleware/statusConnection.ts",
    "src/middleware/auth.ts",
    "src/dto/sync.ts",
    "src/middleware/instrumentation.ts",
    "src/errors/domain.ts",
    "src/middleware/errorHandler.ts",
    "src/services/messageResolver.ts",
    "src/types/express/index.d.ts",
    "src/tests/middleware/instrumentation.test.ts",
    "src/tests/dto/sync.test.ts",
    "src/tests/middleware/errorHandler.test.ts",
    "src/controller/deviceController.ts",
    "src/controller/messageController.ts",
    "src/controller/sessionController.ts",
    "src/controller/statusController.ts",
    "src/routes/index.ts",
]


@pytest.mark.parametrize("rel_path", MIRRORED_FILES)
def test_the_two_copies_of_each_patch_are_identical(rel_path):
    patch = PATCHES / rel_path
    live = API / rel_path
    if not patch.exists():
        pytest.skip(f"client/api_patches/{rel_path} not present")
    if not live.exists():
        # client/api/ is only populated after setup_api.py has run; a bare
        # checkout legitimately has just the tracked subset.
        pytest.skip(f"client/api/{rel_path} not present (API not set up here)")
    # Normalized for line endings only (a Windows checkout of client/api_patches/
    # vs. whatever setup_api.py wrote can legitimately differ in CRLF/LF alone)
    # — an actual content difference below this point is real drift and must
    # still fail loudly. This is the enforcement mechanism for CLAUDE.md's
    # "only ever edit the tracked copy under client/api_patches/... or the
    # next setup_api.py run will silently revert this file" rule; softening
    # it to a skip on any difference (as a previous version of this test did)
    # defeats that on the one environment where it can actually catch
    # anything — CI's fast test job never has client/api/ at all (skipped
    # above), and CI's build job always regenerates client/api/ fresh from
    # client/api_patches/ so the two can never differ there either. A local
    # dev machine with a stale client/api/ left over from before an
    # api_patches/ edit is the only place this assertion is ever reachable.
    patch_bytes = patch.read_bytes().replace(b"\r\n", b"\n")
    live_bytes = live.read_bytes().replace(b"\r\n", b"\n")
    assert patch_bytes == live_bytes, (
        f"client/api/{rel_path} and client/api_patches/{rel_path} have drifted. "
        f"api_patches/ is the source of truth setup_api.py restores from — edit "
        f"that copy (and mirror it into client/api/), or the next setup_api.py "
        f"run will silently revert this file."
    )


def test_setup_api_patch_list_matches_this_one():
    """MIRRORED_FILES and setup_api.py's own lists must name the same files.

    Checked as a set equality against the imported constants rather than by
    grepping the source, because the direction that actually bites is the one
    a subset check misses: a file added to setup_api.py and never added here
    is restored on every setup run while nothing ever compares the two copies.
    src/middleware/auth.ts and src/controller/statusController.ts sat in
    exactly that blind spot.
    """
    setup_api = _setup_api_module()
    patched = set(setup_api.CUSTOM_ROOT_FILES) | set(setup_api.CUSTOM_SRC_FILES)
    assert patched == set(MIRRORED_FILES), (
        f"only in setup_api.py (patched but never compared): "
        f"{sorted(patched - set(MIRRORED_FILES))}; "
        f"only here (compared but no longer patched): "
        f"{sorted(set(MIRRORED_FILES) - patched)}"
    )


def test_the_in_app_installer_restores_the_same_patches():
    """ApiSetupDialog — the "install modules" flow every end user goes through
    just by running the program — has its own copy of the list. It must not fall
    behind setup_api.py's, or the API installed on users' machines is not the
    one we develop and test against.

    A containment check rather than the set equality used for setup_api.py:
    ApiSetupDialog also restores dist/middleware/auth.js, a *compiled* artifact
    with no counterpart in api_patches/, so there are legitimately no two
    copies of it to compare.
    """
    src = (ROOT / "client" / "ui" / "dialogs" / "api_setup.py").read_text(encoding="utf-8")
    for rel_path in MIRRORED_FILES:
        assert f'"{rel_path}"' in src, f"ApiSetupDialog does not restore {rel_path}"


def test_both_installers_patch_the_same_dependencies():
    """package.json is merged, not copied, by both flows — but only setup_api.py
    used to do it at all, so every end-user install ran npm install against the
    vanilla upstream file and never got the ffmpeg dependency it actually needs."""
    setup = (ROOT / "setup_api.py").read_text(encoding="utf-8")
    dialog = (ROOT / "client" / "ui" / "dialogs" / "api_setup.py").read_text(encoding="utf-8")
    assert '"@ffmpeg-installer/ffmpeg"' in setup
    assert '"@ffmpeg-installer/ffmpeg"' in dialog, "ApiSetupDialog does not patch @ffmpeg-installer/ffmpeg"


def test_wppconnect_runtime_is_pinned_by_both_installers():
    """WinZapp ships a *homologated pair*: one WPPConnect Server tag
    (client/wpp_minimum_version.txt) together with one exact
    @wppconnect-team/wppconnect + @wppconnect/wa-js it was validated against.

    Upstream declares a caret range, so leaving it alone meant a plain
    `npm install` of the same server tag could change the browser-side send
    and status APIs underneath an unchanged WinZapp build — which is what it
    did. Moving the pair is a deliberate act, made in one commit alongside
    client/wpp_minimum_version.txt, never something a reinstall does on its
    own. Both installers therefore have to carry the key, or the end-user
    install flow silently resolves a different pair than a dev build."""
    setup = (ROOT / "setup_api.py").read_text(encoding="utf-8")
    dialog = (ROOT / "client" / "ui" / "dialogs" / "api_setup.py").read_text(encoding="utf-8")

    def _patched_keys(src: str, list_name: str) -> str:
        start = src.index(f"{list_name} = [")
        end = src.index("]", start)
        return src[start:end]

    assert "@wppconnect-team/wppconnect" in _patched_keys(setup, "_PATCHED_DEPENDENCY_KEYS")
    assert "@wppconnect-team/wppconnect" in _patched_keys(dialog, "_PATCHED_DEPENDENCY_KEYS")


def test_fluent_ffmpeg_is_gone_everywhere():
    """It was declared as a dependency but never imported anywhere — not by
    wppconnect-server, not by WinZapp's own patched TypeScript, not by the
    Python side (which shells out to the @ffmpeg-installer/ffmpeg binary
    directly instead). A genuinely unused dependency installed on every
    end-user machine for nothing."""
    for path in (
        ROOT / "setup_api.py",
        ROOT / "client" / "ui" / "dialogs" / "api_setup.py",
        PATCHES / "package.json",
        API / "package.json",
    ):
        if path.exists():
            assert "fluent-ffmpeg" not in path.read_text(encoding="utf-8"), path


def test_the_in_app_installer_refreshes_root_files_rather_than_only_preserving_them():
    """start.js and config.json carry no per-install state — the API key and
    port both come from environment variables injected at launch, and nothing
    writes either file at runtime. Merely preserving whatever was on disk froze
    them at whatever an older WinZapp install left behind, so a user updating
    from an old version silently kept its config.json forever."""
    dialog = (ROOT / "client" / "ui" / "dialogs" / "api_setup.py").read_text(encoding="utf-8")
    assert "_CUSTOM_ROOT_FILES" in dialog
    assert "_CUSTOM_SRC_FILES + _CUSTOM_ROOT_FILES" in dialog, (
        "root files must go through the same api_patches/ restore loop"
    )
    # ...while still being kept out of the upstream ZIP's way.
    assert '_PRESERVE = {"start.js", ".env", "config.json"}' in dialog


class TestRestoreRunsOnEveryPath:
    """Re-running setup_api.py against an existing client/api/ must repair it.

    The restore used to live inside the `else:` of `if already_cloned:` and
    inside `if tag:`, so the most common invocation of all — an existing
    client/api/ and no WPPCONNECT_TAG_VERSION — fell through both and restored
    nothing. A client/api/ whose patched files had been deleted (which is what
    `git rm`-ing the client/api/ copies out of this repo does to every
    developer's working tree on the next pull) could then not be repaired by
    re-running the script at all: npm install died with ENOENT on the missing
    package.json, and had it survived, `npm run build` would have compiled
    vanilla upstream with none of WinZapp's patches in it.
    """

    def test_the_restore_is_not_nested_inside_a_branch(self):
        src = (ROOT / "setup_api.py").read_text(encoding="utf-8").splitlines()
        calls = [ln for ln in src if "_restore_custom_files(custom_contents)" in ln]
        assert calls, "main() no longer restores the patched files at all"
        for line in calls:
            indent = len(line) - len(line.lstrip())
            assert indent == 4, (
                f"_restore_custom_files is nested inside a branch ({indent} spaces of "
                f"indent) — it must run on every path through main(), including an "
                f"already-cloned client/api/ with no tag pinned"
            )

    def test_missing_patched_files_are_written_back(self, tmp_path, monkeypatch):
        setup_api = _setup_api_module()
        api = tmp_path / "api"
        api.mkdir()
        monkeypatch.setattr(setup_api, "CLIENT_API_DIR", str(api))
        setup_api._restore_custom_files({
            "start.js": b"// patched start\n",
            "src/controller/sessionController.ts": b"// patched controller\n",
        })
        assert (api / "start.js").read_bytes() == b"// patched start\n"
        # Subdirectories the clone no longer has must be recreated, not crash.
        assert (api / "src" / "controller" / "sessionController.ts").exists()

    def test_an_edited_live_copy_is_overwritten(self, tmp_path, monkeypatch):
        """api_patches/ is the source of truth — editing only client/api/ must
        not survive a setup_api.py run (see this module's docstring)."""
        setup_api = _setup_api_module()
        api = tmp_path / "api"
        api.mkdir()
        (api / "start.js").write_bytes(b"// hand-edited, unsaved anywhere else\n")
        monkeypatch.setattr(setup_api, "CLIENT_API_DIR", str(api))
        setup_api._restore_custom_files({"start.js": b"// patched start\n"})
        assert (api / "start.js").read_bytes() == b"// patched start\n"


class TestPackageJsonRecovery:
    """A missing client/api/package.json is fatal to npm install, so it has to
    be put back before the merge (which only patches a file that exists)."""

    def _api_dir(self, tmp_path, monkeypatch):
        setup_api = _setup_api_module()
        api = tmp_path / "api"
        api.mkdir()
        monkeypatch.setattr(setup_api, "CLIENT_API_DIR", str(api))
        monkeypatch.setattr(setup_api, "API_PATCHES_DIR", str(PATCHES))
        return setup_api, api

    def test_an_existing_package_json_is_left_alone(self, tmp_path, monkeypatch):
        """Upstream's own file for the tag on disk is the one we want — this
        must never clobber it with api_patches/' frozen 'version' field."""
        setup_api, api = self._api_dir(tmp_path, monkeypatch)
        (api / "package.json").write_text(
            json.dumps({"version": "2.10.1", "dependencies": {}}), encoding="utf-8"
        )
        setup_api._recover_upstream_package_json()
        assert json.loads((api / "package.json").read_text(encoding="utf-8"))["version"] == "2.10.1"

    def test_it_falls_back_to_api_patches_when_there_is_no_clone(self, tmp_path, monkeypatch):
        """No .git in client/api/ means no upstream copy to check out — better a
        frozen package.json than npm install failing with ENOENT."""
        setup_api, api = self._api_dir(tmp_path, monkeypatch)
        setup_api._recover_upstream_package_json()
        assert (api / "package.json").exists()
        deps = json.loads((api / "package.json").read_text(encoding="utf-8"))["dependencies"]
        assert "@ffmpeg-installer/ffmpeg" in deps


class TestPackageJsonMerge:
    """ApiSetupDialog._merge_package_json_dependencies is a staticmethod, so it
    runs without a wx.App."""

    @staticmethod
    def _dialog():
        from ui.dialogs.api_setup import ApiSetupDialog
        return ApiSetupDialog

    def _setup_dirs(self, tmp_path, pkg, patch):
        api = tmp_path / "api"
        patches = tmp_path / "api_patches"
        api.mkdir()
        patches.mkdir()
        (api / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        (patches / "package.json").write_text(json.dumps(patch), encoding="utf-8")
        return api, patches

    def test_patched_dependencies_are_applied(self, tmp_path):
        api, patches = self._setup_dirs(
            tmp_path,
            {"version": "2.10.1", "dependencies": {"@ffmpeg-installer/ffmpeg": "^0.9.0"}},
            {"version": "0.0.0", "dependencies": {"@ffmpeg-installer/ffmpeg": "^1.1.0"}},
        )
        self._dialog()._merge_package_json_dependencies(str(api), str(patches))
        out = json.loads((api / "package.json").read_text(encoding="utf-8"))
        assert out["dependencies"]["@ffmpeg-installer/ffmpeg"] == "^1.1.0"

    def test_wppconnect_runtime_pin_is_applied_by_the_merge(self, tmp_path):
        """The merge is what actually installs the homologated pair: whatever
        range the download declared has to lose to api_patches/package.json for
        this one key, exactly like every other patched dependency. Anything
        else and the pin exists only on paper."""
        api, patches = self._setup_dirs(
            tmp_path,
            {"version": "2.10.1", "dependencies": {"@wppconnect-team/wppconnect": "^2.2.6"}},
            {"version": "0.0.0", "dependencies": {"@wppconnect-team/wppconnect": "2.2.4"}},
        )
        self._dialog()._merge_package_json_dependencies(str(api), str(patches))
        out = json.loads((api / "package.json").read_text(encoding="utf-8"))
        assert out["dependencies"]["@wppconnect-team/wppconnect"] == "2.2.4"

    def test_the_downloaded_version_field_is_never_overwritten(self, tmp_path):
        """WppUpdateChecker compares this against the latest GitHub release — it
        has to describe the tag actually downloaded, not one frozen in
        api_patches/."""
        api, patches = self._setup_dirs(
            tmp_path,
            {"version": "2.10.1", "dependencies": {}},
            {"version": "2.10.0", "dependencies": {"@ffmpeg-installer/ffmpeg": "^1.1.0"}},
        )
        self._dialog()._merge_package_json_dependencies(str(api), str(patches))
        assert json.loads((api / "package.json").read_text(encoding="utf-8"))["version"] == "2.10.1"

    def test_other_upstream_dependencies_are_left_alone(self, tmp_path):
        """A wholesale copy would roll every unrelated dependency back to
        whatever api_patches/ happened to freeze."""
        api, patches = self._setup_dirs(
            tmp_path,
            {"version": "2.10.1", "dependencies": {"express": "4.22.1", "axios": "^1.14.0"}},
            {"version": "0.0.0", "dependencies": {"express": "4.0.0", "@ffmpeg-installer/ffmpeg": "^1.1.0"}},
        )
        self._dialog()._merge_package_json_dependencies(str(api), str(patches))
        deps = json.loads((api / "package.json").read_text(encoding="utf-8"))["dependencies"]
        assert deps["express"] == "4.22.1", "express is not a patched key"
        assert deps["axios"] == "^1.14.0"

    def test_a_missing_package_json_is_not_fatal(self, tmp_path):
        api = tmp_path / "api"
        patches = tmp_path / "api_patches"
        api.mkdir()
        patches.mkdir()
        # Must not raise: npm install can still work on the upstream file.
        self._dialog()._merge_package_json_dependencies(str(api), str(patches))

    def test_malformed_json_leaves_the_file_untouched(self, tmp_path):
        api, patches = self._setup_dirs(tmp_path, {"dependencies": {}}, {"dependencies": {}})
        (api / "package.json").write_text("{ not json", encoding="utf-8")
        self._dialog()._merge_package_json_dependencies(str(api), str(patches))
        assert (api / "package.json").read_text(encoding="utf-8") == "{ not json"

    def test_the_real_patch_file_produces_the_real_pins(self, tmp_path):
        """Runs the merge against the actual api_patches/package.json shipped in
        the repo, so a bad edit to it is caught here."""
        api = tmp_path / "api"
        api.mkdir()
        (api / "package.json").write_text(
            json.dumps({"version": "9.9.9", "dependencies": {}}), encoding="utf-8"
        )
        self._dialog()._merge_package_json_dependencies(str(api), str(PATCHES))
        out = json.loads((api / "package.json").read_text(encoding="utf-8"))
        assert out["version"] == "9.9.9"
        assert "@ffmpeg-installer/ffmpeg" in out["dependencies"]
        patched = json.loads((PATCHES / "package.json").read_text(encoding="utf-8"))
        assert (
            out["dependencies"]["@wppconnect-team/wppconnect"]
            == patched["dependencies"]["@wppconnect-team/wppconnect"]
        )


def test_patched_dependencies_are_present_in_the_live_package_json():
    """setup_api.py merges this into whatever the clone produced. It is what the
    file is patched *for*, so its absence means the merge never ran (or was
    undone)."""
    live = API / "package.json"
    if not live.exists():
        pytest.skip("client/api/package.json not present")
    patched = json.loads((PATCHES / "package.json").read_text(encoding="utf-8"))
    deps = json.loads(live.read_text(encoding="utf-8")).get("dependencies", {})
    key = "@ffmpeg-installer/ffmpeg"
    assert key in deps, f"{key} missing from client/api/package.json"
    assert deps[key] == patched["dependencies"][key], (
        f"{key} is pinned to {patched['dependencies'][key]} in api_patches/ "
        f"but is {deps[key]} in client/api/"
    )


def test_wppconnect_is_frozen_to_the_homologated_live_version():
    """The live client/api/package.json must carry the exact pair declared in
    api_patches/, not the caret range the clone came with — a range here means
    the merge never ran, and the next `npm install` is free to change the
    browser-side send/status APIs this patch set was written against.

    The expected value is read from api_patches/package.json rather than
    written out again, so moving the pair stays a one-file edit.
    """
    live = API / "package.json"
    if not live.exists():
        pytest.skip("client/api/package.json not present")
    deps = json.loads(live.read_text(encoding="utf-8")).get("dependencies", {})
    patched = json.loads((PATCHES / "package.json").read_text(encoding="utf-8"))
    expected = patched["dependencies"]["@wppconnect-team/wppconnect"]
    assert deps.get("@wppconnect-team/wppconnect", "") == expected


# Matches `from './x'`, `import '../y'` and `require('./z')` — only the
# relative forms, since a bare specifier is an npm package resolved from
# node_modules and has nothing to do with the patch set.
_RELATIVE_IMPORT = re.compile(
    r"""(?:from|import|require\()\s*['"](\.[^'"]*)['"]""")

# Resolution order TypeScript itself uses for an extensionless specifier.
_MODULE_SUFFIXES = ("", ".ts", ".tsx", ".d.ts", ".js", ".json",
                    "/index.ts", "/index.js")

# package.json is imported by src/index.ts (for the version string) and lives
# in api_patches/, but is deliberately absent from MIRRORED_FILES: setup_api.py
# merges a few keys into it instead of restoring it wholesale, so there are
# legitimately no two identical copies to compare. See this module's docstring.
_NOT_MIRRORED_BY_DESIGN = {"package.json"}


def _resolve_import(importer_rel: str, spec: str):
    """Where a relative import lands, searched in api_patches/ then api/.

    Returns (rel_path, root) for the first hit, or (None, None). api_patches/
    is searched first because that is the copy setup_api.py restores from — a
    module that exists only under api/ is one the next clean setup will not
    reproduce.
    """
    base = pathlib.PurePosixPath(importer_rel).parent
    target = (base / spec).as_posix()
    # PurePosixPath keeps '..' segments literal; normalise them away.
    parts: list[str] = []
    for part in target.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    candidate = "/".join(parts)
    for root in (PATCHES, API):
        for suffix in _MODULE_SUFFIXES:
            probe = root / (candidate + suffix)
            if probe.is_file():
                return candidate + suffix, root
    return None, None


@pytest.mark.parametrize(
    "rel_path", [f for f in MIRRORED_FILES if f.endswith((".ts", ".js"))])
def test_every_import_of_a_patched_file_resolves(rel_path):
    """A patched file must never import a module the patch set does not carry.

    This is the guard that was missing when client/api_patches/ was first made
    a permanent patch source: the snapshot took a src/routes/index.ts that
    already did `import ... from '../middleware/instrumentation'` but left the
    middleware itself behind. Nothing noticed, because every other test
    compares only the files it already knows about. On any machine whose
    client/api/ predated the split the import still resolved on disk, while a
    clean setup_api.py run — a new dev machine, the CI release build, and the
    ApiSetupDialog flow every end user goes through — produced a tree where
    `tsc` failed with TS2307 and dist/server.js was never emitted at all.

    Note the blind spot this deliberately keeps: resolution also accepts
    client/api/, so a stale local clone that still holds a since-removed file
    passes here. The case that matters is CI, where client/api/ is exactly a
    fresh upstream clone plus the restored patches — the same tree the release
    is built from.
    """
    patch = PATCHES / rel_path
    if not patch.exists():
        pytest.skip(f"client/api_patches/{rel_path} not present")
    if not API.exists():
        pytest.skip("client/api/ not present (API not set up here)")

    source = patch.read_text(encoding="utf-8", errors="replace")
    for spec in sorted(set(_RELATIVE_IMPORT.findall(source))):
        resolved, root = _resolve_import(rel_path, spec)
        assert resolved is not None, (
            f"client/api_patches/{rel_path} imports '{spec}', which exists in "
            f"neither client/api_patches/ nor client/api/. A clean setup_api.py "
            f"run will not produce it, so `npm run build` fails at tsc "
            f"(TS2307) and dist/server.js is never written."
        )
        if root is PATCHES and resolved not in _NOT_MIRRORED_BY_DESIGN:
            assert resolved in MIRRORED_FILES, (
                f"client/api_patches/{rel_path} imports '{spec}' -> {resolved}, "
                f"which lives in api_patches/ but is not restored by "
                f"setup_api.py. Add it to CUSTOM_SRC_FILES (and to "
                f"MIRRORED_FILES here), or the next clean install ships without it."
            )
