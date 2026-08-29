#!/usr/bin/env python3
"""
WinZapp — WPPConnect Server setup script.

Clones the WPPConnect Server repository into client/api/ and optionally checks
out a specific tag. After cloning, follow the build instructions printed at
the end to compile the API before running build.py.

Configuration (via .env at the project root):
  WPPCONNECT_TAG_VERSION  — git tag to pin to. Leave unset or empty to
                            auto-track the latest stable (non-prerelease)
                            release tag instead — this script fetches tags
                            and updates client/api/ to it on every run, even
                            when client/api/ already exists from a previous
                            run.

Usage:
  venv\\Scripts\\python.exe setup_api.py
"""

import json
import os
import re
import shutil
import subprocess
import sys

# ---------------------------------------------------------------------------

ROOT_DIR         = os.path.dirname(os.path.abspath(__file__))
CLIENT_API_DIR   = os.path.join(ROOT_DIR, "client", "api")
API_PATCHES_DIR  = os.path.join(ROOT_DIR, "client", "api_patches")
WPPCONNECT_REPO  = "https://github.com/wppconnect-team/wppconnect-server.git"

# client/core/wppconnect_host_layer_patch.py is the single source of truth
# for the host.layer.js pairing-code patch text — shared with
# ApiSetupDialog (client/ui/dialogs/api_setup.py), which needs the exact
# same patch applied through the real end-user install flow. It's a
# zero-dependency module (no wx, nothing else from client/), so importing
# it here doesn't pull in anything setup_api.py couldn't already run before
# client-side dependencies are installed.
_CLIENT_DIR = os.path.join(ROOT_DIR, "client")
if _CLIENT_DIR not in sys.path:
    sys.path.insert(0, _CLIENT_DIR)

# Files WinZapp patches on top of upstream wppconnect-server. client/api_patches/
# is the permanent, always-git-tracked source of truth for all of these —
# preferred below over whatever (if anything) happens to still be sitting in
# client/api/ right before it gets wiped. That "stash what's currently there"
# fallback used to be the ONLY restore path, and is worthless the moment
# client/api/ is already gone (e.g. a user deletes it before reinstalling,
# reported live as every patch silently regressing to whatever old snapshot
# happened to get stashed months earlier) — client/api_patches/ never has
# that problem since it's never inside the folder that gets deleted.
#
# package.json is NOT in this list — see _merge_package_json_dependencies().
# It used to be a full-file overwrite like the others, which meant its
# "version" field (WPPConnect Server's own self-reported version — what
# WppUpdateChecker compares against the latest GitHub release) came from
# whatever was checked into api_patches/package.json at the time, not from
# whatever tag was actually cloned/checked out here. Reported live: WinZapp
# insisting its installed version was still 2.10.0 on a build that had
# genuinely cloned/built 2.10.1, because api_patches/package.json's own
# "version" field had gone stale.
CUSTOM_ROOT_FILES = [
    "start.js",
    "config.json",
    ".eslintrc.json",
    ".prettierrc",
    ".prettierignore",
    "jest.config.js",
]
CUSTOM_SRC_FILES = [
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
    "decrypt.js",
]


