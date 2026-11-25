"""Where the Mac app keeps things.

A frozen Windows WinZapp writes data/ next to its .exe and ships the Node
server (api/) and runtime (node/) beside it. Inside a Mac .app bundle that
would put the account data and the paired WhatsApp session inside the app,
wiped by every update. So, when frozen on macOS:

  ~/Library/Application Support/WinZapp/data/          accounts, messages, session
  ~/Library/Application Support/WinZapp/runtime/api    WPPConnect server
  ~/Library/Application Support/WinZapp/runtime/node   Node.js

The app carries its own runtime in Contents/Resources/runtime (built by
macos/build_app.py, stamped with a build id). At launch, if the installed
runtime's stamp differs, the bundled one is cloned into Application Support
(APFS clone: under a second, no extra disk), keeping the folders the server
writes to and the browser it downloaded; the old runtime is deleted in the
background. The server writes next to itself, so it must not run from inside
the signed app. Dev runs (macos/launcher.py) keep upstream's behaviour.
"""

import logging
import os
import re
import shutil
import signal
import subprocess
import sys

SUPPORT = os.path.expanduser("~/Library/Application Support/WinZapp")
RUNTIME = os.path.join(SUPPORT, "runtime")
_RUNTIME_PARTS = ("api", "node")
STAMP = "STAMP"
# Written by the server at run time; carried over when the runtime updates.
# Its browser download (.cache) too, by _carry_browser_cache().
KEEP_IN_API = ("log", "uploads", "WhatsAppImages", "wppconnect_tokens", "tokens", "userDataDir")


#: the frozen Neonize bridge, in Contents/Resources like the runtime
#: (macos/bridge_bundle.py); it runs from inside the app and writes only to its
#: data folder in Application Support
BRIDGE_FOLDER = "neonize_bridge"


def bundled_resource(meipass, *parts):
    """A path in Contents/Resources of the app whose PyInstaller folder
    (sys._MEIPASS, Contents/Frameworks) is `meipass`."""
    return os.path.join(os.path.dirname(meipass), "Resources", *parts)


def bundled_runtime():
    if not hasattr(sys, "_MEIPASS"):
        return None
    path = os.path.join(os.path.dirname(sys._MEIPASS), "Resources", "runtime")
    return path if os.path.isfile(os.path.join(path, STAMP)) else None


def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _clonefile(src, dst):
    """clonefile(2) of a whole directory tree: one call, in the kernel.
    False where APFS cannot do it (another volume, another file system)."""
    import ctypes
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.clonefile(os.fsencode(src), os.fsencode(dst), 0) == 0
    except (OSError, AttributeError):
        return False


def _clone_tree(src, dst):
    """APFS copy-on-write clone; a plain copy where cloning isn't possible.
    The directory clone is what makes an update's first launch quick: `cp -cR`
    clones the same 50,000 files one by one, 5.0 s against 0.6 s measured on
    the 2.1.0 runtime (2026-10-15)."""
    if _clonefile(src, dst):
        return
    if os.path.exists(dst):
        shutil.rmtree(dst, ignore_errors=True)
    r = subprocess.run(["cp", "-cR", src, dst], capture_output=True)
    if r.returncode != 0:
        if os.path.exists(dst):
            shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst, symlinks=True)


def _discard(path, runtime):
    """Take *path* out of the way now and delete it behind the app's back.

    Deleting the old runtime blocked the first launch after every update
    (1.9 s for 50,000 files). The rename is instant and frees the name; a
    detached `rm` does the rest and outlives the app if it quits first. What
    an interrupted `rm` leaves is swept by the next launch (_sweep_trash).
    Returns the `rm` process, or None when *path* was not there."""
    import uuid
    trash = "%s.trash-%s" % (runtime, uuid.uuid4().hex)
    try:
        os.rename(path, trash)
    except FileNotFoundError:
        return None
    except OSError:
        # Not in the background: the caller reuses the name at once (.new),
        # and an `rm` still running there would delete what it puts back.
        shutil.rmtree(path, ignore_errors=True)
        return None
    return _rm_detached(trash)


