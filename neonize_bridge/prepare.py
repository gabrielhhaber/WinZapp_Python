"""Prepares the Neonize bridge to run: a virtualenv, its requirements and
WinZapp's build of neonize.

    python neonize_bridge/prepare.py                # the published build, or source
    python neonize_bridge/prepare.py --from-source  # always compile it here

The bridge uses events and functions that official neonize does not have
yet: WinZapp builds neonize from the approved upstream release plus
`neonize_patches/` (build_neonize.py, docs/reference/neonize-build.md). When
`neonize_upstream.json` names a `release` (published by the "Neonize DLL" CI
workflow), this script downloads that package, checks its SHA-256 and what is
inside it, and puts it in place of the PyPI neonize; without one, or with
--from-source, it runs build_neonize.py (Go and a C compiler needed: a 64-bit
MinGW gcc on Windows, the Xcode command line tools on macOS).

Windows x64 and macOS (Apple Silicon and Intel). The published build is the
Windows DLL, so macOS always builds from source. Standard library only, so it
runs before anything is installed. Run it again to repair the install; close WinZapp first (the
bridge keeps the DLL open).
"""

import hashlib
import io
import os
import posixpath
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile

import neonize_platform
import neonize_upstream

#: Where the "Neonize DLL" workflow publishes the built package.
RELEASE_REPOSITORY = "gabrielhhaber/WinZapp_Python"

# The build replaces the neonize package and nothing else. The
# release's hash proves the archive is the one CI built; these limits
# make sure that even a wrong archive cannot drop a `.pth` file or a
# `sitecustomize.py` (both run on every start of the bridge's Python) or
# anything outside `neonize/` into site-packages.
PACKAGE = "neonize"
ALLOWED_EXTENSIONS = (".py", ".pyi", ".pyc", ".dll", ".typed")
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MAX_UNPACKED_BYTES = 400 * 1024 * 1024
MAX_MEMBERS = 2000
ALLOWED_HOST_SUFFIXES = ("github.com", "githubusercontent.com")

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, ".venv")
#: the identity of the neonize installed in the virtualenv (neonize_upstream.py),
#: so build.py can tell it is the build this checkout calls for
MARKER = os.path.join(VENV, neonize_upstream.MARKER)
#: None on a system the bridge is not built for (main() says so)
try:
    TARGET = neonize_platform.target()
except ValueError:
    TARGET = None
PYTHON = neonize_platform.venv_python(VENV, TARGET) if TARGET else ""


def run(*cmd):
    subprocess.run(cmd, check=True)


def runtime_lock(target) -> str:
    """The hashed lock of the bridge's requirements for this system: neonize
    needs python-magic-bin on Windows and python-magic (Homebrew's libmagic)
    elsewhere, and the wheels differ."""
    name = "requirements.lock" if target.system == "windows" else "requirements-macos.lock"
    return os.path.join(HERE, name)


def site_packages() -> str:
    out = subprocess.run(
        [PYTHON, "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"],
        check=True, capture_output=True, text=True)
    return out.stdout.strip()


def member_problem(info) -> str:
    """Why this archive member may not be extracted, or "" when it may."""
    name = info.filename
    if "\\" in name or name.startswith("/") or ":" in name:
        return f"{name}: not a plain relative path"
    parts = name.split("/")
    if ".." in parts or "" in parts[:-1]:
        return f"{name}: leaves its folder"
    if parts[0] != PACKAGE:
        return f"{name}: outside {PACKAGE}/"
    if (info.external_attr >> 16) & 0o170000 == 0o120000:
        return f"{name}: a symbolic link"
    if info.is_dir():
        return ""
    if not name.lower().endswith(ALLOWED_EXTENSIONS):
        return f"{name}: a file type the neonize package does not have"
    return ""


def check_members(infos) -> list:
    """Every reason the archive must be refused (empty list: it is fine)."""
    infos = list(infos)
    problems = [p for p in map(member_problem, infos) if p]
    if len(infos) > MAX_MEMBERS:
        problems.append(f"{len(infos)} members (more than {MAX_MEMBERS})")
    if sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
        problems.append("unpacked size is too large")
    return problems


