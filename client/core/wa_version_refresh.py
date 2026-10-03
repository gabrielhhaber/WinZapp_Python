"""Keep the installed @wppconnect/wa-version catalogue from going stale.

start.js pins the WhatsApp Web main document to the newest build in
node_modules/@wppconnect/wa-version, while the workers and glue come from
WhatsApp live. A catalogue that has fallen behind therefore pins glue that no
longer matches Meta's worker bundle, and VoIP never initialises
(docs/traps/whatsapp-web-version-pin.md). The catalogue used to refresh only
when node_modules was rebuilt, i.e. when the user reinstalled WPPConnect by
hand, so testers silently kept an old one.

This module refreshes just that one package, in two phases, because the
package is ~43 MB to download (~35 s on a 10 Mbit link) and Node has to start
without waiting for it:

* STAGE (network, background thread, at most once per 6 hours): ask the
  registry for ``/latest`` (a few KB); when it is newer and acceptable,
  download, verify and extract it into a sibling of the live package
  (``wa-version.staged-<version>``), renamed into that name only once verified,
  so a half-finished stage is never mistaken for a complete one. The live
  package is never touched.
* APPLY (local, instant, never throttled): right before Node is spawned, swap
  a complete staged package in. A first update therefore takes effect one
  launch after it was downloaded, unless the download won the short bounded
  wait at startup.

Everything that touches the outside world is a parameter, so the tests inject
it: ``fetch``, ``clock``, the directories. Every failure is swallowed after one
log line: the refresh is an optimisation, never a reason to delay or break
startup.

Safety, because the package comes off the network and replaces files a running
install depends on:

* HTTPS to registry.npmjs.org only, no redirects, a size cap on every response.
* The tarball must match the registry's own ``dist.integrity`` (sha512 SRI),
  or ``dist.shasum`` when the registry gave no integrity.
* Extraction validates every member (regular files and directories, no
  absolute path, no "..", no links) into a temp dir beside the target.
* The result is sanity-checked before it is staged and again before it is
  applied, and the swap is rename-old-aside / rename-new-in with a rollback, so
  the install is never left without a working package.
* Several WinZapp processes share one node_modules: staging runs under one
  OS-level lock and applying under another, and each gives way silently when
  its lock is held. A stalled download holds only the stage lock, never the
  apply lock.

What is and is not guaranteed about swapping under a running Node: start.js
reads html/<build>.html from this package every time a session's page is
created (getPageContent, before the first navigation), not only once per
Node. So a swap that lands while a Node of ANY account is up can make a
session start in that Node find the directory missing or half renamed. The
apply therefore (a) runs only right before this process spawns Node, (b) is
skipped, leaving the package staged ("busy"), while another account holds a
live node-lease, and (c) never promotes a package whose html/ lacks the file
for the newest build in its own versions.json. What is NOT guaranteed: an
account started by hand between the busy check and the rename, or a Node from
a WinZapp that predates the lease; on Windows such a Node usually makes the
rename itself fail, which is also a skip.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import shutil
import tarfile
import threading
import time
import urllib.request
from urllib.parse import urlparse

from core.wa_version_catalogue import newest_build

PACKAGE = "@wppconnect/wa-version"
#: wppconnect declares the range it was built against for this package.
_WPPCONNECT_PACKAGE_JSON = ("@wppconnect-team", "wppconnect", "package.json")

REGISTRY_HOST = "registry.npmjs.org"
_REGISTRY_URL = f"https://{REGISTRY_HOST}/{PACKAGE}"

TIMEOUT_SECONDS = 8
CHECK_INTERVAL_SECONDS = 6 * 3600
#: How long startup waits for the stage thread before spawning Node anyway
#: (the thread is a daemon and keeps going; the next launch applies its result).
JOIN_SECONDS = 3

#: Absolute budgets for one response, whatever the link does in between.
LATEST_DEADLINE_SECONDS = 15
TARBALL_DEADLINE_SECONDS = 180
_CHUNK = 64 * 1024

MAX_VERSION_DOC_BYTES = 64 * 1024
#: The package measured 42.6 MB packed and 161 MB on disk (2026-10); the caps
#: leave room for the catalogue to keep growing without trusting a runaway.
MAX_TARBALL_BYTES = 120 * 1024 * 1024
MAX_EXTRACTED_BYTES = 400 * 1024 * 1024
MAX_MEMBERS = 20000

_STATE_FILE = "wa_version_refresh.json"
_FETCH_LOCK_FILE = "wa_version_refresh.lock"
_APPLY_LOCK_FILE = "wa_version_apply.lock"

# Outcomes that answer "is there something newer for me?" definitively, so the
# next check can wait the full interval. The others (offline, lock held, a
# download cut short) say nothing and are retried on the next launch.
_DEFINITIVE = {"staged", "current", "skipped-deps", "rejected"}

# ASCII digits, no leading zeros, fullmatch: no whitespace or newline slips
# through, and the string we pick is the canonical one.
_PLAIN_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


# ---------------------------------------------------------------- versions ---

def parse_version(version) -> tuple | None:
    """(major, minor, patch) of a plain release version, or None.

    A prerelease ("1.6.0-beta.1") or build-metadata version answers None: the
    refresh never installs one.
    """
    if not isinstance(version, str):
        return None
    m = _PLAIN_VERSION.fullmatch(version)
    return tuple(int(x) for x in m.groups()) if m else None


def satisfies_range(version, spec) -> bool | None:
    """Whether ``version`` meets ``spec``; None when the range is not one of
    the simple forms understood here ("x.y.z", "^x.y.z", "~x.y.z",
    ">=x.y.z", "*").

    Deliberately small: a full semver resolver is not worth carrying for a
    package that declares caret ranges. An unknown form answers None and the
    callers treat that as "cannot tell", never as a match.
    """
    v = parse_version(version)
    if v is None:
        return None
    spec = str(spec or "").strip()
    if spec in ("*", "latest", "x"):
        return True
    m = re.match(r"^(\^|~|>=|=)?\s*v?(\d+)\.(\d+)\.(\d+)$", spec)
    if not m:
        return None
    op = m.group(1) or "="
    base = tuple(int(x) for x in m.groups()[1:])
    if op in ("=",):
        return v == base
    if op == ">=":
        return v >= base
    if op == "~":
        return base <= v < (base[0], base[1] + 1, 0)
    # caret: the leftmost non-zero component is frozen
    if base[0] > 0:
        upper = (base[0] + 1, 0, 0)
    elif base[1] > 0:
        upper = (0, base[1] + 1, 0)
    else:
        upper = (0, 0, base[2] + 1)
    return base <= v < upper


def pick_version(installed, candidates, wanted_range=None) -> str | None:
    """The newest candidate worth installing over ``installed``, or None.

    Strictly newer, a plain release (no prerelease), the same major, and
    inside the range wppconnect declares when that range is readable. A range
    this module cannot parse is treated as unreadable: same major only.
    """
    have = parse_version(installed)
    if have is None:
        return None
    best = None
    for name in candidates:
        v = parse_version(name)
        if v is None or v <= have or v[0] != have[0]:
            continue
        if wanted_range and satisfies_range(name, wanted_range) is False:
            continue
        if best is None or v > parse_version(best):
            best = name
    return best


# --------------------------------------------------------------- integrity ---

def verify_integrity(data: bytes, integrity=None, shasum=None) -> bool:
    """True when ``data`` matches the registry's SRI string, or its sha1
    ``shasum`` when no integrity was given. Nothing to compare against is a
    failure, never a pass."""
    if integrity:
        for entry in str(integrity).split():
            alg, _, b64 = entry.partition("-")
            if alg not in ("sha256", "sha384", "sha512"):
                continue
            try:
                expected = base64.b64decode(b64.split("?")[0], validate=True)
            except (binascii.Error, ValueError):
                continue
            if hashlib.new(alg, data).digest() == expected:
                return True
        return False
    if shasum:
        return hashlib.sha1(data).hexdigest() == str(shasum).strip().lower()
    return False


def tarball_url_allowed(url) -> bool:
    """HTTPS and the registry's own host; nothing a version document says can
    point the download elsewhere."""
    try:
        parsed = urlparse(str(url))
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname == REGISTRY_HOST and not parsed.username


# -------------------------------------------------------------- extraction ---

class UnsafeArchive(Exception):
    """The tarball holds something that must not be written to disk."""


def _safe_relative(name: str) -> list:
    """The path parts of a tar member under its leading 'package/', or raise."""
    if "\\" in name or name.startswith("/"):
        raise UnsafeArchive("absolute or backslash path")
    parts = [p for p in name.split("/") if p != ""]
    if len(parts) < 1 or parts[0] != "package":
        raise UnsafeArchive("member outside package/")
    rest = parts[1:]
    for part in rest:
        if part in (".", "..") or ":" in part or part != part.strip():
            raise UnsafeArchive("unsafe path part")
    return rest


def safe_extract(tar_bytes: bytes, dest: str) -> None:
    """Extract an npm tarball into ``dest`` (created, must not exist).

    Not tarfile.extractall(): the names come from the network. Every member
    must be a regular file or a directory; links, devices and anything that
    escapes ``dest`` raise UnsafeArchive. Member count and total size are
    capped.
    """
    os.makedirs(dest)
    root = os.path.realpath(dest)
    total = 0
    count = 0
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
        for member in tf:
            count += 1
            if count > MAX_MEMBERS:
                raise UnsafeArchive("too many members")
            if not (member.isreg() or member.isdir()):
                raise UnsafeArchive("non-regular member")
            rest = _safe_relative(member.name)
            if not rest:
                continue  # the package/ directory itself
            target = os.path.realpath(os.path.join(root, *rest))
            if os.path.commonpath([root, target]) != root:
                raise UnsafeArchive("path escapes destination")
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            # A duplicate, or a name differing only by case (the same file on
            # Windows), would let a later member overwrite a validated one.
            folded = tuple(part.lower() for part in rest)
            if folded in seen:
                raise UnsafeArchive("duplicate member")
            seen.add(folded)
            total += max(member.size, 0)
            if total > MAX_EXTRACTED_BYTES:
                raise UnsafeArchive("archive too large")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            src = tf.extractfile(member)
            with open(target, "wb") as out:
                shutil.copyfileobj(src, out)


# --------------------------------------------------------------- the check ---

def _read_json(path: str):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def dependencies_satisfied(package_json: dict, new_dir: str, node_modules: str) -> bool:
    """Whether every runtime dependency the new package declares is already
    installed at a version that meets its range.

    Resolution mirrors Node's: the package's own nested node_modules first
    (carried over from the old copy), then the shared one. No install can run
    from here, so a missing or too-old dependency, or a range this module
    cannot read, means "leave it to the in-app reinstall".
    """
    deps = package_json.get("dependencies") or {}
    if not isinstance(deps, dict):
        return False
    for name, spec in deps.items():
        found = None
        for base in (os.path.join(new_dir, "node_modules"), node_modules):
            found = _read_json(os.path.join(base, *str(name).split("/"), "package.json"))
            if isinstance(found, dict):
                break
        if not isinstance(found, dict) or satisfies_range(found.get("version"), spec) is not True:
            return False
    return True


def sanity_check(new_dir: str, expected_version: str) -> bool:
    """The extracted package is the one we asked for and its catalogue parses."""
    pkg = _read_json(os.path.join(new_dir, "package.json"))
    if not isinstance(pkg, dict) or pkg.get("name") != PACKAGE or pkg.get("version") != expected_version:
        return False
    if newest_build(_read_json(os.path.join(new_dir, "versions.json"))) is None:
        return False
    main = pkg.get("main")
    if not (isinstance(main, str) and os.path.isfile(os.path.join(new_dir, *main.split("/")))):
        return False
    # start.js serves html/<newest build>.html; a package without it would pin
    # a build it cannot assemble.
    newest = newest_build(_read_json(os.path.join(new_dir, "versions.json")))
    html = os.path.join(new_dir, "html", f"{newest}.html")
    return os.path.isfile(html) and os.path.getsize(html) > 0


def swap_directories(target: str, new_dir: str, stamp, *, rename=None) -> str:
    """Replace ``target`` with ``new_dir``; on any failure put the old one back.
    Returns the path the old copy was renamed to; deleting it (161 MB) is the
    caller's business, off the critical path.

    Raises when the swap did not happen (a Windows rename of a directory some
    Node has open fails here, and that is the expected way to give up).
    """
    rename = rename or os.rename
    old = f"{target}.old-{stamp}"
    rename(target, old)
    try:
        rename(new_dir, target)
    except BaseException:
        try:
            rename(old, target)
        except OSError:
            logging.error("[wa-version] could not restore the previous catalogue")
        raise
    return old


@contextlib.contextmanager
def exclusive_lock(path: str):
    """Yield True when this process took the cross-process lock, else False."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a+b")
    held = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = True
        except OSError:
            held = False
        yield held
    finally:
        if held:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: the registry answers directly, and following one
    would leave the host the URL checks were made against."""

    def redirect_request(self, *args, **kwargs):
        return None


def default_fetch(url: str, max_bytes: int, deadline: float = LATEST_DEADLINE_SECONDS, *,
                  timeout: float = TIMEOUT_SECONDS, opener=None,
                  clock=time.monotonic) -> bytes:
    """GET over HTTPS with a hard size cap and an ABSOLUTE deadline.

    ``timeout`` bounds each socket operation only, so a server trickling bytes
    would never trip it; the deadline is checked after every chunk, and the
    size cap is enforced while streaming. ``opener`` is a test seam; the
    https-only rule is not.
    """
    if urlparse(url).scheme != "https":
        raise ValueError("https only")
    opener = opener or urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={
        "Accept": "application/json, application/octet-stream",
        "User-Agent": "WinZapp",
    })
    started = clock()
    chunks = []
    total = 0
    with opener.open(req, timeout=timeout) as resp:
        declared = resp.headers.get("Content-Length") if getattr(resp, "headers", None) else None
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise ValueError("response too large")
        while True:
            if clock() - started > deadline:
                raise TimeoutError("deadline exceeded")
            chunk = resp.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("response too large")
            chunks.append(chunk)
    return b"".join(chunks)


# ------------------------------------------------------------------ stage ---

def _wanted_range(node_modules: str):
    pkg = _read_json(os.path.join(node_modules, *_WPPCONNECT_PACKAGE_JSON))
    deps = pkg.get("dependencies") if isinstance(pkg, dict) else None
    spec = deps.get(PACKAGE) if isinstance(deps, dict) else None
    return spec if isinstance(spec, str) else None


def _target(node_modules: str) -> str:
    return os.path.join(node_modules, *PACKAGE.split("/"))


def _installed_version(target: str) -> str:
    pkg = _read_json(os.path.join(target, "package.json"))
    version = pkg.get("version") if isinstance(pkg, dict) else ""
    return version if parse_version(version) is not None else ""


def _siblings(target: str, marker: str) -> list:
    """Paths next to ``target`` named ``<name>.<marker>-<suffix>``."""
    parent, name = os.path.split(target)
    prefix = f"{name}.{marker}-"
    try:
        found = [e for e in os.listdir(parent) if e.startswith(prefix)]
    except OSError:
        return []
    return [os.path.join(parent, e) for e in found]


def _staged_path(target: str, version: str) -> str:
    return f"{target}.staged-{version}"


def check_and_stage(node_modules: str, state_dir: str, *, fetch=default_fetch,
                    clock=time.time, interval: float = CHECK_INTERVAL_SECONDS) -> str:
    """The network half: stage a newer catalogue package; never raises.

    Outcomes: "staged" (a complete package now waits to be applied), "current",
    "throttled", "locked", "missing" (no package installed), "offline",
    "skipped-deps", "rejected" (a check failed: hash, archive, sanity),
    "error".
    """
    outcome = "error"
    old_version = ""
    try:
        target = _target(node_modules)
        old_version = _installed_version(target)
        if not old_version:
            outcome = "missing"
            return outcome
        with exclusive_lock(os.path.join(state_dir, _FETCH_LOCK_FILE)) as held:
            if not held:
                outcome = "locked"
                return outcome
            state_path = os.path.join(state_dir, _STATE_FILE)
            state = _read_json(state_path)
            last = state.get("checked") if isinstance(state, dict) else None
            now = clock()
            if isinstance(last, (int, float)) and 0 <= now - last < interval:
                outcome = "throttled"
                return outcome
            # We hold the only lock that stages, so a *.staging-* left here is
            # a download some earlier process never finished.
            for stale in _siblings(target, "staging"):
                shutil.rmtree(stale, ignore_errors=True)
            outcome = _stage(node_modules, target, old_version, fetch, now)
            if outcome in _DEFINITIVE:
                try:
                    os.makedirs(state_dir, exist_ok=True)
                    with open(state_path, "w", encoding="utf-8") as fh:
                        json.dump({"checked": now}, fh)
                except OSError:
                    pass
        return outcome
    except Exception as exc:
        logging.warning("[wa-version] refresh failed (%s)", type(exc).__name__)
        return outcome
    finally:
        if outcome != "throttled":
            logging.info("[wa-version] catalogue check: %s (installed %s)", outcome,
                         old_version or "none")


def _stage(node_modules, target, old_version, fetch, now) -> str:
    try:
        doc = json.loads(fetch(f"{_REGISTRY_URL}/latest", MAX_VERSION_DOC_BYTES,
                               LATEST_DEADLINE_SECONDS))
    except (OSError, ValueError):
        # urllib's URLError/timeouts are OSError; bad JSON is ValueError.
        return "offline"
    version = doc.get("version") if isinstance(doc, dict) else None
    # Only /latest is read, never the packument (7.6 MB abbreviated). When the
    # newest release is a major bump or outside wppconnect's range, nothing is
    # staged and the in-app reinstall remains the way forward.
    if pick_version(old_version, [version], _wanted_range(node_modules)) is None:
        return "current"
    if os.path.isdir(_staged_path(target, version)):
        return "staged"
    dist = doc.get("dist")
    if not isinstance(dist, dict) or not tarball_url_allowed(dist.get("tarball")):
        return "rejected"
    # Cheaper to learn now than after 43 MB: the dependencies the new package
    # declares must already be installed (the old copy's nested ones count).
    if not dependencies_satisfied(doc, target, node_modules):
        return "skipped-deps"
    try:
        data = fetch(dist["tarball"], MAX_TARBALL_BYTES, TARBALL_DEADLINE_SECONDS)
    except (OSError, ValueError):
        return "offline"
    if not verify_integrity(data, dist.get("integrity"), dist.get("shasum")):
        return "rejected"
    # Extracted under a name no apply looks at, renamed to the staged name only
    # when it is whole: a crash or a closed window leaves a *.staging-* that
    # the next check removes, never a staged package that is half a package.
    staging = f"{target}.staging-{int(now)}"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        try:
            safe_extract(data, staging)
        except (UnsafeArchive, tarfile.TarError, OSError, EOFError, ValueError):
            return "rejected"
        if not sanity_check(staging, version):
            return "rejected"
        try:
            os.rename(staging, _staged_path(target, version))
        except OSError:
            return "error"
        return "staged"
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ------------------------------------------------------------------ apply ---

_discards: list = []


def _discard(path: str) -> None:
    """Delete a directory tree off the caller's thread, best effort.

    The old package is 161 MB of small files; removing it must not sit between
    the swap and the Node spawn. A process that exits first leaves it behind,
    and the next apply sweeps it.
    """
    thread = threading.Thread(target=shutil.rmtree, args=(path, True),
                              name="wa-version-discard", daemon=True)
    _discards.append(thread)
    thread.start()


def join_discards(timeout: float = 30) -> None:
    """Wait for pending background deletions (tests; never called at startup)."""
    while _discards:
        _discards.pop().join(timeout)


def apply_staged(node_modules: str, state_dir: str, *, clock=time.time, busy=None) -> str:
    """The local half: swap the newest complete staged package in; never raises.

    Called right before Node is spawned, so it must be quick and must never
    touch the network. ``busy`` answers "is another account's Node alive?"; if
    so the package stays staged (see the module docstring for why).

    Outcomes: "updated", "nothing-staged", "busy", "locked", "missing",
    "skipped-deps", "rejected", "error".
    """
    outcome = "error"
    try:
        target = _target(node_modules)
        with exclusive_lock(os.path.join(state_dir, _APPLY_LOCK_FILE)) as held:
            if not held:
                outcome = "locked"
                return outcome
            _restore_if_interrupted(target)
            old_version = _installed_version(target)
            if not old_version:
                outcome = "missing"
                return outcome
            staged = {}
            for path in _siblings(target, "staged"):
                name = path.rsplit(".staged-", 1)[1]
                if parse_version(name) is None:
                    _discard(path)
                else:
                    staged[name] = path
            best = pick_version(old_version, staged.keys(), _wanted_range(node_modules))
            for version, path in staged.items():
                # Not newer than what is installed any more (another process
                # applied it, or a reinstall passed it): nothing will use it.
                if version != best and parse_version(version) <= parse_version(old_version):
                    _discard(path)
            if best is None:
                outcome = "nothing-staged"
                return outcome
            if busy is not None and busy():
                outcome = "busy"
                return outcome
            outcome = _apply_one(node_modules, target, staged[best], best, old_version, clock())
            return outcome
    except Exception as exc:
        logging.warning("[wa-version] apply failed (%s)", type(exc).__name__)
        return outcome
    finally:
        if outcome != "nothing-staged":
            logging.info("[wa-version] catalogue apply: %s", outcome)


def _restore_if_interrupted(target: str) -> None:
    """A crash between the two renames of a swap leaves no package at all and
    the old one beside it: put it back before anything else."""
    if os.path.isdir(target):
        for leftover in _siblings(target, "old"):
            _discard(leftover)
        return
    leftovers = sorted(_siblings(target, "old"))
    if leftovers:
        try:
            os.rename(leftovers[-1], target)
            logging.warning("[wa-version] restored the catalogue after an interrupted swap")
        except OSError:
            pass


def _apply_one(node_modules, target, staged_dir, version, old_version, now) -> str:
    if not sanity_check(staged_dir, version):
        _discard(staged_dir)
        return "rejected"
    # The old copy's own node_modules (semver) rides along; the registry
    # tarball never ships one. Redone on every attempt, since a failed swap
    # leaves the staged dir behind with the previous copy inside.
    old_nested = os.path.join(target, "node_modules")
    new_nested = os.path.join(staged_dir, "node_modules")
    if os.path.isdir(old_nested):
        shutil.rmtree(new_nested, ignore_errors=True)
        shutil.copytree(old_nested, new_nested)
    new_pkg = _read_json(os.path.join(staged_dir, "package.json"))
    if not dependencies_satisfied(new_pkg, staged_dir, node_modules):
        _discard(staged_dir)
        return "skipped-deps"
    try:
        old = swap_directories(target, staged_dir, int(now))
    except OSError:
        # A Windows directory some Node has open cannot be renamed: the staged
        # package stays for the next launch.
        return "error"
    _discard(old)
    logging.info("[wa-version] catalogue %s -> %s", old_version, version)
    return "updated"


# ----------------------------------------------------------- startup hooks ---

_thread: threading.Thread | None = None
_paths: tuple | None = None
_busy = None
_hook_lock = threading.Lock()


def other_accounts_node_alive(global_dir, account_id, ignore_corrupt: bool = False) -> bool:
    """True when another account of this install holds a live node-lease.

    Fails closed: a lease that cannot be read, or a lookup that raises, counts
    as alive. Without a global dir or account id there is nobody to ask.

    ``ignore_corrupt`` is for a caller that only asks "may I prompt?" (the
    WPPConnect update): an unreadable lease file is logged, once per call, and
    skipped, because treating it as alive would silence that prompt for good.
    A lookup that raises still counts as alive.
    """
    if not global_dir or not account_id:
        return False
    try:
        import node_coord
        import update_coord
        leases = node_coord.live_node_leases(global_dir, is_alive=update_coord.lease_alive)
    except Exception:
        return True
    if ignore_corrupt:
        corrupt = [lease for lease in leases if lease.get("_corrupt")]
        if corrupt:
            logging.warning("[wa_version_refresh] ignoring %d unreadable node lease(s) "
                            "for this check", len(corrupt))
        leases = [lease for lease in leases if not lease.get("_corrupt")]
        return any(lease.get("account_id") != account_id for lease in leases)
    return any(lease.get("_corrupt") or lease.get("account_id") != account_id
               for lease in leases)


def start_in_background(node_modules: str, state_dir: str, *, check: bool = True,
                        busy=None, **kwargs) -> bool:
    """Remember where the catalogue lives and, when ``check`` is set, start the
    stage thread (daemon, once per process). True when this call started it.

    ``check`` is False while a Node is already running: staging is harmless
    then, but a launch that will not spawn gains nothing from it.
    """
    global _thread, _paths, _busy
    with _hook_lock:
        _paths = (node_modules, state_dir)
        _busy = busy
        if not check or _thread is not None:
            return False
        _thread = threading.Thread(
            target=check_and_stage, args=(node_modules, state_dir), kwargs=kwargs,
            name="wa-version-stage", daemon=True,
        )
        _thread.start()
        return True


def start_at_launch(window, node_modules: str, state_dir: str) -> bool:
    """The startup hook (MainWindow.__init__): wire the refresh to a window's
    own Node and account."""
    gd = getattr(window, "global_dir", None)
    acc = getattr(window, "account_id", None)
    return start_in_background(
        node_modules, state_dir, check=not window._is_wpp_running(),
        busy=lambda: other_accounts_node_alive(gd, acc),
    )


def wait_for_refresh(timeout: float = JOIN_SECONDS, **kwargs) -> None:
    """Call right before spawning Node, from a worker, never the UI thread:
    give the stage thread ``timeout`` seconds (never longer; it is abandoned,
    not stopped, and the next launch applies what it finishes), then apply
    whatever complete package is staged.

    Applying runs on every call, including later respawns of Node. A no-op
    when startup never registered the paths.
    """
    thread, paths = _thread, _paths
    if paths is None:
        return
    if thread is not None and thread.is_alive():
        thread.join(timeout)
        if thread.is_alive():
            logging.info("[wa-version] download still running; not waiting for it")
    kwargs.setdefault("busy", _busy)
    apply_staged(*paths, **kwargs)