def _rm_detached(path):
    return subprocess.Popen(["rm", "-rf", path], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


def _sweep_trash(runtime):
    import glob
    for trash in glob.glob(glob.escape(runtime) + ".trash-*"):
        _rm_detached(trash)


# The browser start.js launches (api/.cache/<product>/<platform>-<version>):
# chrome-headless-shell on the Mac, full Chrome only as its fallback.
_BROWSER_PRODUCTS = ("chrome-headless-shell", "chrome")
_BROWSER_BUILD = re.compile(r"[a-z0-9_]+-(\d+(?:\.\d+){3})")


def pinned_browser_versions(api_dir):
    """{product: {versions}} the runtime's Puppeteer asks for, read from every
    puppeteer-core it holds (normally one). A product missing from the
    answer is one this could not read; nothing of it is pruned."""
    import glob
    pins = {}
    for revisions in (
            glob.glob(os.path.join(api_dir, "node_modules", "puppeteer-core", "lib", "cjs",
                                   "puppeteer", "revisions.js"))
            + glob.glob(os.path.join(api_dir, "node_modules", "*", "node_modules", "puppeteer-core",
                                     "lib", "cjs", "puppeteer", "revisions.js"))
            + glob.glob(os.path.join(api_dir, "node_modules", "@*", "*", "node_modules",
                                     "puppeteer-core", "lib", "cjs", "puppeteer", "revisions.js"))):
        text = _read(revisions) or ""
        for product in _BROWSER_PRODUCTS:
            m = re.search(r"""['"]?%s['"]?\s*:\s*['"](\d+(?:\.\d+){3})['"]""" % re.escape(product), text)
            if m:
                pins.setdefault(product, set()).add(m.group(1))
    return pins


def _carry_browser_cache(old_api, new_api, runtime):
    """Keep the downloaded browser across a runtime update.

    api/.cache is where the server downloads chrome-headless-shell (190 MB).
    It was not carried over, so the first launch after every update fetched
    it again (5.5 s on 2026-10-15, far longer on a slow connection). Builds
    the new runtime already holds win; one its Puppeteer no longer pins is
    discarded, so a new pin still downloads its own version (WinZapp only
    downloads when no usable browser is in the cache) and old ones do not
    pile up update after update."""
    old_cache, new_cache = os.path.join(old_api, ".cache"), os.path.join(new_api, ".cache")
    if os.path.isdir(old_cache):
        if not os.path.exists(new_cache):
            os.rename(old_cache, new_cache)
        else:
            for product in os.listdir(old_cache):
                src_dir = os.path.join(old_cache, product)
                if not os.path.isdir(src_dir) or os.path.islink(src_dir):
                    continue
                for name in os.listdir(src_dir):
                    dst = os.path.join(new_cache, product, name)
                    if not os.path.lexists(dst):
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        os.rename(os.path.join(src_dir, name), dst)
    if not os.path.isdir(new_cache):
        return
    pins = pinned_browser_versions(new_api)
    for product in _BROWSER_PRODUCTS:
        product_dir = os.path.join(new_cache, product)
        if product not in pins or not os.path.isdir(product_dir) or os.path.islink(product_dir):
            continue
        for name in os.listdir(product_dir):
            m = _BROWSER_BUILD.fullmatch(name)
            path = os.path.join(product_dir, name)
            if m and m.group(1) not in pins[product] and os.path.isdir(path):
                logging.info("[paths_mac] dropping %s %s: the runtime pins %s",
                             product, name, ", ".join(sorted(pins[product])))
                _discard(path, runtime)


def executable_of(pid):
    """Full path of the executable process *pid* runs (libproc), or None."""
    import ctypes
    import ctypes.util
    try:
        libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib")
        buf = ctypes.create_string_buffer(4096)         # PROC_PIDPATHINFO_MAXSIZE
        n = libproc.proc_pidpath(int(pid), buf, ctypes.sizeof(buf))
    except (OSError, AttributeError, ValueError):
        return None
    return os.fsdecode(buf.raw[:n]) if n > 0 else None


def runtime_node_pids(node_path):
    """This user's processes whose executable IS *node_path* — not every
    process that merely mentions the path on its command line."""
    try:
        out = subprocess.run(["pgrep", "-U", str(os.getuid()), "-f", node_path],
                             capture_output=True, text=True).stdout.split()
    except Exception:
        return []
    want = os.path.realpath(node_path)
    pids = []
    for pid in out:
        try:
            pid = int(pid)
        except ValueError:
            continue
        exe = executable_of(pid)
        if pid != os.getpid() and exe and os.path.realpath(exe) == want:
            pids.append(pid)
    return pids


def _stop_stale_node(node_path=None, grace=3.0):
    """A Node left running from the runtime being replaced (only after a
    crash — a normal quit stops it) would keep serving the old code. Asked
    to quit first; one still running after *grace* seconds (checked again,
    so a reused pid is never hit) is killed."""
    import time
    node_path = node_path or os.path.join(RUNTIME, "node", "node")
    pids = runtime_node_pids(node_path)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + grace
    while pids and time.monotonic() < deadline:
        time.sleep(0.1)
        pids = [pid for pid in pids if pid in runtime_node_pids(node_path)]
    for pid in pids:
        logging.warning("[paths_mac] stale Node %s ignored SIGTERM; killing it", pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def sync_runtime(bundle=None, runtime=RUNTIME):
    """Install the bundled runtime if it differs from the one in place.
    Returns True when it replaced (or first installed) the runtime."""
    bundle = bundle or bundled_runtime()
    if not bundle:
        return False
    import fcntl
    os.makedirs(os.path.dirname(runtime), exist_ok=True)
    # Several accounts' processes can start together; one installs.
    with open(runtime + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _sweep_trash(runtime)
        return _sync_locked(bundle, runtime)


def _sync_locked(bundle, runtime):
    want = _read(os.path.join(bundle, STAMP))
    if want and _read(os.path.join(runtime, STAMP)) == want:
        return False
    logging.info("[paths_mac] installing bundled runtime %s", want)
    _stop_stale_node()
    new, old = runtime + ".new", runtime + ".old"
    for p in (new, old):
        if os.path.lexists(p):
            _discard(p, runtime)
    os.makedirs(os.path.dirname(runtime), exist_ok=True)
    _clone_tree(bundle, new)
    for name in KEEP_IN_API:
        src = os.path.join(runtime, "api", name)
        if os.path.exists(src):
            dst = os.path.join(new, "api", name)
            if os.path.exists(dst):
                shutil.rmtree(dst, ignore_errors=True)
            os.rename(src, dst)
    if os.path.isdir(os.path.join(runtime, "api")):
        try:
            _carry_browser_cache(os.path.join(runtime, "api"), os.path.join(new, "api"), runtime)
        except OSError:
            # The browser is downloaded again at worst; never a reason to
            # keep running the old runtime.
            logging.exception("[paths_mac] could not carry the browser cache over")
    if os.path.exists(runtime):
        os.rename(runtime, old)
    os.rename(new, runtime)
    _discard(old, runtime)
    return True


def install():
    if not getattr(sys, "frozen", False):
        return
    import app_paths

    orig_resource_path = app_paths.resource_path

    def resource_path(*parts):
        if parts and parts[0] in _RUNTIME_PARTS:
            return os.path.join(RUNTIME, *parts)
        if parts and parts[0] == BRIDGE_FOLDER:
            return bundled_resource(sys._MEIPASS, *parts)
        return orig_resource_path(*parts)

    def writable_base_dir():
        os.makedirs(SUPPORT, exist_ok=True)
        return SUPPORT

    app_paths.resource_path = resource_path
    app_paths._writable_base_dir = writable_base_dir

    # platform_utils assumes a py2app layout (<exe>/Contents/MacOS) for
    # frozen Mac apps; sound_lib uses it to find its dylibs. PyInstaller
    # puts bundled files under sys._MEIPASS.
    from platform_utils import paths as pu_paths
    pu_paths.embedded_data_path = lambda: sys._MEIPASS

    try:
        sync_runtime()
    except Exception:
        logging.exception("[paths_mac] could not install the bundled runtime")
