#!/usr/bin/env python3
"""Build a self-contained WinZapp.app for macOS (Apple Silicon or Intel —
whichever Mac runs the build).

    python3 macos/build_app.py [--no-deps] [--no-api] [--zip] [--install | --install-only]

Steps
  1. Python deps into .pydeps/ (requirements.txt minus Windows-only pins).
  2. Node.js (the version client/node_download_config.py pins) and ffmpeg
     (pinned below), both SHA-256 checked.
  3. setup_api.py with that Node -> client/api (WPPConnect server + its
     headless Chrome).
  4. PyInstaller -> macos/dist/WinZapp.app (runtime hook installs winzapp_mac).
  5. The server runtime (api/ + node/) into Contents/Resources/runtime with a
     STAMP, without the server's development packages (runtime_prune.py);
     the app installs it into Application Support at launch
     (winzapp_mac/paths_mac.py).
  6. Sparkle.framework (pinned below, SHA-256 checked) into Contents/Frameworks;
     Info.plist keys (mic/camera prompts; the Sparkle feed and key when
     WINZAPP_SPARKLE_PUBLIC_KEY is set), ad-hoc signature.
  7. --zip: macos/dist/WinZapp-macOS-<arch>.zip for a release, and next to it
     WinZapp-macOS-provenance-<arch>.json when HEAD is the commit our
     repository's release tag points at (winzapp_mac/provenance.py; the
     tag and commit are also written to Info.plist as WinZappReleaseTag and
     WinZappSourceCommit).
     --install: the app into /Applications (only while WinZapp is not running).

Requires Homebrew's portaudio (brew install portaudio) for PyAudio.
Every step fails loudly; nothing is installed unless all of them passed.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
# Also when loaded as a module by path (appcast.py's sign_update_tool(), the
# stamping scripts): sys.path then lacks this script's directory.
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import runtime_prune     # noqa: E402  (macos/, beside this script)
ROOT = os.path.dirname(HERE)
CLIENT = os.path.join(ROOT, "client")
PYDEPS = os.path.join(ROOT, ".pydeps")
BUILD = os.path.join(HERE, "build")
DIST = os.path.join(HERE, "dist")
APP = os.path.join(DIST, "WinZapp.app")
NODE_CACHE = os.path.join(HERE, "cache")
ARCH = platform.machine()                       # "arm64" or "x86_64"
NODE_ARCH = {"arm64": "arm64", "x86_64": "x64"}[ARCH]
# ffmpeg: static macOS builds from ffmpeg.martin-riedl.de (system frameworks
# only, libopus included). Pinned per architecture with the published SHA-256.
FFMPEG = {
    "arm64": ("https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2/ffmpeg.zip",
              "c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924"),
    "x86_64": ("https://ffmpeg.martin-riedl.de/download/macos/amd64/1789931006_9.0.2/ffmpeg.zip",
               "7c6b4125b191cbf773832dc51f424cf2b6bb7da43007d1e066f95909e47cacd4"),
}

# Sparkle 2 (winzapp_mac/sparkle_mac.py), the official release archive with
# the SHA-256 GitHub publishes for it. 2.10 needs macOS 12, as the bundled
# Chrome already does.
SPARKLE = ("2.10.0",
           "https://github.com/sparkle-project/Sparkle/releases/download/2.10.0/Sparkle-2.10.0.tar.xz",
           "c2bf58aa8387266ac179357b1415d6f2635f044da8be41042af32425dae6da0c")
SPARKLE_DIR = os.path.join(NODE_CACHE, f"sparkle-{SPARKLE[0]}")
FRAMEWORKS = os.path.join(APP, "Contents", "Frameworks")

WINDOWS_ONLY = re.compile(r"^(pywin32|pywin32-ctypes|comtypes|Windows-Toasts|winrt-|pefile)", re.I)
MAC_EXTRA = ["pyobjc-framework-Cocoa", "pyobjc-framework-ApplicationServices",
             "pyobjc-framework-UserNotifications", "pyobjc-framework-Intents",
             # update_coord's process liveness off Windows (real creation times,
             # executable names); without it every lease records 0.0 and a
             # leaked one stays "alive" once its pid is reused (coord_mac.py).
             "psutil==7.2.2"]
BUNDLE_ID = os.environ.get("WINZAPP_BUNDLE_ID", "com.winzapp.macos")


def load_sparkle_policy():
    """winzapp_mac/sparkle_policy.py: pure standard library like provenance.py
    (winzapp_mac/__init__.py imports nothing until install() runs)."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from winzapp_mac import sparkle_policy
    return sparkle_policy


