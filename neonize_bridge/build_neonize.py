"""Builds the neonize package the Neonize bridge runs: official neonize at the
approved release, plus WinZapp's patches, compiled into the Windows DLL.

    python neonize_bridge/build_neonize.py all --install   # what most people want
    python neonize_bridge/build_neonize.py prepare         # workspace from the patches
    python neonize_bridge/build_neonize.py export          # workspace commits -> patches
    python neonize_bridge/build_neonize.py generate        # protobuf code from Neonize.proto
    python neonize_bridge/build_neonize.py build           # DLL + package + zip in dist/
    python neonize_bridge/build_neonize.py install [--venv DIR]
    python neonize_bridge/build_neonize.py check --ref 0.6.1        # do the patches fit?
    python neonize_bridge/build_neonize.py approve --ref 0.6.1 --version 0.6.1

`neonize_build/` (git-ignored) is the workspace: `neonize/`, `whatsmeow/` and
`meowcaller/` are git checkouts whose commits on the `winzapp` branch are the
patches (tag `upstream` is where they start), `tools/` the pinned protobuf
toolchain and `dist/` the output. Change the code there, commit, run
`export`, and commit the patches in this repository. The whole flow, and how
to move to a new upstream release: docs/reference/neonize-build.md.

Needs git, Go (the version neonize's go.mod asks) and a 64-bit MinGW gcc on
the PATH (`winget install GoLang.Go BrechtSanders.WinLibs.POSIX.UCRT`).
"""
import argparse
import datetime
import glob
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import urllib.request
import zipfile

import neonize_upstream as upstream

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "neonize_build")
PATCHES = os.path.join(HERE, "neonize_patches")
TOOLS = os.path.join(WORK, "tools")
DIST = os.path.join(WORK, "dist")
TREES = ("neonize", "whatsmeow", "meowcaller")
BRANCH, BASE = "winzapp", "upstream"
# committer of the workspace commits (the author comes from each patch)
GIT_ID = ["-c", "user.name=WinZapp build", "-c", "user.email=build@winzapp.invalid"]
GIT_CONFIG = {"core.autocrlf": "false", "core.longpaths": "true", "advice.detachedHead": "false"}


def say(text):
    print(f"[build_neonize] {text}", flush=True)