def extract_checked(archive, target):
    """Extract an archive that passed check_members(), one member at a time,
    each into a path that is proven to stay inside `target`."""
    problems = check_members(archive.infolist())
    if problems:
        sys.exit("The neonize build is not a plain neonize package:\n  "
                 + "\n  ".join(problems[:10]))
    root = os.path.realpath(target)
    written = 0
    for info in archive.infolist():
        destination = os.path.realpath(
            os.path.join(root, *posixpath.normpath(info.filename).split("/")))
        if os.path.commonpath([root, destination]) != root:
            sys.exit(f"Refusing to extract {info.filename} outside {target}.")
        if info.is_dir():
            os.makedirs(destination, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with archive.open(info) as source, open(destination, "wb") as out:
            # The sizes in the archive can lie: count what is really written.
            while chunk := source.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UNPACKED_BYTES:
                    sys.exit("The neonize build unpacks to more than expected.")
                out.write(chunk)


def host_allowed(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return urllib.parse.urlparse(url).scheme == "https" and any(
        host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_HOST_SUFFIXES)


def usable_release(target=None):
    """The `release` of neonize_upstream.json when it was built from this
    checkout's pins and patches (its identity) for this system, else None:
    build from source. The published build is the Windows DLL."""
    if (target or TARGET).system != "windows":
        return None
    release = neonize_upstream.load(os.path.join(HERE, neonize_upstream.MANIFEST)).get("release")
    if release is None:
        return None
    if release["identity"] != neonize_upstream.checkout_identity(HERE):
        print("The published neonize build is from older patches; building from source.")
        return None
    return release


def download_build(release) -> bytes:
    url = (f"https://github.com/{RELEASE_REPOSITORY}/releases/download/"
           f"{release['tag']}/{release['asset']}")
    print(f"Downloading {url}...")
    with urllib.request.urlopen(url, timeout=120) as response:
        if not host_allowed(response.geturl()):
            sys.exit(f"The download was redirected to an unexpected address ({response.geturl()}).")
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        sys.exit("The download is larger than expected.")
    digest = hashlib.sha256(data).hexdigest()
    if digest != release["sha256"]:
        sys.exit(f"The downloaded build does not match the expected SHA-256 ({digest}).")
    return data


def main():
    if TARGET is None or TARGET.system not in ("windows", "darwin"):
        sys.exit("The Neonize bridge is built for Windows x64 and macOS only.")
    if not os.path.isfile(PYTHON):
        print("Creating the virtualenv...")
        run(sys.executable, "-m", "venv", VENV)
    print("Installing requirements...")
    # The lock pins every package, direct or not, to the hashes that were reviewed.
    run(PYTHON, "-m", "pip", "install", "--require-hashes", "-r", runtime_lock(TARGET))
    release = None if "--from-source" in sys.argv[1:] else usable_release()
    if release is None:
        print("Building neonize from source (build_neonize.py)...")
        run(PYTHON, os.path.join(HERE, "build_neonize.py"), "all", "--install", "--venv", VENV)
    else:
        data = download_build(release)
        target = site_packages()
        package = os.path.join(target, PACKAGE)
        dll = os.path.join(package, neonize_platform.library_name(TARGET))
        if os.path.isfile(dll):
            try:                    # a running bridge keeps the DLL open
                with open(dll, "r+b"):
                    pass
            except OSError:
                sys.exit("The neonize DLL is in use: close WinZapp and run this again.")
        shutil.rmtree(package, ignore_errors=True)   # the whole package, not a mix of two
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            extract_checked(archive, target)
        for stale in neonize_upstream.stale_metadata(os.listdir(target)):
            shutil.rmtree(os.path.join(target, stale), ignore_errors=True)
        with open(MARKER, "w", encoding="utf-8") as stream:
            stream.write(release["identity"])
        print(f"WinZapp's neonize build ({release['tag']}) installed in {target}.")
    print("Done. WinZapp starts the bridge by itself when the Neonize engine is chosen "
          "(Settings, Connection).")
    print("Video thumbnails and stickers use the ffmpeg in client/lib (lib/ in a compiled "
          "build); other folders go in neonize_bridge/path_extra.txt (one folder per line).")


if __name__ == "__main__":
    main()