def load_provenance():
    """winzapp_mac/provenance.py by path: it is pure standard library, and
    importing the winzapp_mac package would pull in PyObjC."""
    spec = importlib.util.spec_from_file_location(
        "winzapp_provenance", os.path.join(HERE, "winzapp_mac", "provenance.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def release_source():
    """(tag, commit) tying this build to our repository, or None for a
    development build. A tag that does not hold stops the build; a release
    build (one that names a releases repository) without a tag too, because
    no updater would accept it."""
    prov = load_provenance()

    def git(*args):
        return subprocess.run(["git", "-C", ROOT, *args], check=True, capture_output=True,
                              text=True).stdout.strip()
    try:
        source = prov.resolve_build_source(git, prov.fetch_official,
                                           os.environ.get("WINZAPP_RELEASE_TAG", ""))
    except prov.ProvenanceError as exc:
        sys.exit(f"Commit provenance: {exc}")
    if source is None and os.environ.get("WINZAPP_MAC_RELEASES_REPO"):
        sys.exit("Commit provenance: a release build must be built from a release tag of "
                 f"{prov.OFFICIAL_REPO} (check it out, or set WINZAPP_RELEASE_TAG).")
    if source is None:
        print("   (HEAD is not a release tag: no commit provenance; this build will not self-update)")
    return source


def write_provenance(source, zip_path):
    """WinZapp-macOS-provenance-<arch>.json next to the zip; none (and no
    stale one) without a source."""
    prov = load_provenance()
    out = os.path.join(DIST, prov.provenance_name(ARCH))
    if os.path.exists(out):
        os.remove(out)
    if source is None:
        return None
    tag, commit = source
    with open(zip_path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    with open(out, "wb") as fh:
        fh.write(prov.build_provenance(tag, commit, {os.path.basename(zip_path): digest}))
    print(f"   provenance {os.path.relpath(out, ROOT)} ({tag} -> {commit[:12]})")
    return out


def step(msg):
    print(f"\n==> {msg}", flush=True)


def run(cmd, **kw):
    print("   $", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


APPLE_ATTEMPTS = 3
APPLE_RETRY_WAIT = 30


def run_apple(cmd, quiet=False, attempts=APPLE_ATTEMPTS, wait=APPLE_RETRY_WAIT):
    """Run a command that talks to Apple's servers (codesign --timestamp,
    notarytool) and return its CompletedProcess.

    Those servers time out now and then, so a failed attempt is retried after
    `wait` seconds. Every failed attempt prints the command's own output, so a
    failure in CI says what Apple answered instead of only an exit status.
    quiet=True keeps a successful attempt's output out of the log (codesign
    runs once per binary)."""
    for attempt in range(1, attempts + 1):
        if not quiet:
            print("   $", " ".join(str(c) for c in cmd), flush=True)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            if not quiet:
                _print_output(r)
            return r
        print(f"   ! attempt {attempt}/{attempts} failed (exit {r.returncode}):",
              " ".join(str(c) for c in cmd), flush=True)
        _print_output(r)
        if attempt < attempts:
            time.sleep(wait)
    raise subprocess.CalledProcessError(r.returncode, cmd, r.stdout, r.stderr)


def _print_output(r):
    for text in (r.stdout, r.stderr):
        if text and text.strip():
            print(text.rstrip(), flush=True)


def notary_status(stdout):
    """(id, status) from `notarytool submit --output-format json`, or
    (None, None) when Apple never answered (the submission did not happen)."""
    for line in reversed((stdout or "").strip().splitlines()):
        try:
            info = json.loads(line)
        except ValueError:
            continue
        if isinstance(info, dict) and info.get("id"):
            return info["id"], info.get("status")
    return None, None


def download(url, dest=None):
    """urllib with a named User-Agent (some mirrors refuse Python's)."""
    req = urllib.request.Request(url, headers={"User-Agent": "WinZapp-macOS-build/1.0"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        if dest is None:
            return resp.read()
        with open(dest, "wb") as fh:
            shutil.copyfileobj(resp, fh)
    return dest


def env_with_pydeps():
    env = dict(os.environ)
    env["PYTHONPATH"] = PYDEPS + os.pathsep + env.get("PYTHONPATH", "")
    return env


# 1 ------------------------------------------------------------------------
def python_deps():
    step("Python dependencies -> .pydeps/")
    req = os.path.join(BUILD, "requirements-mac.txt")
    os.makedirs(BUILD, exist_ok=True)
    with open(os.path.join(ROOT, "requirements.txt")) as fh, open(req, "w") as out:
        for line in fh:
            s = line.strip()
            if s and not s.startswith("#") and not WINDOWS_ONLY.match(s):
                out.write(line)
        out.write("\n".join(MAC_EXTRA) + "\n")
    brew = subprocess.run(["brew", "--prefix", "portaudio"], capture_output=True,
                          text=True).stdout.strip()
    if not brew:
        sys.exit("PortAudio is needed for PyAudio: brew install portaudio")
    env = dict(os.environ, CFLAGS=f"-I{brew}/include", LDFLAGS=f"-L{brew}/lib -lportaudio")
    run([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "--target", PYDEPS,
         "-r", req], env=env)
    # PyAudio must link Homebrew's PortAudio; a cached wheel built without it
    # imports fine in pip's eyes and fails at runtime.
    probe = subprocess.run([sys.executable, "-s", "-S", "-c", "import pyaudio._portaudio"],
                           env=env_with_pydeps(), capture_output=True)
    if probe.returncode != 0:
        run([sys.executable, "-m", "pip", "install", "--quiet", "--no-cache-dir", "--force-reinstall",
             "--no-deps", "--upgrade", "--no-binary", "PyAudio", "--target", PYDEPS, "PyAudio"], env=env)
    check_psutil()
    # sound_lib ships Intel-only/old macOS BASS builds; use the universal ones.
    sl = os.path.join(PYDEPS, "sound_lib", "lib", "x64")
    for f in os.listdir(os.path.join(HERE, "lib")):
        if f.endswith(".dylib"):
            shutil.copy2(os.path.join(HERE, "lib", f), os.path.join(sl, f))


def check_psutil():
    """psutil must import and measure a creation time with the build's own
    Python, from a native module built for this Mac: without it every
    account lease records 0.0 and leaked ones read as live accounts
    (winzapp_mac/coord_mac.py). Stops the build otherwise."""
    probe = subprocess.run(
        [sys.executable, "-s", "-S", "-c",
         "import psutil, psutil._psutil_osx as m; assert psutil.Process().create_time() > 0; print(m.__file__)"],
        env=env_with_pydeps(), capture_output=True, text=True)
    if probe.returncode != 0:
        sys.exit("psutil does not work in .pydeps: " + (probe.stderr or "").strip()[-300:])
    native = probe.stdout.strip().splitlines()[-1]
    archs = subprocess.run(["lipo", "-archs", native], capture_output=True, text=True).stdout.split()
    if ARCH not in archs:
        sys.exit(f"psutil's native module is {archs or 'unreadable'}, not {ARCH}: {native}")
    print(f"   psutil OK ({ARCH})")


# 2 ------------------------------------------------------------------------
def node_version():
    with open(os.path.join(CLIENT, "node_download_config.py")) as fh:
        return re.search(r'NODE_VERSION\s*=\s*"([^"]+)"', fh.read()).group(1)


def node_runtime():
    ver = node_version()
    step(f"Node.js v{ver} (darwin-{NODE_ARCH})")
    name = f"node-v{ver}-darwin-{NODE_ARCH}"
    dest = os.path.join(NODE_CACHE, name)
    if not os.path.isfile(os.path.join(dest, "bin", "node")):
        os.makedirs(NODE_CACHE, exist_ok=True)
        base = f"https://nodejs.org/dist/v{ver}/"
        tgz = os.path.join(NODE_CACHE, name + ".tar.gz")
        download(base + name + ".tar.gz", tgz)
        sums = download(base + "SHASUMS256.txt").decode()
        want = next(l.split()[0] for l in sums.splitlines() if l.endswith(name + ".tar.gz"))
        got = hashlib.sha256(open(tgz, "rb").read()).hexdigest()
        if got != want:
            sys.exit(f"Node checksum mismatch: {got} != {want}")
        with tarfile.open(tgz) as tf:
            tf.extractall(NODE_CACHE, filter="tar")
        os.remove(tgz)
    return dest


def fetch_ffmpeg():
    dest = os.path.join(HERE, "lib", "ffmpeg")
    url, want = FFMPEG[ARCH]
    stamp = dest + ".sha256"
    if os.path.isfile(dest) and os.path.isfile(stamp) and open(stamp).read().strip() == want:
        return
    step(f"ffmpeg ({ARCH})")
    os.makedirs(NODE_CACHE, exist_ok=True)
    zpath = os.path.join(NODE_CACHE, "ffmpeg.zip")
    download(url, zpath)
    got = hashlib.sha256(open(zpath, "rb").read()).hexdigest()
    if got != want:
        sys.exit(f"ffmpeg checksum mismatch: {got} != {want}")
    import zipfile
    with zipfile.ZipFile(zpath) as zf:
        with zf.open("ffmpeg") as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
    os.chmod(dest, 0o755)
    os.remove(zpath)
    with open(stamp, "w") as fh:
        fh.write(want)


def fetch_sparkle():
    """The pinned Sparkle release, unpacked once into macos/cache (also what
    macos/release/appcast.py takes sign_update from). Returns its folder."""
    version, url, want = SPARKLE
    stamp = os.path.join(SPARKLE_DIR, ".sha256")
    if os.path.isfile(stamp) and open(stamp).read().strip() == want:
        return SPARKLE_DIR
    step(f"Sparkle {version}")
    if os.path.exists(SPARKLE_DIR):
        shutil.rmtree(SPARKLE_DIR)
    os.makedirs(NODE_CACHE, exist_ok=True)
    archive = os.path.join(NODE_CACHE, f"Sparkle-{version}.tar.xz")
    download(url, archive)
    got = hashlib.sha256(open(archive, "rb").read()).hexdigest()
    if got != want:
        os.remove(archive)
        sys.exit(f"Sparkle checksum mismatch: {got} != {want}")
    # "data": the framework's own relative symlinks stay, nothing may point
    # outside the folder.
    with tarfile.open(archive) as tf:
        tf.extractall(SPARKLE_DIR, filter="data")
    os.remove(archive)
    with open(stamp, "w") as fh:
        fh.write(want)
    return SPARKLE_DIR


def embed_sparkle(sparkle_dir):
    """Sparkle.framework into Contents/Frameworks, without its XPC services:
    they exist for sandboxed apps (WinZapp is not; entitlements/app.plist),
    are off unless Info.plist enables them, and Sparkle's documentation offers
    removing them ("Removing XPC Services",
    sparkle-project.org/documentation/sandboxing). Fewer helpers to sign and
    notarize."""
    step("Sparkle.framework -> Contents/Frameworks")
    dest = os.path.join(FRAMEWORKS, "Sparkle.framework")
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(FRAMEWORKS, exist_ok=True)
    run(["ditto", os.path.join(sparkle_dir, "Sparkle.framework"), dest])
    for rel in ("XPCServices", os.path.join("Versions", "B", "XPCServices")):
        path = os.path.join(dest, rel)
        if os.path.islink(path):
            os.remove(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
    return dest


def sparkle_plist(source, arch=ARCH):
    """The Info.plist keys that turn Sparkle on (sparkle_policy.plist_keys),
    from WINZAPP_SPARKLE_PUBLIC_KEY and WINZAPP_SPARKLE_FEED_BASE; {} when no
    key is set, so the build updates as before (updater_mac). A key on a
    build without a release tag stops the build: Sparkle could not tell a
    downgrade from an update, any more than updater_mac can."""
    key = os.environ.get("WINZAPP_SPARKLE_PUBLIC_KEY", "").strip()
    if not key:
        return {}
    if source is None:
        sys.exit("Sparkle: WINZAPP_SPARKLE_PUBLIC_KEY is set but this build has no release tag")
    with open(os.path.join(CLIENT, "languages", "language_map.json"), encoding="utf-8") as fh:
        locales = list(json.load(fh))
    try:
        return load_sparkle_policy().plist_keys(
            key, os.environ.get("WINZAPP_SPARKLE_FEED_BASE", "").strip(), arch, locales)
    except ValueError as exc:
        sys.exit(f"Sparkle: {exc} (WINZAPP_SPARKLE_PUBLIC_KEY / WINZAPP_SPARKLE_FEED_BASE)")


# 3 ------------------------------------------------------------------------
def build_api(node_dir):
    step("WPPConnect server (setup_api.py)")
    env = env_with_pydeps()
    env["PATH"] = os.path.join(node_dir, "bin") + os.pathsep + env["PATH"]
    run([sys.executable, os.path.join(ROOT, "setup_api.py")], env=env, cwd=ROOT)
    if not os.path.isdir(os.path.join(CLIENT, "api", "dist")):
        sys.exit("setup_api.py did not produce client/api/dist")


# 4 ------------------------------------------------------------------------
def make_icon():
    icns = os.path.join(BUILD, "WinZapp.icns")
    if os.path.isfile(icns):
        return icns
    step("App icon")
    iconset = os.path.join(BUILD, "WinZapp.iconset")
    os.makedirs(iconset, exist_ok=True)
    code = f"""
import sys; sys.path.insert(0, {PYDEPS!r})
from AppKit import (NSImage, NSBitmapImageRep, NSColor, NSBezierPath, NSFont,
    NSString, NSMakeRect, NSGraphicsContext, NSPNGFileType,
    NSFontAttributeName, NSForegroundColorAttributeName, NSDeviceRGBColorSpace)
for s in (16, 32, 64, 128, 256, 512, 1024):
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(None, s, s, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    NSColor.colorWithRed_green_blue_alpha_(0.15, 0.68, 0.38, 1).set()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(s*.06, s*.06, s*.88, s*.88), s*.2, s*.2).fill()
    attrs = {{NSFontAttributeName: NSFont.boldSystemFontOfSize_(s*.42),
             NSForegroundColorAttributeName: NSColor.whiteColor()}}
    t = NSString.stringWithString_("WZ"); sz = t.sizeWithAttributes_(attrs)
    t.drawAtPoint_withAttributes_(((s-sz.width)/2, (s-sz.height)/2), attrs)
    NSGraphicsContext.restoreGraphicsState()
    data = rep.representationUsingType_properties_(NSPNGFileType, {{}})
    for name, px in (("icon_%dx%d.png" % (s, s), s), ("icon_%dx%d@2x.png" % (s//2, s//2), s)):
        if name.startswith("icon_8x8") or name.startswith("icon_1024x1024.png"): continue
        data.writeToFile_atomically_({iconset!r} + "/" + name, True)
"""
    run([sys.executable, "-c", code])
    run(["iconutil", "-c", "icns", iconset, "-o", icns])
    return icns


def mac_libs():
    """macos/lib, with the obsolete i386 slice stripped from BASS's
    universal dylibs (PyInstaller cannot process i386)."""
    out = os.path.join(BUILD, "lib")
    os.makedirs(out, exist_ok=True)
    for f in os.listdir(os.path.join(HERE, "lib")):
        src, dst = os.path.join(HERE, "lib", f), os.path.join(out, f)
        archs = subprocess.run(["lipo", "-archs", src], capture_output=True, text=True).stdout.split()
        if "i386" in archs:
            run(["lipo", src, "-remove", "i386", "-output", dst])
        else:
            shutil.copy2(src, dst)
    return out


def pyinstaller():
    # Build the same MO-only resources as Windows before bundling any assets.
    language_resources = os.path.join(BUILD, "languages")
    run([sys.executable, "-m", "winzapp_tools.translations", "compile",
         "--mo-dir", language_resources], cwd=ROOT, env=env_with_pydeps())
    step("PyInstaller -> macos/dist/WinZapp.app")
    libdir = mac_libs()
    # sound_lib and accessible_output2 ship Windows/Linux/i386 binaries that
    # PyInstaller cannot process on arm64: take their code only, and the
    # universal dylibs from macos/lib (placed where sound_lib looks).
    submodules_only = ["sound_lib", "accessible_output2"]
    collect = ["platform_utils", "libloader", "wx",
               "cryptography", "requests", "socketio", "engineio", "pyperclip", "packaging",
               "pyaudio", "aiosqlite", "sounddevice", "livekit", "objc", "AppKit", "Foundation",
               "ApplicationServices", "UserNotifications", "Intents"]
    # -s -S: no user or system site-packages — only .pydeps (via PYTHONPATH)
    # and the standard library, so nothing else installed on this Mac leaks
    # into the bundle.
    cmd = [sys.executable, "-s", "-S", "-m", "PyInstaller", "--noconfirm", "--windowed", "--onedir",
           "--name", "WinZapp", "--osx-bundle-identifier", BUNDLE_ID,
           "--icon", make_icon(),
           "--distpath", DIST, "--workpath", os.path.join(BUILD, "pyinstaller"),
           "--specpath", BUILD,
           "--paths", CLIENT, "--paths", HERE,
           "--runtime-hook", os.path.join(HERE, "rthook_winzapp_mac.py"),
           "--additional-hooks-dir", os.path.join(HERE, "pyi_hooks")]
    for pkg in collect:
        cmd += ["--collect-all", pkg]
    for pkg in submodules_only:
        cmd += ["--collect-submodules", pkg]
    for mod in ("accounts", "coord_locks", "node_coord", "ipc", "update_coord", "app_settings",
                "account_migration", "account_bootstrap", "account_launcher", "account_ui",
                "session_store", "window_title"):
        cmd += ["--hidden-import", mod]
    cmd += ["--hidden-import", "winzapp_mac", "--hidden-import", "psutil"]
    for f in sorted(os.listdir(os.path.join(HERE, "winzapp_mac"))):
        if f.endswith(".py") and f != "__init__.py":
            cmd += ["--hidden-import", "winzapp_mac." + f[:-3]]
    datas = [("sounds", "sounds"), ("api_patches", "api_patches"),
             (os.path.join("data", "settings_default.json"), "data"),
             ("wpp_minimum_version.txt", ".")]
    datas += [(f, ".") for f in os.listdir(CLIENT) if f.startswith("changelog_")]
    for src, dst in datas:
        cmd += ["--add-data", f"{os.path.join(CLIENT, src)}:{dst}"]
    cmd += ["--add-data", f"{language_resources}:languages"]
    for f in os.listdir(libdir):
        cmd += ["--add-binary", f"{os.path.join(libdir, f)}:lib"]
        if f.endswith(".dylib"):
            cmd += ["--add-binary", f"{os.path.join(libdir, f)}:sound_lib/lib/x64"]
    cmd.append(os.path.join(CLIENT, "main.py"))
    if os.path.isdir(APP):
        shutil.rmtree(APP)
    run(cmd, env=env_with_pydeps(), cwd=CLIENT)


# 5 ------------------------------------------------------------------------
# api/ entries the server writes at run time, or that a runtime never needs
# (upstream's git hooks and editor settings included).
API_SKIP = {"log", "uploads", "WhatsAppImages", "wppconnect_tokens", "tokens", "userDataDir",
            "src", "docs", ".git", ".github", ".husky", ".vscode"}


def bundle_runtime(node_dir):
    """Contents/Resources/runtime/{api,node,STAMP}."""
    step("Server runtime -> Contents/Resources/runtime")
    rt = os.path.join(APP, "Contents", "Resources", "runtime")
    if os.path.exists(rt):
        shutil.rmtree(rt)
    os.makedirs(os.path.join(rt, "node"))
    shutil.copy2(os.path.join(node_dir, "bin", "node"), os.path.join(rt, "node", "node"))
    shutil.copytree(os.path.join(node_dir, "lib", "node_modules"),
                    os.path.join(rt, "node", "node_modules"), symlinks=True)
    api_src = os.path.join(CLIENT, "api")
    os.makedirs(os.path.join(rt, "api"))
    for name in os.listdir(api_src):
        if name in API_SKIP:
            continue
        src, dst = os.path.join(api_src, name), os.path.join(rt, "api", name)
        if os.path.isdir(src) and not os.path.islink(src):
            run(["ditto", src, dst])
        else:
            shutil.copy2(src, dst, follow_symlinks=False)
    # Development packages leave the copy only: client/api keeps them for the
    # next build (runtime_prune.py).
    removed, missing = runtime_prune.prune_dev_modules(os.path.join(rt, "api"),
                                                        os.path.join(node_dir, "bin", "node"))
    if missing:
        sys.exit(f"the pruned runtime cannot load: {', '.join(missing)}")
    print(f"   {removed} development packages left out")
    # Identifies this runtime: the app reinstalls it into Application Support
    # whenever the stamp differs (winzapp_mac/paths_mac.py).
    h = hashlib.sha256()
    h.update(node_version().encode())
    for rel in ("package-lock.json", "package.json", os.path.join("dist", "config.js")):
        path = os.path.join(api_src, rel)
        if os.path.isfile(path):
            h.update(open(path, "rb").read())
    head = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    h.update(head.encode())
    with open(os.path.join(rt, "STAMP"), "w") as fh:
        fh.write(f"{head[:12]}-{h.hexdigest()[:16]}\n")


# 6 ------------------------------------------------------------------------
def bundle_version(source):
    """CFBundleShortVersionString: the release tag's version for a release
    build, which is checked out clean from the tag, so client/version.py there
    is the unstamped placeholder (CI stamps it only at build); version.py for
    a development build. The "alpha"/"beta" suffix is dropped so the value
    stays numeric (Apple expects period-separated integers there; the four
    parts already shipped notarized), rather than risk notarization or
    Gatekeeper on a letter; the app itself still runs as the full tag
    (winzapp_mac/version_mac.py)."""
    if source:
        return re.sub(r"(alpha|beta)$", "", load_provenance().tag_version(source[0]))
    sys.path.insert(0, CLIENT)
    try:
        from version import __version__ as ver  # noqa
    except Exception:
        ver = "0"
    return str(ver)


def finish_bundle(source=None):
    step("Info.plist + signature")
    plist_path = os.path.join(APP, "Contents", "Info.plist")
    with open(plist_path, "rb") as fh:
        plist = plistlib.load(fh)
    plist.update({
        "CFBundleName": "WinZapp",
        "CFBundleDisplayName": "WinZapp",
        "CFBundleShortVersionString": bundle_version(source),
        "NSMicrophoneUsageDescription": "WinZapp records voice messages and voice calls.",
        "NSCameraUsageDescription": "WinZapp uses the camera for video calls.",
        "NSAudioCaptureUsageDescription": "WinZapp records computer audio into voice messages when you ask it to.",
        "NSFocusStatusUsageDescription": "WinZapp stays quiet while a Focus is on.",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.social-networking",
    })
    # Release builds (the macOS release workflow) turn the Mac updater on and
    # say where their updates are published (winzapp_mac/updater_mac.py).
    if os.environ.get("WINZAPP_MAC_RELEASES_REPO"):
        plist["WinZappMacReleasesRepo"] = os.environ["WINZAPP_MAC_RELEASES_REPO"]
    # The official tag and commit this app is built from: the tag is the
    # running version for the updater's downgrade check (client/version.py
    # is the unstamped placeholder in every tagged commit), the commit is
    # refused as an update.
    plist.pop("WinZappSourceCommit", None)
    plist.pop("WinZappReleaseTag", None)
    if source:
        plist["WinZappReleaseTag"], plist["WinZappSourceCommit"] = source
        # What Sparkle compares, ordered exactly like the tags
        # (provenance.bundle_version).
        plist["CFBundleVersion"] = load_provenance().bundle_version(source[0])
    plist.update(sparkle_plist(source))
    with open(plist_path, "wb") as fh:
        plistlib.dump(plist, fh)
    identity = os.environ.get("WINZAPP_SIGN_IDENTITY")
    if identity:
        sign_developer_id(identity, os.environ.get("WINZAPP_PROVISIONING_PROFILE"))
    else:
        run(["codesign", "--force", "--deep", "--sign", "-", APP])
    run(["codesign", "--verify", "--deep", "--strict", APP])


_MACHO_MAGICS = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce"}


def _is_macho(path):
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return False
    if head[:4] in _MACHO_MAGICS:
        return True
    # Universal binary; Java class files share the magic but their next
    # field (a version >= 45) is never a small architecture count.
    return head[:4] == b"\xca\xfe\xba\xbe" and 0 < int.from_bytes(head[4:8], "big") < 20


def sign_developer_id(identity, profile=None):
    """Sign every Mach-O inside out with the hardened runtime and a secure
    timestamp (what notarization requires), then the app itself.

    Node and the headless Chrome (processes the app launches) get
    entitlements/runtime.plist (JIT); libraries run under their host's.
    The app gets entitlements/app.plist, plus focus.plist and the embedded
    provisioning profile when one is given."""
    step(f"Developer ID signature ({identity})")
    ent = os.path.join(HERE, "entitlements")
    main_exe = os.path.join(APP, "Contents", "MacOS", "WinZapp")
    sparkle = os.path.join(FRAMEWORKS, "Sparkle.framework")
    binaries = []
    for root, dirs, files in os.walk(APP):
        if root == sparkle or root.startswith(sparkle + os.sep):
            continue            # signed as bundles below, the way Sparkle documents
        for f in files:
            p = os.path.join(root, f)
            if os.path.islink(p) or p == main_exe or not _is_macho(p):
                continue
            binaries.append(p)
    # deepest first, so containing code is signed after what it contains
    binaries.sort(key=lambda p: p.count(os.sep), reverse=True)
    runtime_exes = {"node", "chrome-headless-shell"}
    base = ["codesign", "--force", "--timestamp", "--options", "runtime", "--sign", identity]
    print(f"   signing {len(binaries)} binaries", flush=True)
    for p in binaries:
        cmd = list(base)
        if os.path.basename(p) in runtime_exes and os.access(p, os.X_OK):
            cmd += ["--entitlements", os.path.join(ent, "runtime.plist")]
        run_apple(cmd + [p], quiet=True)
    for p in sparkle_signing_order(sparkle):
        run_apple(base + [p], quiet=True)
    app_ent = os.path.join(BUILD, "app-entitlements.plist")
    with open(os.path.join(ent, "app.plist"), "rb") as fh:
        ents = plistlib.load(fh)
    if profile:
        shutil.copy2(profile, os.path.join(APP, "Contents", "embedded.provisionprofile"))
        with open(os.path.join(ent, "focus.plist"), "rb") as fh:
            ents.update(plistlib.load(fh))
        team = subprocess.run(["security", "cms", "-D", "-i", profile], capture_output=True).stdout
        prof = plistlib.loads(team)
        ents["com.apple.application-identifier"] = prof["Entitlements"]["com.apple.application-identifier"]
        ents["com.apple.developer.team-identifier"] = prof["Entitlements"]["com.apple.developer.team-identifier"]
    with open(app_ent, "wb") as fh:
        plistlib.dump(ents, fh)
    run(base + ["--entitlements", app_ent, APP])


def sparkle_signing_order(framework):
    """Sparkle's helpers, then the framework, as sparkle-project.org/
    documentation/sandboxing ("Code Signing") re-signs them for distribution:
    each on its own with the hardened runtime, never --deep. Its XPC services
    are not embedded (embed_sparkle), so their lines do not apply."""
    if not os.path.isdir(framework):
        return []
    version = os.path.join(framework, "Versions", "B")
    return [os.path.join(version, "Autoupdate"), os.path.join(version, "Updater.app"), framework]


def notarize():
    """Submit to Apple's notary service and staple the ticket to the app.
    Needs WINZAPP_NOTARY_KEY (path to the App Store Connect .p8),
    WINZAPP_NOTARY_KEY_ID and WINZAPP_NOTARY_ISSUER."""
    key = os.environ.get("WINZAPP_NOTARY_KEY")
    if not key:
        return False
    step("Notarization")
    sub = os.path.join(BUILD, "notarize.zip")
    if os.path.exists(sub):
        os.remove(sub)
    run(["ditto", "-c", "-k", "--keepParent", APP, sub])
    auth = ["--key", key, "--key-id", os.environ["WINZAPP_NOTARY_KEY_ID"],
            "--issuer", os.environ["WINZAPP_NOTARY_ISSUER"]]
    submit = ["xcrun", "notarytool", "submit", sub, "--wait", "--timeout", "45m",
              "--output-format", "json"] + auth
    sub_id = status = None
    for attempt in range(1, APPLE_ATTEMPTS + 1):
        print("   $", " ".join(submit), flush=True)
        r = subprocess.run(submit, capture_output=True, text=True)
        _print_output(r)
        sub_id, status = notary_status(r.stdout)
        if sub_id:
            break
        # No submission id: Apple was never reached (a timeout), so the
        # same archive can simply be sent again.
        print(f"   ! notarytool attempt {attempt}/{APPLE_ATTEMPTS} got no answer "
              f"(exit {r.returncode})", flush=True)
        if attempt < APPLE_ATTEMPTS:
            time.sleep(APPLE_RETRY_WAIT)
    if not sub_id:
        sys.exit("Notarization: Apple's notary service could not be reached")
    if status != "Accepted":
        # Apple's reasons (an unsigned binary, a missing hardened runtime...)
        # are only in the submission log; print it before failing.
        print(f"   notarization {sub_id} ended as {status}; Apple's log:", flush=True)
        run_apple(["xcrun", "notarytool", "log", sub_id] + auth)
        sys.exit(f"Notarization {status or 'failed'} ({sub_id})")
    run(["xcrun", "stapler", "staple", APP])
    run(["spctl", "--assess", "--type", "execute", "--verbose=2", APP])
    os.remove(sub)
    return True


def make_zip():
    out = os.path.join(DIST, f"WinZapp-macOS-{ARCH}.zip")
    step(f"Release archive -> {os.path.relpath(out, ROOT)}")
    if os.path.exists(out):
        os.remove(out)
    run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", APP, out])
    return out


# 7 ------------------------------------------------------------------------
def winzapp_running():
    return subprocess.run(["pgrep", "-f", "WinZapp.app/Contents/MacOS/WinZapp"],
                          capture_output=True).returncode == 0


def install():
    """Replace /Applications/WinZapp.app. Its runtime is installed into
    Application Support by the app itself at next launch."""
    step("Install app")
    if winzapp_running():
        sys.exit("WinZapp is running — quit it first (nothing was installed).")
    target = "/Applications/WinZapp.app"
    staging = target + ".new"
    if os.path.exists(staging):
        shutil.rmtree(staging)
    run(["ditto", APP, staging])
    if os.path.exists(target):
        old = target + ".old"
        if os.path.exists(old):
            shutil.rmtree(old)
        os.rename(target, old)
        os.rename(staging, target)
        shutil.rmtree(old)
    else:
        os.rename(staging, target)
    print(f"   installed {target}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-api", action="store_true", help="skip rebuilding client/api")
    ap.add_argument("--no-deps", action="store_true", help="skip pip")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--zip", action="store_true", help="also write a release .zip")
    ap.add_argument("--install-only", action="store_true",
                    help="install the last build (macos/dist) without rebuilding")
    a = ap.parse_args()
    if a.install_only:
        if not os.path.isdir(APP):
            sys.exit("no build in macos/dist")
        install()
        print("\nDone.")
        return
    # First, before minutes of work: a tag that does not hold stops here.
    source = release_source() if a.zip or os.environ.get("WINZAPP_MAC_RELEASES_REPO") else None
    sparkle_plist(source)       # a bad Sparkle key or feed stops here too, not at the end
    if not a.no_deps:
        python_deps()
    node_dir = node_runtime()
    fetch_ffmpeg()
    if not a.no_api:
        build_api(node_dir)
    sparkle_dir = fetch_sparkle()
    pyinstaller()
    bundle_runtime(node_dir)
    embed_sparkle(sparkle_dir)
    finish_bundle(source)
    notarize()
    if a.zip:
        write_provenance(source, make_zip())
    if a.install:
        install()
    print("\nDone.")


if __name__ == "__main__":
    main()