def run(cmd, cwd=None, env=None, capture=False):
    result = subprocess.run(cmd, cwd=cwd, env=env, text=True,
                            capture_output=capture, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() if capture else ""
        sys.exit(f"Command failed ({result.returncode}): {' '.join(cmd)}\n{detail}")
    return result.stdout.strip() if capture else ""


def git(tree, *args, capture=False):
    return run(["git", *GIT_ID, *args], cwd=tree, capture=capture)


def tree_path(name):
    return os.path.join(WORK, name)


def manifest():
    return upstream.load(os.path.join(HERE, upstream.MANIFEST))


# ------------------------------------------------------------------ prepare
def format_patches(name, target):
    tree = tree_path(name)
    excludes = [f":(exclude){path}" for path in upstream.GENERATED] if name == "neonize" else []
    git(tree, "format-patch", "-q", "--zero-commit", "--no-signature", "--no-numbered", "-o", target,
        f"{BASE}..{BRANCH}", "--", ".", *excludes)


def _patch_files(folder):
    found = {}
    for path in sorted(glob.glob(os.path.join(folder, "*.patch"))):
        with open(path, "rb") as f:
            found[os.path.basename(path)] = f.read()
    return found


def _uncommitted(tree):
    listing = subprocess.run(["git", "status", "--porcelain", "-z"], cwd=tree, check=True,
                             capture_output=True, text=True).stdout
    return upstream.uncommitted(listing)


def matches_patches(name):
    """Do this tree's commits export to exactly the files in neonize_patches/?
    (export is deterministic, so the comparison is byte for byte)"""
    probe = os.path.join(WORK, f".probe-{name}")
    shutil.rmtree(probe, ignore_errors=True)
    try:
        format_patches(name, probe)
        return _patch_files(probe) == _patch_files(os.path.join(PATCHES, name))
    finally:
        shutil.rmtree(probe, ignore_errors=True)


#: exit code of `export` refusing a workspace older than the repository's patches
PATCHES_MOVED = 5


def _commit_time(cwd, *args):
    result = subprocess.run(["git", "log", "-1", "--format=%ct", *args], cwd=cwd,
                            capture_output=True, text=True)
    try:
        return int(result.stdout.strip() or 0) if result.returncode == 0 else 0
    except ValueError:
        return 0


def patches_moved(name):
    """True when neonize_patches/<name> changed in the repository after this
    tree was prepared, and the tree does not hold that change.

    Exporting such a tree writes its older commits over the newer patches, and
    building it builds the older code. Seen after pulling #732 (the call layer
    hooked back into Neonize()): the workspace was still at the commits of
    the patches before it, at the same pinned neonize version, so the version
    check passed, `export` put the old 0002 back and the DLL had no calls.

    The tree's newest commit is when `prepare` applied the patches (git am
    stamps the committer date), or a later commit of its own; the repository's
    newest commit under neonize_patches/<name> is when the patches last
    changed. Not a git checkout, or no workspace: never moved.
    """
    tree = tree_path(name)
    if not os.path.isdir(os.path.join(tree, ".git")):
        return False
    repo = _commit_time(HERE, "--", os.path.join(PATCHES, name))
    work = _commit_time(tree, BRANCH)
    if not repo or not work or repo <= work:
        return False
    return not matches_patches(name)


def moved_trees():
    return [name for name in TREES if patches_moved(name)]


def back_up_workspace(folder=None):
    """Keep every tree's commits as patches before a prepare replaces them."""
    folder = folder or os.path.join(WORK, "stale-backup")
    shutil.rmtree(folder, ignore_errors=True)
    for name in TREES:
        if os.path.isdir(os.path.join(tree_path(name), ".git")):
            try:
                format_patches(name, os.path.join(folder, name))
            except SystemExit:
                continue
    say(f"the workspace's commits are kept in {folder}")


def ensure_clean(name):
    """Refuse to throw away work: uncommitted changes outside the generated files,
    or workspace commits that are not exported yet."""
    tree = tree_path(name)
    if not os.path.isdir(os.path.join(tree, ".git")):
        return
    dirty = _uncommitted(tree)
    if dirty:
        sys.exit(f"{tree} has uncommitted changes ({', '.join(dirty[:5])}). Commit them and "
                 "run `export`, or pass --force to discard them.")
    if not matches_patches(name):
        sys.exit(f"{tree} has commits that are not in neonize_patches/{name}: run `export` "
                 "first, or pass --force to discard them.")


def fresh_repo(tree):
    if os.path.isdir(tree):
        shutil.rmtree(tree, onexc=_writable_retry)
    os.makedirs(tree)
    run(["git", "init", "-q"], cwd=tree)
    for key, value in GIT_CONFIG.items():
        run(["git", "config", key, value], cwd=tree)


def _writable_retry(function, path, _error):
    os.chmod(path, stat.S_IWRITE)          # the Go module cache is read-only
    function(path)


def apply_patches(tree, name):
    patches = sorted(glob.glob(os.path.join(PATCHES, name, "*.patch")))
    git(tree, "checkout", "-q", "-f", "-B", BRANCH, BASE)
    for patch in patches:
        result = subprocess.run(["git", *GIT_ID, "am", "-q", "--3way", "--keep-cr", patch],
                                cwd=tree, text=True, capture_output=True)
        if result.returncode != 0:
            sys.exit(f"{os.path.basename(patch)} does not apply to {name}:\n{result.stdout}"
                     f"{result.stderr}\nResolve it in {tree} (git am --continue), then `export`.")
    say(f"{name}: {len(patches)} patch(es) applied")


def prepare_git(name, url, ref, label, force):
    tree = tree_path(name)
    if not force:
        ensure_clean(name)
    fresh_repo(tree)
    run(["git", "remote", "add", "origin", url], cwd=tree)
    run(["git", "fetch", "-q", "--depth", "1", "origin", ref], cwd=tree)
    commit = run(["git", "rev-parse", "FETCH_HEAD"], cwd=tree, capture=True)
    run(["git", "tag", "-f", BASE, commit], cwd=tree, capture=True)
    run(["git", "config", "winzapp.version", label], cwd=tree)
    apply_patches(tree, name)
    return commit


def prepare_whatsmeow(info, force):
    tree = tree_path("whatsmeow")
    if not force:
        ensure_clean("whatsmeow")
    found = json.loads(run(["go", "mod", "download", "-json",
                            f"{info['module']}@{info['version']}"], capture=True))
    fresh_repo(tree)
    shutil.copytree(found["Dir"], tree, dirs_exist_ok=True)
    for root, dirs, files in os.walk(tree):
        for entry in dirs + files:
            os.chmod(os.path.join(root, entry), stat.S_IWRITE | stat.S_IREAD)
    git(tree, "add", "-A")
    git(tree, "commit", "-q", "-m", f"{info['module']} {info['version']} ({found['Sum']})")
    run(["git", "tag", "-f", BASE], cwd=tree, capture=True)
    apply_patches(tree, "whatsmeow")


def check_go_mod(info):
    go_mod = open(os.path.join(tree_path("neonize"), "goneonize", "go.mod"), encoding="utf-8").read()
    wanted = upstream.required_version(go_mod, info["whatsmeow"]["module"])
    if wanted != info["whatsmeow"]["version"]:
        sys.exit(f"goneonize/go.mod requires whatsmeow {wanted}, the manifest pins "
                 f"{info['whatsmeow']['version']}: update one of them.")
    for module, path in ((info["whatsmeow"]["module"], "../../whatsmeow"),
                         ("github.com/purpshell/meowcaller", "../../meowcaller")):
        if upstream.replaced_by(go_mod, module) != path:
            sys.exit(f"goneonize/go.mod must replace {module} with {path}.")


def prepare(info, ref=None, force=False):
    neonize = upstream.current(info)
    label = ref or neonize["version"]
    commit = prepare_git("neonize", info["neonize"]["repository"], ref or neonize["commit"],
                         label, force)
    if ref is None and commit != neonize["commit"]:
        sys.exit(f"neonize {neonize['version']} resolved to {commit}, not the approved commit.")
    prepare_whatsmeow(info["whatsmeow"], force)
    prepare_git("meowcaller", info["meowcaller"]["repository"], info["meowcaller"]["commit"],
                info["meowcaller"]["commit"][:12], force)
    check_go_mod(info)
    say(f"workspace ready: neonize {label} ({commit[:12]})")
    return commit


def workspace_ready():
    return all(os.path.isdir(os.path.join(tree_path(name), ".git")) for name in TREES)


# ------------------------------------------------------------------ export
def export():
    moved = moved_trees()
    if moved:
        say(f"neonize_patches/ changed after the workspace was prepared ({', '.join(moved)}): "
            "exporting would put the older patches back. Run `prepare --force` to take the "
            "repository's patches (copy your own commits from the workspace first).")
        sys.exit(PATCHES_MOVED)
    for name in TREES:
        target = os.path.join(PATCHES, name)
        for old in glob.glob(os.path.join(target, "*.patch")):
            os.remove(old)
        format_patches(name, target)
        say(f"{name}: {len(glob.glob(os.path.join(target, '*.patch')))} patch(es) exported")


# ------------------------------------------------------------------ tools
def _stamp_ok(folder, value):
    stamp = os.path.join(folder, ".version")
    return os.path.isfile(stamp) and open(stamp, encoding="utf-8").read() == value


def _stamp(folder, value):
    with open(os.path.join(folder, ".version"), "w", encoding="utf-8") as f:
        f.write(value)


def ensure_tools(info):
    """protoc (checked against its SHA-256), protoc-gen-go and mypy-protobuf, at the
    versions upstream generates with. Returns the PATH entries they need."""
    tool = info["toolchain"]
    protoc_dir = os.path.join(TOOLS, "protoc")
    if not _stamp_ok(protoc_dir, tool["protoc"]["sha256"]):
        url = (f"https://github.com/protocolbuffers/protobuf/releases/download/"
               f"v{tool['protoc']['version']}/{tool['protoc']['asset']}")
        say(f"downloading {url}")
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read(64 * 1024 * 1024)
        if upstream.sha256_hex(data) != tool["protoc"]["sha256"]:
            sys.exit("protoc download does not match the manifest's SHA-256.")
        shutil.rmtree(protoc_dir, ignore_errors=True)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            archive.extractall(protoc_dir)
        _stamp(protoc_dir, tool["protoc"]["sha256"])
    gobin = os.path.join(TOOLS, "gobin")
    if not _stamp_ok(gobin, tool["protoc_gen_go"]):
        os.makedirs(gobin, exist_ok=True)
        run(["go", "install", f"google.golang.org/protobuf/cmd/protoc-gen-go@{tool['protoc_gen_go']}"],
            env={**os.environ, "GOBIN": gobin})
        _stamp(gobin, tool["protoc_gen_go"])
    mypy = os.path.join(TOOLS, "mypy")
    if not _stamp_ok(mypy, tool["mypy_protobuf"]):
        shutil.rmtree(mypy, ignore_errors=True)
        run([sys.executable, "-m", "venv", mypy])
        run([os.path.join(mypy, "Scripts", "python.exe"), "-m", "pip", "install", "-q",
             f"mypy-protobuf=={tool['mypy_protobuf']}"])
        _stamp(mypy, tool["mypy_protobuf"])
    return [os.path.join(protoc_dir, "bin"), gobin, os.path.join(mypy, "Scripts")]


def generate(info):
    # the plugins are found through PATH (protoc looks them up), but Windows
    # resolves the program itself on this process's PATH: protoc goes by full path
    tool_dirs = ensure_tools(info)
    env = {**os.environ, "PATH": os.pathsep.join(tool_dirs + [os.environ.get("PATH", "")])}
    protoc = os.path.join(tool_dirs[0], "protoc.exe")
    defproto = os.path.join(tree_path("neonize"), "goneonize", "defproto")
    run([protoc, "--go_out=.", "--go_opt=paths=source_relative", "Neonize.proto"], cwd=defproto, env=env)
    run([protoc, "--python_out=../../neonize/proto", "--mypy_out=../../neonize/proto",
         "Neonize.proto"], cwd=defproto, env=env, capture=True)
    say("protobuf code generated from Neonize.proto")


# ------------------------------------------------------------------ build
def check_compilers():
    if not shutil.which("go"):
        sys.exit("Go is not on the PATH (winget install GoLang.Go).")
    compiler = os.environ.get("CC", "gcc")
    if not shutil.which(compiler):
        sys.exit(f"{compiler} is not on the PATH (winget install BrechtSanders.WinLibs.POSIX.UCRT).")
    machine = run([compiler, "-dumpmachine"], capture=True)
    if not machine.startswith("x86_64"):
        sys.exit(f"{compiler} builds for {machine}; the DLL needs a 64-bit (x86_64) MinGW gcc.")


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def patches_sha256(name):
    """One hash of the patch files a tree was built from (names and contents)."""
    digest = hashlib.sha256()
    for file_name, content in _patch_files(os.path.join(PATCHES, name)).items():
        digest.update(file_name.encode() + b"\0" + content)
    return digest.hexdigest()


def build(info):
    check_compilers()
    tree = tree_path("neonize")
    package = os.path.join(DIST, "neonize")
    shutil.rmtree(DIST, ignore_errors=True)
    shutil.copytree(os.path.join(tree, "neonize"), package,
                    ignore=shutil.ignore_patterns("__pycache__", "*.dll", "*.so", "*.dylib"))
    env = {**os.environ, "CGO_ENABLED": "1", "GOFLAGS": "-mod=readonly", "GOTOOLCHAIN": "local"}
    say("compiling the DLL (go build -buildmode=c-shared)")
    run(["go", "build", "-buildmode=c-shared", "-trimpath", "-ldflags=-s",
         "-o", os.path.join(package, upstream.DLL_NAME), "."],
        cwd=os.path.join(tree, "goneonize"), env=env)
    for header in glob.glob(os.path.join(package, "*.h")):
        os.remove(header)
    version = git(tree, "config", "winzapp.version", capture=True)
    sums = []
    for root, _dirs, files in os.walk(package):
        for name in sorted(files):
            full = os.path.join(root, name)
            sums.append(f"{file_sha256(full)} *{os.path.relpath(full, DIST).replace(os.sep, '/')}")
    with open(os.path.join(DIST, "SHA256SUMS.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(sorted(sums, key=lambda line: line.split("*", 1)[1])) + "\n")
    asset = os.path.join(DIST, upstream.asset_name(version))
    with zipfile.ZipFile(asset, "w", zipfile.ZIP_DEFLATED) as archive:
        for root, _dirs, files in os.walk(package):
            for name in sorted(files):
                full = os.path.join(root, name)
                archive.write(full, os.path.relpath(full, DIST).replace(os.sep, "/"))
    commit = git(tree, "rev-parse", BASE, capture=True)
    # the identity names committed patches only: a workspace with work not
    # exported yet builds fine, but is never taken for the checkout's build
    exported = all(not _uncommitted(tree_path(name)) and matches_patches(name) for name in TREES)
    identity = (upstream.build_identity(upstream.pins(info, commit), upstream.read_patches(PATCHES))
                if exported else "")
    if not exported:
        say("the workspace differs from neonize_patches/: this build has no identity "
            "(run `export` to make it the checkout's)")
    details = {"identity": identity, "neonize": version, "neonize_commit": commit,
               "whatsmeow": info["whatsmeow"]["version"], "meowcaller": info["meowcaller"]["commit"],
               "patches": {name: patches_sha256(name) for name in TREES},
               "asset": os.path.basename(asset), "sha256": file_sha256(asset)}
    with open(os.path.join(DIST, "BUILD_INFO.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(details, f, indent=2)
    say(f"built {details['asset']} (sha256 {details['sha256']})")
    return details


# ------------------------------------------------------------------ install
def install(venv):
    python = os.path.join(venv, "Scripts", "python.exe")
    if not os.path.isfile(python):
        sys.exit(f"No virtualenv at {venv} (run prepare.py first).")
    source = os.path.join(DIST, "neonize")
    if not os.path.isfile(os.path.join(source, upstream.DLL_NAME)):
        sys.exit("Nothing built yet: run `build` first.")
    site = run([python, "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"], capture=True)
    target = os.path.join(site, "neonize")
    dll = os.path.join(target, upstream.DLL_NAME)
    if os.path.isfile(dll):
        # A loaded DLL can be renamed on Windows but not opened for writing:
        # check before deleting anything, or a running bridge leaves half a package.
        try:
            with open(dll, "r+b"):
                pass
        except OSError:
            sys.exit("The neonize DLL is in use: close WinZapp (it stops the bridge) and try again.")
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target)
    for stale in upstream.stale_metadata(os.listdir(site)):
        shutil.rmtree(os.path.join(site, stale), ignore_errors=True)
    with open(os.path.join(DIST, "BUILD_INFO.json"), encoding="utf-8") as f:
        identity = json.load(f).get("identity", "")
    marker = os.path.join(venv, upstream.MARKER)
    if identity:
        with open(marker, "w", encoding="utf-8") as f:
            f.write(identity)
    elif os.path.exists(marker):
        os.remove(marker)       # whatever it named is no longer what is installed
    say(f"installed into {target}")


# ------------------------------------------------------------------ main
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("all", "prepare", "export", "generate", "build",
                                            "install", "check", "approve"))
    parser.add_argument("--ref", help="neonize tag, branch or commit instead of the approved one")
    parser.add_argument("--version", help="X.Y.Z being approved (approve)")
    parser.add_argument("--force", action="store_true", help="discard uncommitted workspace changes")
    parser.add_argument("--install", action="store_true", help="all: install after building")
    parser.add_argument("--venv", default=os.path.join(HERE, ".venv"), help="install: target virtualenv")
    args = parser.parse_args(argv)
    info = manifest()
    os.makedirs(WORK, exist_ok=True)
    if args.command == "prepare":
        prepare(info, args.ref, args.force)
    elif args.command == "export":
        export()
    elif args.command == "generate":
        generate(info)
    elif args.command == "build":
        build(info)
    elif args.command == "install":
        install(args.venv)
    elif args.command in ("all", "check", "approve"):
        if args.command == "approve" and not (args.ref and args.version):
            sys.exit("approve needs --ref and --version.")
        moved = moved_trees() if args.command == "all" and workspace_ready() else []
        if moved and not args.force:
            # building it would build the code from before the pull
            say(f"neonize_patches/ changed after the workspace was prepared ({', '.join(moved)}): "
                "preparing it again from the repository's patches")
            back_up_workspace()
            commit = prepare(info, args.ref, True)
        elif args.command == "all" and workspace_ready() and not args.force:
            # keeps the workspace (and any commit not exported yet) as it is
            commit = git(tree_path("neonize"), "rev-parse", BASE, capture=True)
            say("using the existing workspace (prepare rebuilds it from the patches)")
        else:
            commit = prepare(info, args.ref, args.force)
        generate(info)
        build(info)
        if args.command == "approve":
            today = datetime.date.today().isoformat()
            updated = upstream.with_approval(info, args.version, commit, today)
            with open(os.path.join(HERE, upstream.MANIFEST), "w", encoding="utf-8", newline="\n") as f:
                json.dump(updated, f, indent=2)
                f.write("\n")
            say(f"neonize {args.version} ({commit[:12]}) approved in {upstream.MANIFEST}")
        if args.command == "all" and args.install:
            install(args.venv)


if __name__ == "__main__":
    main()