def _load_env() -> dict:
    """Parse the root .env file and return a key→value dict."""
    env_path = os.path.join(ROOT_DIR, ".env")
    result = {}
    if not os.path.isfile(env_path):
        return result
    with open(env_path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def _run(cmd: list, cwd: str = None):
    # subprocess.run() with a bare command name (no shell=True, no
    # explicit .cmd/.exe suffix) can fail on Windows with
    # "[WinError 2] The system cannot find the file specified" for
    # commands installed as a .cmd/.bat shim — npm is the common case,
    # since Windows npm installs as npm.cmd, not npm.exe, and Windows'
    # CreateProcess (which subprocess.run ultimately uses) doesn't apply
    # PATHEXT resolution the way cmd.exe itself does. Resolving through
    # shutil.which() first finds the real .cmd/.exe path PATH would give
    # you, sidestepping the issue without needing shell=True (which has
    # its own quoting/injection concerns).
    if cmd and isinstance(cmd[0], str) and not os.path.isabs(cmd[0]):
        resolved = shutil.which(cmd[0])
        if resolved:
            cmd = [resolved] + cmd[1:]
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        print(f"\n[ERROR] Command failed (exit {result.returncode}).")
        sys.exit(result.returncode)


def _latest_stable_tag(cwd: str) -> str:
    """Return the highest vX.Y.Z release tag reachable from origin, or "" if
    none is found (e.g. offline, or no tags match the vX.Y.Z pattern —
    pre-release tags like v2.10.4-rc.1 are deliberately excluded so this
    never auto-updates a dev/CI setup onto an unstable release).
    """
    result = subprocess.run(
        ["git", "fetch", "--tags", "--force"], cwd=cwd,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[WARNING] git fetch --tags failed — skipping auto-update check.\n{result.stderr}")
        return ""
    result = subprocess.run(
        ["git", "tag", "--list", "v*"], cwd=cwd, capture_output=True, text=True
    )
    candidates = []
    for line in result.stdout.splitlines():
        tag = line.strip()
        m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag)
        if m:
            candidates.append((tuple(int(x) for x in m.groups()), tag))
    if not candidates:
        return ""
    candidates.sort()
    return candidates[-1][1]


def _current_tag(cwd: str) -> str:
    """Return the tag the working tree is currently checked out at exactly,
    or "" if HEAD isn't exactly on a tag (detached at an arbitrary commit,
    or on a branch)."""
    result = subprocess.run(
        ["git", "describe", "--tags", "--exact-match"], cwd=cwd,
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


# The only dependency entries WinZapp actually overrides on top of whatever
# upstream wppconnect-server ships for a given tag. Deliberately a narrow,
# explicit list rather than merging api_patches/package.json's entire
# "dependencies" block wholesale — the latter would also silently roll back
# every OTHER dependency to whatever version happened to be frozen in
# api_patches/ at some earlier point, undoing legitimate upstream bumps on
# every future tag this script prepares.
_PATCHED_DEPENDENCY_KEYS = [
    "prom-client",  # imported by src/middleware/instrumentation.ts, which is
                    # WinZapp's own patch. Upstream happens to declare it too,
                    # but under devDependencies — so our production import is
                    # satisfied today only because both installers run a plain
                    # `npm install`. Listing it here means the merge writes it
                    # into dependencies regardless of what upstream does with
                    # its own copy. npm accepts the entry appearing in both
                    # blocks (verified with `npm install --dry-run`).
    "zod",  # runtime schema for the sync endpoints' response contracts
            # (src/dto/sync.ts). Declared here because the controllers import
            # it at runtime — it is not a build-only tool.
    "@ffmpeg-installer/ffmpeg",  # vendors a real ffmpeg binary via npm — WinZapp's
                                  # own Python side shells out to it directly
                                  # (main.py: _find_api_ffmpeg/_convert_wav_to_ogg)
                                  # to encode voice messages to OGG/Opus; upstream
                                  # wppconnect-server does not declare it at all.
]

# @wppconnect-team/wppconnect used to be pinned here too, to an exact version
# ("2.2.4") that predated this comment. That went stale fast: this dependency
# releases new patch versions multiple times a week, and wppconnect-server's
# own main branch had already moved on to requiring "^2.2.6" — meaning a fresh
# clone/build was running WPPConnect Server against an @wppconnect-team/wppconnect
# release two patches behind what it was actually written and tested against,
# silently, with no error anywhere.
#
# The fix is to not patch it at all: leave upstream's own declared range in
# package.json exactly as the clone/checkout produced it, the same way every
# OTHER unpinned dependency already works here. @wppconnect/wa-js and
# @wppconnect/wa-version are never pinned by WinZapp either — they are pulled
# in transitively through whatever @wppconnect-team/wppconnect version resolves,
# so they now track the paired version automatically instead of needing to be
# kept in sync by hand. This mirrors start.js's own resolveWhatsappVersion(),
# which resolves the WhatsApp Web build version dynamically for exactly the
# same reason ("Rather than hardcoding a version — which rots...").


def _recover_upstream_package_json():
    """Bring client/api/package.json back when it went missing from a clone
    that already exists.

    _merge_package_json_dependencies() patches the file in place and bails out
    silently when it isn't there, which is right for a fresh clone (npm install
    would run against upstream's own file a moment later) but leaves nothing to
    run at all when the file was deleted from an existing client/api/ — npm
    then dies with `ENOENT: no such file or directory, open '.../package.json'`
    and the whole setup aborts. That is not hypothetical: `git rm`-ing the
    client/api/ copies out of WinZapp's own repo deletes them from every
    developer's working tree on the next pull, package.json included.

    client/api/ is itself a clone of wppconnect-server, so its own git checkout
    is the correct source — that restores the real upstream file for the tag on
    disk, which is exactly what the merge below expects to patch. api_patches/
    is only a last resort here: its "version" field is a frozen snapshot, and
    handing that to WppUpdateChecker is the stale-version bug described above.
    """
    pkg_path = os.path.join(CLIENT_API_DIR, "package.json")
    if os.path.isfile(pkg_path):
        return
    if os.path.isdir(os.path.join(CLIENT_API_DIR, ".git")):
        print("[WARNING] client/api/package.json is missing — restoring it from the clone.")
        result = subprocess.run(
            ["git", "checkout", "--", "package.json"], cwd=CLIENT_API_DIR
        )
        if result.returncode == 0 and os.path.isfile(pkg_path):
            print("[OK] Restored client/api/package.json from upstream.")
            return
        print(f"[WARNING] git checkout of package.json failed (exit {result.returncode}).")
    patch_path = os.path.join(API_PATCHES_DIR, "package.json")
    if os.path.isfile(patch_path):
        import shutil as _shutil
        _shutil.copy2(patch_path, pkg_path)
        print(
            "[WARNING] Copied client/api_patches/package.json as a fallback — its "
            "'version' field is a frozen snapshot and may not match the tag on disk."
        )


def _restore_custom_files(custom_contents: dict):
    """Write every patched file back into client/api/.

    Runs on EVERY path through main(), not just after a clone or a tag
    checkout. It used to be called only inside those two branches, so the most
    common invocation of all — `python setup_api.py` against a client/api/ that
    already exists, with no WPPCONNECT_TAG_VERSION set — skipped it entirely
    and rebuilt the API from whatever happened to be on disk. Two ways that
    bites: a patched file edited/deleted directly in client/api/ was never put
    back (npm run build then compiled vanilla upstream, silently), and a
    client/api/ missing its patches could not be repaired by re-running this
    script at all, which is the one thing it exists to do.
    """
    for rel_path, content in custom_contents.items():
        dest_path = os.path.join(CLIENT_API_DIR, rel_path)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(content)
        print(f"[INFO] Restored custom file: {rel_path}")


from core.wppconnect_host_layer_patch import (
    patch_host_layer_source as _patch_host_layer_source,
)
from core.wppconnect_status_layer_patch import ALL_PATCHES as _STATUS_LAYER_PATCHES
from core.wppconnect_sender_layer_patch import ALL_PATCHES as _SENDER_LAYER_PATCHES
from core.wppconnect_sender_layer_patch import patch_sender_layer_source as _patch_sender_layer_source
from core.wppconnect_welcome_layer_patch import ALL_PATCHES as _WELCOME_LAYER_PATCHES


def _patch_wppconnect_host_layer(client_api_dir: str = None) -> bool:
    """Patch @wppconnect-team/wppconnect's compiled host.layer.js: the
    phone-number pairing code must stop regenerating on every QR-code
    rotation WITHOUT freezing forever if it should ever need a refresh
    (WinZapp issue #8), and a failed pairing-code request must report the
    real browser-side error rather than the minified "t: t" that crossing
    the CDP exception boundary raw produces. See
    client/core/wppconnect_host_layer_patch.py's module docstring for the
    full v0 (upstream bug)/v1 (unsafe)/v2/v3 history.

    This lives in node_modules (a vendored dependency of WPPConnect Server,
    not WPPConnect Server itself), so it can't go through the
    api_patches/ full-file-restore mechanism — npm install rebuilds
    node_modules from scratch every time, so this must run AFTER npm
    install, same as the existing decrypt.js patch right above this call.

    The actual search-and-replace lives in patch_host_layer_source() so
    this and ApiSetupDialog's copy can't drift apart. Idempotent, and
    best-effort: if the installed wppconnect version no longer matches a
    known source text (e.g. a future upstream release), it warns and leaves
    that part of the file untouched rather than corrupting it or crashing.
    """
    if client_api_dir is None:
        client_api_dir = CLIENT_API_DIR
    host_layer_path = os.path.join(
        client_api_dir, "node_modules", "@wppconnect-team", "wppconnect",
        "dist", "api", "layers", "host.layer.js",
    )
    if not os.path.isfile(host_layer_path):
        print("[WARNING] host.layer.js not found — skipping pairing-code patch.")
        return False

    with open(host_layer_path, encoding="utf-8") as f:
        content = f.read()

    patched, notes, ok = _patch_host_layer_source(content)
    if patched != content:
        with open(host_layer_path, "w", encoding="utf-8") as f:
            f.write(patched)

    for note in notes:
        if "DID NOT MATCH" in note:
            print(
                f"[WARNING] host.layer.js — {note} The installed "
                "@wppconnect-team/wppconnect version may have changed this "
                "file; please report this to the WinZapp maintainers."
            )
        else:
            print(f"[OK] host.layer.js — {note}")
    return ok


def _patch_wppconnect_status_layer(client_api_dir: str = None) -> bool:
    """Patch @wppconnect-team/wppconnect's compiled status.layer.js so
    posting a status (text/image/video) actually reports whether it
    succeeded, instead of always reporting success — see
    client/core/wppconnect_status_layer_patch.py's module docstring for the
    root cause (a missing async/await/return in three methods there,
    inconsistent with every other evaluateAndReturn() call in this same
    package).

    Idempotent (each of the three patches is independently a no-op once
    applied) and best-effort — a mismatched method is logged and skipped
    rather than corrupting the file.
    """
    if client_api_dir is None:
        client_api_dir = CLIENT_API_DIR
    status_layer_path = os.path.join(
        client_api_dir, "node_modules", "@wppconnect-team", "wppconnect",
        "dist", "api", "layers", "status.layer.js",
    )
    if not os.path.isfile(status_layer_path):
        print("[WARNING] status.layer.js not found — skipping status-posting-result patch.")
        return False

    with open(status_layer_path, encoding="utf-8") as f:
        content = f.read()

    applied = 0
    already = 0
    missing = 0
    for original, patched in _STATUS_LAYER_PATCHES:
        if patched in content:
            already += 1
        elif original in content:
            content = content.replace(original, patched, 1)
            applied += 1
        else:
            missing += 1

    if applied:
        with open(status_layer_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[OK] Patched status.layer.js — {applied} status-posting method(s) now correctly report success/failure.")
    elif already == len(_STATUS_LAYER_PATCHES):
        print("[INFO] status.layer.js status-posting-result patch already applied.")
    if missing:
        print(
            f"[WARNING] status.layer.js: {missing} status-posting method(s) did not match "
            "the expected upstream source — skipping those (the installed "
            "@wppconnect-team/wppconnect version may have changed this file)."
        )
    return missing == 0


def _patch_wppconnect_sender_layer(client_api_dir: str = None) -> bool:
    """Patch @wppconnect-team/wppconnect's compiled sender.layer.js so a
    failed sendFile() (e.g. every video-message send, currently) reports
    the real browser-side error instead of the opaque, minified
    {"name":"t","message":"t"} that crossing the CDP exception boundary
    raw currently produces — see
    client/core/wppconnect_sender_layer_patch.py's module docstring.

    Idempotent and best-effort, same pattern as the host/status layer
    patches right above.
    """
    if client_api_dir is None:
        client_api_dir = CLIENT_API_DIR
    sender_layer_path = os.path.join(
        client_api_dir, "node_modules", "@wppconnect-team", "wppconnect",
        "dist", "api", "layers", "sender.layer.js",
    )
    if not os.path.isfile(sender_layer_path):
        print("[WARNING] sender.layer.js not found — skipping sendFile error-detail patch.")
        return False

    with open(sender_layer_path, encoding="utf-8") as f:
        content = f.read()

    applied = 0
    already = 0
    missing = 0
    for original, patched in _SENDER_LAYER_PATCHES:
        if patched in content:
            already += 1
        elif original in content:
            content = content.replace(original, patched, 1)
            applied += 1
        else:
            missing += 1

    # patch_sender_layer_source() additionally migrates a couple of
    # transitional intermediate states from this patch's own iterative
    # development (a chunked-upload variant missing the MediaGatingUtils
    # 1 GB override, and an even earlier chunking-threshold marker) that
    # the literal ALL_PATCHES pairs above don't cover on their own — see
    # that function's own docstring. Idempotent no-op on content the loop
    # above already brought fully up to date.
    migrated = _patch_sender_layer_source(content)
    if migrated != content:
        content = migrated
        applied += 1

    if applied:
        with open(sender_layer_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[OK] Patched sender.layer.js — {applied} sendFile error(s) now report real detail instead of minified junk.")
    elif already == len(_SENDER_LAYER_PATCHES):
        print("[INFO] sender.layer.js sendFile error-detail patch already applied.")
    if missing:
        print(
            f"[WARNING] sender.layer.js: {missing} method(s) did not match "
            "the expected upstream source — skipping those (the installed "
            "@wppconnect-team/wppconnect version may have changed this file)."
        )
    return missing == 0


def _patch_wppconnect_welcome_layer(client_api_dir: str = None) -> bool:
    """Patch @wppconnect-team/wppconnect's compiled controllers/welcome.js
    so a plain CommonJS require() of the ESM-only `latest-version` package
    doesn't crash the whole server on startup (Node 20+) — see
    client/core/wppconnect_welcome_layer_patch.py's module docstring for
    why the replacement must be a bare function, not `{ default: fn }`.

    Idempotent and best-effort, same pattern as the other layer patches
    above.
    """
    if client_api_dir is None:
        client_api_dir = CLIENT_API_DIR
    welcome_layer_path = os.path.join(
        client_api_dir, "node_modules", "@wppconnect-team", "wppconnect",
        "dist", "controllers", "welcome.js",
    )
    if not os.path.isfile(welcome_layer_path):
        print("[WARNING] welcome.js not found — skipping latest-version ESM patch.")
        return False

    with open(welcome_layer_path, encoding="utf-8") as f:
        content = f.read()

    applied = 0
    already = 0
    missing = 0
    for original, patched in _WELCOME_LAYER_PATCHES:
        if patched in content:
            already += 1
        elif original in content:
            content = content.replace(original, patched, 1)
            applied += 1
        else:
            missing += 1

    if applied:
        with open(welcome_layer_path, "w", encoding="utf-8") as f:
            f.write(content)
        print("[OK] Patched welcome.js — latest-version ESM require no longer crashes startup on Node 20+.")
    elif already == len(_WELCOME_LAYER_PATCHES):
        print("[INFO] welcome.js latest-version ESM patch already applied.")
    if missing:
        print(
            f"[WARNING] welcome.js: {missing} pattern(s) did not match "
            "the expected upstream source — skipping those (the installed "
            "@wppconnect-team/wppconnect version may have changed this file)."
        )
    return missing == 0


def _merge_package_json_dependencies():
    """Apply WinZapp's specific dependency patches onto whatever
    package.json the clone/checkout actually left on disk, instead of
    overwriting the whole file. Only the keys in _PATCHED_DEPENDENCY_KEYS are
    copied in from client/api_patches/package.json — "version", "name",
    scripts, and every other dependency all come from the real checked-out
    file, so WinZapp's own version-check (WppUpdateChecker /
    _get_installed_wpp_version()) keeps reflecting whatever was genuinely
    cloned/built rather than a value frozen in api_patches/ at some earlier
    point in time.
    """
    pkg_path = os.path.join(CLIENT_API_DIR, "package.json")
    patch_path = os.path.join(API_PATCHES_DIR, "package.json")
    if not (os.path.isfile(pkg_path) and os.path.isfile(patch_path)):
        return
    try:
        with open(pkg_path, encoding="utf-8") as f:
            pkg = json.load(f)
        with open(patch_path, encoding="utf-8") as f:
            patch = json.load(f)
    except Exception as e:
        print(f"[WARNING] Failed to merge package.json dependency patches: {e}")
        return
    patch_deps = patch.get("dependencies", {})
    deps = pkg.setdefault("dependencies", {})
    applied = 0
    for key in _PATCHED_DEPENDENCY_KEYS:
        if key in patch_deps:
            deps[key] = patch_deps[key]
            applied += 1
    with open(pkg_path, "w", encoding="utf-8") as f:
        json.dump(pkg, f, indent=2)
        f.write("\n")
    print(f"[INFO] Applied {applied} patched dependencies into package.json (version kept at {pkg.get('version', '?')})")


def main():
    env = _load_env()
    tag = env.get("WPPCONNECT_TAG_VERSION", "").strip()

    git_dir = os.path.join(CLIENT_API_DIR, ".git")
    already_cloned = os.path.isdir(git_dir)

    # Gather the content to restore for every patched file, preferring
    # client/api_patches/ (permanent, always-tracked) over whatever
    # happens to still be sitting in client/api/ right now — the latter
    # is worthless as a source the moment client/api/ has already been
    # deleted, which is exactly when this restore matters most.
    #
    # Loaded up front, before the clone branch, because the single
    # _restore_custom_files() call at the end of main() consumes it on every
    # path — including the one that wipes and re-clones client/api/, which is
    # why it has to be read into memory *before* that happens. It used to be
    # populated only on the clone path, so checking out a tag against an
    # existing client/api/ raised NameError — and had that line been reached
    # with an empty dict instead, it would have been worse: `git checkout -f`
    # overwrites the patched files with upstream's, and nothing would have put
    # ours back.
    custom_contents = {}
    for rel_path in CUSTOM_ROOT_FILES + CUSTOM_SRC_FILES:
        patches_path = os.path.join(API_PATCHES_DIR, rel_path)
        stash_path = os.path.join(CLIENT_API_DIR, rel_path)
        if os.path.isfile(patches_path):
            with open(patches_path, "rb") as f:
                custom_contents[rel_path] = f.read()
            print(f"[INFO] Loaded {rel_path} from client/api_patches/")
        elif os.path.isfile(stash_path):
            with open(stash_path, "rb") as f:
                custom_contents[rel_path] = f.read()
            print(f"[INFO] client/api_patches/{rel_path} not found — stashed current client/api/{rel_path} instead")

    if already_cloned:
        print(f"[INFO] client/api/ already exists — skipping clone (checking for updates below).")
    else:
        print(f"[INFO] Cloning WPPConnect Server …")
        import shutil
        temp_node_modules = os.path.join(ROOT_DIR, "temp_node_modules")
        node_modules_path = os.path.join(CLIENT_API_DIR, "node_modules")
        has_node_modules = os.path.isdir(node_modules_path)
        if has_node_modules:
            try:
                if os.path.exists(temp_node_modules):
                    shutil.rmtree(temp_node_modules)
                shutil.move(node_modules_path, temp_node_modules)
                print("[INFO] Temporarily moved node_modules to preserve cache.")
            except Exception as e:
                print(f"[WARNING] Failed to move node_modules: {e}")
                has_node_modules = False

        if os.path.isdir(CLIENT_API_DIR):
            try:
                shutil.rmtree(CLIENT_API_DIR)
            except Exception as e:
                print(f"[WARNING] Failed to remove client/api: {e}")
        os.makedirs(os.path.dirname(CLIENT_API_DIR), exist_ok=True)
        _run(["git", "clone", WPPCONNECT_REPO, CLIENT_API_DIR])

        if has_node_modules:
            try:
                shutil.move(temp_node_modules, os.path.join(CLIENT_API_DIR, "node_modules"))
                print("[INFO] Restored node_modules cache successfully.")
            except Exception as e:
                print(f"[WARNING] Failed to restore node_modules: {e}")

    if tag:
        current = _current_tag(CLIENT_API_DIR)
        if current == tag:
            print(f"[INFO] Already pinned to {tag}.")
        else:
            print(f"[INFO] WPPCONNECT_TAG_VERSION pinned — checking out {tag}.")
            _run(["git", "fetch", "--tags", "--force"], cwd=CLIENT_API_DIR)
            _run(["git", "checkout", "-f", tag], cwd=CLIENT_API_DIR)
    else:
        latest = _latest_stable_tag(CLIENT_API_DIR)
        if not latest:
            print("[INFO] No stable release tag found (offline or no tags) — using default branch (main).")
        else:
            current = _current_tag(CLIENT_API_DIR)
            if current == latest:
                print(f"[INFO] Already at the latest stable release ({latest}).")
            else:
                if current:
                    print(f"[INFO] Newer WPPConnect Server release found: {current} -> {latest}. Updating...")
                else:
                    print(f"[INFO] No WPPCONNECT_TAG_VERSION pinned — using latest stable release {latest}.")
                _run(["git", "checkout", "-f", latest], cwd=CLIENT_API_DIR)

    # Single restore point, deliberately outside every branch above: the clone,
    # the tag checkout (`git checkout -f` overwrites the patched files with
    # upstream's) and the plain "client/api/ is already here" path all need the
    # exact same thing done afterwards, and only the first two used to get it.
    _recover_upstream_package_json()
    _restore_custom_files(custom_contents)
    _merge_package_json_dependencies()

    print()
    print("[OK] WPPConnect Server ready at client/api/")
    print()

    # Platform-specific installations
    is_windows = sys.platform == "win32"

    # 1. Automating Node dependency installation and build
    print("[INFO] Automating Node.js dependency installation and compilation...")
    try:
        # Determine node/npm command
        # On Windows, check if portable node exists in client/node/node.exe
        node_bin = "node"
        npm_bin = "npm"
        if is_windows:
            win_node = os.path.join(ROOT_DIR, "client", "node", "node.exe")
            if os.path.isfile(win_node):
                node_bin = win_node
                # Try to locate npm CLI
                win_npm = os.path.join(ROOT_DIR, "client", "node", "node_modules", "npm", "bin", "npm-cli.js")
                if os.path.isfile(win_npm):
                    npm_bin = win_npm

        # Run npm install
        print("[INFO] Running npm install...")
        if npm_bin.endswith("npm-cli.js"):
            _run([node_bin, npm_bin, "install", "--no-audit", "--no-fund", "--legacy-peer-deps"], cwd=CLIENT_API_DIR)
        else:
            _run([npm_bin, "install", "--no-audit", "--no-fund", "--legacy-peer-deps"], cwd=CLIENT_API_DIR)

        # Apply the RangeError/memory-leak patch to @wppconnect-team/wppconnect decrypt.js by copying our modified file
        try:
            import shutil as _shutil
            custom_decrypt = os.path.join(CLIENT_API_DIR, "decrypt.js")
            decrypt_js_path = os.path.join(CLIENT_API_DIR, "node_modules", "@wppconnect-team", "wppconnect", "dist", "api", "helpers", "decrypt.js")
            if os.path.isfile(custom_decrypt):
                print("[INFO] Copying custom decrypt.js patch to node_modules...")
                # Ensure the destination directory exists (should exist due to npm install)
                os.makedirs(os.path.dirname(decrypt_js_path), exist_ok=True)
                _shutil.copy2(custom_decrypt, decrypt_js_path)
                print("[OK] Copied decrypt.js patch successfully.")
            else:
                print("[WARNING] Custom decrypt.js patch not found in client/api. Skipping patch.")
        except Exception as e:
            print(f"[WARNING] Failed to copy decrypt.js patch: {e}")

        # Slow down phone-number pairing-code rotation (WinZapp issue #8) —
        # see _patch_wppconnect_host_layer()'s docstring for the upstream bug.
        try:
            _patch_wppconnect_host_layer()
        except Exception as e:
            print(f"[WARNING] Failed to patch host.layer.js pairing-code rotation: {e}")

        # Status posting always reported success regardless of whether it
        # actually worked — see _patch_wppconnect_status_layer()'s docstring.
        try:
            _patch_wppconnect_status_layer()
        except Exception as e:
            print(f"[WARNING] Failed to patch status.layer.js posting-result reporting: {e}")

        # Failed video sends only ever logged opaque minified junk — see
        # _patch_wppconnect_sender_layer()'s docstring.
        try:
            _patch_wppconnect_sender_layer()
        except Exception as e:
            print(f"[WARNING] Failed to patch sender.layer.js sendFile error detail: {e}")

        # A CommonJS require() of the ESM-only `latest-version` package
        # crashes the whole server at startup on Node 20+ — see
        # _patch_wppconnect_welcome_layer()'s docstring.
        try:
            _patch_wppconnect_welcome_layer()
        except Exception as e:
            print(f"[WARNING] Failed to patch welcome.js latest-version ESM require: {e}")

        # Download Chromium (Puppeteer postinstall)
        print("[INFO] Downloading Chromium (Puppeteer)...")
        install_js = os.path.join(CLIENT_API_DIR, "node_modules", "puppeteer", "install.mjs")
        if os.path.isfile(install_js):
            _run([node_bin, install_js], cwd=CLIENT_API_DIR)
        else:
            print("[WARNING] puppeteer install.mjs not found. Attempting fallback browser download...")
            _run([npm_bin, "run", "postinstall"], cwd=CLIENT_API_DIR)

        # Run npm run build
        print("[INFO] Compiling WPPConnect Server...")
        if npm_bin.endswith("npm-cli.js"):
            _run([node_bin, npm_bin, "run", "build"], cwd=CLIENT_API_DIR)
        else:
            _run([npm_bin, "run", "build"], cwd=CLIENT_API_DIR)

        print("[OK] WPPConnect Server dependencies installed and built successfully.")

    except Exception as e:
        print(f"[ERROR] Node.js dependencies installation/build failed: {e}")
        print("Please resolve the error above or install manually by running:")
        print(f"  cd {CLIENT_API_DIR}")
        print("  npm install")
        print("  npm run build")
        # This used to only print the error and fall through: setup_api.py
        # exited 0 either way, so a failed/partial `npm run build` silently
        # left whatever dist/server.js already happened to be on disk (stale,
        # or from a much older checkout) in place. build.py only checks that
        # dist/server.js *exists*, not that it matches the current src/patches
        # — so that stale build got shipped in a release without any warning.
        # Failing loudly here is what actually surfaces the problem.
        sys.exit(1)

    # 2. Linux OS dependencies installation (Debian/Ubuntu)
    if not is_windows:
        print("\n[INFO] Detecting Linux OS and installing system dependencies for Chromium...")
        # Check if apt-get is available
        import shutil
        if shutil.which("apt-get"):
            # Check if running as root or has sudo
            try:
                getuid = os.getuid
            except AttributeError:
                getuid = lambda: -1
            is_root = getuid() == 0
            apt_cmd = ["apt-get", "update"]
            install_cmd = [
                "apt-get", "install", "-y", "--no-install-recommends",
                "ca-certificates", "fonts-liberation", "libasound2", "libatk-bridge2.0-0",
                "libatk1.0-0", "libc6", "libcairo2", "libcups2", "libdbus-1-3", "libdrm2", "libexpat1",
                "libfontconfig1", "libgbm1", "libglib2.0-0", "libgtk-3-0", "libnspr4",
                "libnss3", "libpango-1.0-0", "libpangocairo-1.0-0", "libstdc++6", "libx11-6",
                "libx11-xcb1", "libxcb1", "libxcomposite1", "libxcursor1", "libxdamage1",
                "libxext6", "libxfixes3", "libxi6", "libxkbcommon0", "libxrandr2", "libxrender1", "libxshmfence1", "libxss1",
                "libxtst6", "lsb-release", "xdg-utils", "wget"
            ]
            if not is_root:
                if shutil.which("sudo"):
                    print("[INFO] Requesting root privileges via sudo for apt-get...")
                    apt_cmd = ["sudo"] + apt_cmd
                    install_cmd = ["sudo", "env", "DEBIAN_FRONTEND=noninteractive"] + install_cmd
                else:
                    print("[WARNING] Not running as root and sudo is not available. Please install system dependencies manually:")
                    print("  apt-get update && apt-get install -y libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libasound2 libpango-1.0-0 libpangocairo-1.0-0 libxshmfence1")
                    apt_cmd = None

            if apt_cmd:
                try:
                    # Set noninteractive environment variable
                    os.environ["DEBIAN_FRONTEND"] = "noninteractive"
                    print("[INFO] Updating package lists...")
                    subprocess.run(apt_cmd, check=True)
                    print("[INFO] Installing system libraries for Chrome/Puppeteer...")
                    subprocess.run(install_cmd, check=True)
                    print("[OK] Linux system dependencies for Chromium installed successfully!")
                except Exception as e:
                    print(f"[WARNING] Failed to automatically install system packages: {e}")
                    print("Please install them manually using:")
                    print("  sudo apt-get update && sudo apt-get install -y libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libasound2 libpango-1.0-0 libpangocairo-1.0-0 libxshmfence1")
        else:
            print("[INFO] Package manager apt-get not found (non-Debian/Ubuntu system).")
            print("Please ensure your system has all required Chromium dependencies installed:")
            print("https://pptr.dev/troubleshooting#chrome-headless-doesnt-launch-on-unix")


if __name__ == "__main__":
    main()
