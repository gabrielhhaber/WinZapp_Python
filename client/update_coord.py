"""
Updater coordination for WinZapp multi-account (client/update_coord.py)
=======================================================================

Closes the updater TOCTOU (plan Zad. 2.-1). Everything runs UNDER the dedicated
``updater_lock`` (never the registry lock).

Two coordinated pieces:

  * runtime-lease — a per-process marker file ``global/runtime/<pid>_<ct>_<id>``.
    Created VERY EARLY in bootstrap via ``try_create_runtime_lease`` (refuses if
    an update is in progress). The updater refuses to install while any *other*
    live runtime-lease exists. Leases key on (pid, process_create_time) so a
    reused PID from a dead process is never mistaken for a live holder.

  * update_state.json — ``{update_in_progress, owner_pid, owner_create_time,
    owner_token}`` guarded ONLY by ``updater_lock``. ``try_begin_update`` claims
    it atomically (refusing a live owner or any live account lease) and returns
    an owner-token; only the matching token may ``end_update``. Bootstrap
    consults it and refuses to start mid-install. A crashed owner (dead pid) is
    auto-recovered; an unreadable/invalid state file fails CLOSED (blocks).

Hardening (GPT code review r2/r3): all writes are atomic (tmp+fsync+os.replace);
all persisted ints/floats are strictly validated so a crafted value can't raise
mid-scan or be mis-swept; owner_token (uuid4) identifies a specific install run.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import uuid
from typing import Callable, Optional

from coord_locks import updater_lock

_RUNTIME_DIR = "runtime"
_STATE_FILE = "update_state.json"
_CORRUPT = object()


# ── strict scalar validation (total; fail-closed on anything odd) ────────────
_MAX_PID = 2 ** 31  # comfortably above any real OS pid


def _valid_pid(v) -> bool:
    try:
        return isinstance(v, int) and not isinstance(v, bool) and 0 < v < _MAX_PID
    except Exception:
        return False


def _valid_ct(v) -> bool:
    # Must be a finite, NON-NEGATIVE number. Total: never raises (a huge JSON
    # int can make math.isfinite raise OverflowError — caught here). GPT r5 #1.
    try:
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return False
        return math.isfinite(v) and v >= 0
    except (OverflowError, ValueError, TypeError):
        return False


# ── process liveness (pid + create_time, guards against PID reuse) ───────────
# Sentinel meaning "process exists but its create_time can't be determined".
_CT_UNKNOWN = 0.0


def _default_proc_create_time(pid: int):
    """Return the process create_time, the _CT_UNKNOWN sentinel if it exists but
    can't be measured, or None ONLY when the process provably does NOT exist.
    A transient error (AccessDenied, etc.) must NOT be reported as death
    (GPT r4 #2) — we return the sentinel so callers fail CLOSED (treat as live).
    """
    try:
        import psutil  # type: ignore
        try:
            return psutil.Process(pid).create_time()
        except psutil.NoSuchProcess:
            return None
        except psutil.Error:
            return _CT_UNKNOWN  # exists-but-unknown / access denied -> live
    except ImportError:
        pass
    # No psutil. On Windows, os.kill(pid, 0) is NOT a liveness probe — it calls
    # TerminateProcess and would KILL the process (including our own). Use
    # OpenProcess via ctypes instead (GPT-review class bug found on real Win run).
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            # Could be "no such process" (dead) or access-denied (alive-but-ours-not).
            err = ctypes.get_last_error()
            ERROR_INVALID_PARAMETER = 87  # pid doesn't exist
            if err == ERROR_INVALID_PARAMETER:
                return None
            return _CT_UNKNOWN  # access denied etc. -> fail closed (treat as live)
        # Process handle opened: it's alive. Check whether it already exited.
        try:
            STILL_ACTIVE = 259
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                if code.value != STILL_ACTIVE:
                    return None  # already exited
            # The REAL creation time, not the sentinel. psutil is not shipped,
            # so this branch is what every installed WinZapp runs, and returning
            # _CT_UNKNOWN here made every lease and claim record 0.0 — which
            # lease_alive() accepts for ANY process holding that pid. Windows
            # reuses pids quickly (measured on one install: svchost,
            # RuntimeBroker and msedgewebview2 sitting on the pids of WinZapps
            # that had long exited), so a claim left behind by a finished
            # update looked alive forever, and "check for updates" said a
            # dialog was open in another account on machines with one account.
            # Same conversion as psutil: FILETIME 100 ns ticks since 1601.
            ft_create, ft_exit, ft_kernel, ft_user = (
                wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME())
            if kernel32.GetProcessTimes(h, ctypes.byref(ft_create), ctypes.byref(ft_exit),
                                        ctypes.byref(ft_kernel), ctypes.byref(ft_user)):
                ticks = (ft_create.dwHighDateTime << 32) | ft_create.dwLowDateTime
                if ticks > 116444736000000000:
                    return (ticks - 116444736000000000) / 10_000_000
            return _CT_UNKNOWN  # alive, create_time could not be read
        finally:
            kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return _CT_UNKNOWN  # alive, unknown create_time
    except ProcessLookupError:
        return None
    except PermissionError:
        return _CT_UNKNOWN  # exists but not ours -> treat as live
    except OSError:
        return _CT_UNKNOWN  # unknown -> fail closed


def _current_image_basename() -> str:
    r"""Lower-cased basename of the image THIS process is actually running.

    ``sys.executable`` is not always that image. Under the Microsoft Store
    build of Python it reports the WindowsApps *alias*
    (``...\AppData\Local\Microsoft\WindowsApps\...\python.exe``) while the
    process Windows actually created — and the name every process listing,
    psutil included, reports for it — is ``python3.13.exe`` under
    ``C:\Program Files\WindowsApps\...``. Both liveness paths below compare
    image basenames, so on such an install WinZapp failed to recognise even
    its own pid: `_default_proc_matches_us(os.getpid())` returned False, and
    a live holder read as dead. ``GetModuleFileNameW(NULL)`` resolves the
    real image regardless of the alias, without adding a psutil dependency
    the installed build does not have.
    """
    if sys.platform != "win32":
        return ""
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetModuleFileNameW(None, buffer, len(buffer)):
            return os.path.basename(buffer.value).lower()
    except Exception:
        pass
    return ""


def _expected_process_basenames() -> set:
    """Lower-cased executable basenames a live WinZapp process could have.

    sys.executable covers both the frozen build (WinZapp.exe) and a dev-mode
    run (python.exe/pythonw.exe); the literal fallback guards the unlikely
    case sys.executable reports something else in a bundled runner. The real
    running image is added on top of it, because the two genuinely differ
    under an aliased interpreter — see _current_image_basename()."""
    names = {"winzapp.exe"}
    try:
        exe = os.path.basename(sys.executable or "").lower()
        if exe:
            names.add(exe)
    except Exception:
        pass
    image = _current_image_basename()
    if image:
        names.add(image)
    return names


def _default_proc_matches_us(pid: int) -> Optional[bool]:
    """True/False if *pid*'s executable image can be positively identified as
    (not) a WinZapp process; None when inconclusive — callers must treat None
    exactly like an unknown create_time (fail closed, i.e. still "alive").

    Measured live on a real install: the leaked 0.0-create_time leases this
    exists to reap had their pids reused by Windows SERVICE processes
    (svchost.exe, vmms.exe, vmcompute.exe — session 0, running as SYSTEM).
    OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) against one of those from
    an ordinary user-session WinZapp.exe fails with access-denied, not "no
    such process" — indistinguishable, to that API, from a process we simply
    aren't allowed to ask about. That turned every real collision into the
    inconclusive case, which still fails closed, so the fix never actually
    broke the tie it was written for. CreateToolhelp32Snapshot lists every
    process's image name system-wide without opening a handle to any of
    them, so it reads a SYSTEM service's name with the same ordinary-user
    permissions that list it in Task Manager."""
    expected = _expected_process_basenames()
    try:
        import psutil  # type: ignore
        try:
            return psutil.Process(pid).name().lower() in expected
        except psutil.NoSuchProcess:
            return False
        except psutil.Error:
            return None
    except ImportError:
        pass
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        return None  # inconclusive -> fail closed
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return None
        while True:
            if entry.th32ProcessID == pid:
                return entry.szExeFile.lower() in expected
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                return False  # walked every process; this pid isn't one of them
    finally:
        kernel32.CloseHandle(snapshot)


def lease_alive(pid: int, create_time: float,
                proc_create_time: Callable[[int], Optional[float]] = _default_proc_create_time,
                proc_matches_us: Callable[[int], Optional[bool]] = None) -> bool:
    """True iff a process with this pid AND matching create_time is alive.

    The 0.0 sentinel (unknown create_time) matches leases recorded as 0.0 —
    but only while *pid* could plausibly still be us. Left unqualified, that
    match never expires: a lease is written once at process start and never
    refreshed, so a process that crashes or is killed without reaching
    release_runtime_lease() leaves a 0.0-recorded lease on disk forever, and
    once Windows reuses that pid for ANY other process (routine — measured
    live: svchost, a browser tab, an unrelated service), proc_create_time()
    keeps returning a real value and this kept saying "alive". Reported live
    on a genuinely single-account install: 11 leaked 0.0 leases going back
    over two weeks, one of them colliding with whatever pid a live Windows
    process happened to hold that moment, so try_begin_update() refused the
    install believing another account was still open.
    proc_matches_us disambiguates by process IMAGE NAME rather than by
    existence alone — the one thing that can positively PROVE a pid is no
    longer ours (a definite False), as opposed to merely being unable to
    prove it's dead (None, which still fails closed, same as before)."""
    ct = proc_create_time(pid)
    if ct is None:
        return False
    if ct == 0.0 or create_time == 0.0:
        matcher = proc_matches_us or _default_proc_matches_us
        return matcher(pid) is not False
    return abs(ct - create_time) < 1e-6


def prompt_owner_alive(pid: int, create_time: float,
                       proc_create_time: Callable[[int], Optional[float]] = _default_proc_create_time) -> bool:
    """Liveness for the update-PROMPT claim: alive only on a positive match.

    lease_alive() fails CLOSED — unknown counts as alive — because a lease
    guards the install, and installing under a running account corrupts it.
    The prompt claim guards against nothing worse than a second dialog, and its
    reader already fails open for that reason (_read_prompt). Letting an
    unknown create_time count as alive there is what kept a claim left behind
    by an update alive for as long as ANY process sat on its old pid, telling
    people with a single account that another account's dialog was open. So
    here both sides must be known and equal; a claim recorded as 0.0 by an
    older build is never trusted, which also clears the ones already stuck.
    """
    if create_time == _CT_UNKNOWN:
        return False
    ct = proc_create_time(pid)
    if ct is None or ct == _CT_UNKNOWN:
        return False
    return abs(ct - create_time) < 1e-6


# ── atomic json write ────────────────────────────────────────────────────────
def _atomic_write(path: str, payload: dict) -> None:
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ── runtime leases ───────────────────────────────────────────────────────────
def _runtime_dir(global_dir: str) -> str:
    d = os.path.join(global_dir, _RUNTIME_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def _resolve_identity(pid, create_time):
    if pid is None:
        pid = os.getpid()
    if create_time is None:
        ct = _default_proc_create_time(pid)
        create_time = ct if ct is not None else 0.0
    # Validate the resolved identity (GPT r4 #1) so a bad caller value can't
    # write a permanently-corrupt lease / update_state. create_time must be
    # finite and >= 0.
    if not _valid_pid(pid) or not _valid_ct(create_time) or create_time < 0:
        raise ValueError(f"invalid process identity pid={pid!r} create_time={create_time!r}")
    return pid, create_time


def _write_lease(d: str, pid: int, create_time: float) -> str:
    lease_id = uuid.uuid4().hex[:8]
    fname = f"{pid}_{create_time}_{lease_id}"
    _atomic_write(os.path.join(d, fname),
                  {"pid": pid, "create_time": create_time,
                   "lease_id": lease_id, "started_at": int(time.time())})
    return fname


def try_create_runtime_lease(global_dir: str, pid: Optional[int] = None,
                             create_time: Optional[float] = None,
                             is_alive: Callable[[int, float], bool] = lease_alive):
    """Atomically create a runtime-lease unless an update is in progress.
    Returns the lease filename, or None if blocked. Whole check+write under one
    updater_lock (GPT r2 #1)."""
    pid, create_time = _resolve_identity(pid, create_time)
    with updater_lock(global_dir):
        if _is_update_in_progress_locked(global_dir, is_alive):
            return None
        return _write_lease(_runtime_dir(global_dir), pid, create_time)


def release_runtime_lease(global_dir: str, lease_name: str) -> None:
    # Guard against path traversal / absolute paths (GPT r5 #3): a lease name
    # must be a plain filename in the runtime dir, nothing else.
    if (not lease_name or os.path.basename(lease_name) != lease_name
            or lease_name in (os.curdir, os.pardir)):
        return
    with updater_lock(global_dir):
        path = os.path.join(global_dir, _RUNTIME_DIR, lease_name)
        # Confirm the resolved path really stays inside the runtime dir.
        rt = os.path.realpath(_runtime_dir(global_dir))
        if os.path.dirname(os.path.realpath(path)) != rt:
            return
        try:
            os.remove(path)
        except OSError:
            pass


def _live_leases_locked(global_dir: str, is_alive: Callable[[int, float], bool]) -> list[dict]:
    """Scan leases; caller holds updater_lock. Unreadable OR type-invalid lease
    (can't prove dead) -> treated as a LIVE unknown holder, blocking updates
    (fail-closed, GPT r2 #5 / r3 #3). Leftover *.tmp files are ignored/removed."""
    d = _runtime_dir(global_dir)
    live: list[dict] = []
    for fname in os.listdir(d):
        if fname.endswith(".tmp"):
            try:
                os.remove(os.path.join(d, fname))
            except OSError:
                pass
            continue
        path = os.path.join(d, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                lease = json.load(f)
            pid, ct = lease["pid"], lease["create_time"]
        except (OSError, ValueError, KeyError, TypeError):
            live.append({"pid": None, "create_time": None, "_name": fname, "_corrupt": True})
            continue
        if not _valid_pid(pid) or not _valid_ct(ct):
            live.append({"pid": None, "create_time": None, "_name": fname, "_corrupt": True})
            continue
        if is_alive(pid, float(ct)):
            lease["_name"] = fname
            live.append(lease)
        else:
            try:
                os.remove(path)
            except OSError:
                pass
    return live


def live_runtime_leases(global_dir: str, is_alive: Callable[[int, float], bool] = lease_alive) -> list[dict]:
    with updater_lock(global_dir):
        return _live_leases_locked(global_dir, is_alive)


# ── update_state.json ────────────────────────────────────────────────────────
def _state_path(global_dir: str) -> str:
    return os.path.join(global_dir, _STATE_FILE)


def _read_state(global_dir: str):
    """Return the state dict, or _CORRUPT if the file exists but is unreadable
    or fails strict validation (callers fail closed)."""
    path = _state_path(global_dir)
    if not os.path.lexists(path):
        # Truly absent -> initial state. A dir / broken symlink / other
        # non-regular file existing here is NOT "no update" — it's corrupt,
        # so fail closed (GPT r5 #4).
        return {"update_in_progress": False, "owner_pid": None,
                "owner_create_time": None, "owner_token": None}
    if not os.path.isfile(path):
        return _CORRUPT
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return _CORRUPT
    if not isinstance(data, dict) or not isinstance(data.get("update_in_progress"), bool):
        return _CORRUPT
    if data["update_in_progress"]:
        # An in-progress record MUST carry a strictly-valid owner (GPT r3 #3).
        if not _valid_pid(data.get("owner_pid")) or not _valid_ct(data.get("owner_create_time")):
            return _CORRUPT
        # ...and a well-formed owner_token (GPT r4 #3): 32-hex uuid, else corrupt.
        tok = data.get("owner_token")
        if not isinstance(tok, str) or len(tok) != 32 or not all(c in "0123456789abcdef" for c in tok):
            return _CORRUPT
    return data


def _write_state(global_dir: str, state: dict) -> None:
    _atomic_write(_state_path(global_dir), state)


def _clear_state(global_dir: str) -> None:
    _write_state(global_dir, {"update_in_progress": False, "owner_pid": None,
                              "owner_create_time": None, "owner_token": None})


def _is_update_in_progress_locked(global_dir: str,
                                  is_alive: Callable[[int, float], bool] = lease_alive) -> bool:
    """Caller holds updater_lock. Corrupt state -> True (fail-closed)."""
    state = _read_state(global_dir)
    if state is _CORRUPT:
        return True
    if not state.get("update_in_progress"):
        return False
    if not is_alive(int(state["owner_pid"]), float(state["owner_create_time"])):
        _clear_state(global_dir)  # dead owner -> recover
        return False
    return True


def _is_own_lease(lease: dict, pid: int, create_time: float) -> bool:
    """True iff *lease* was written by the process identified by (pid,
    create_time) — same tolerance as lease_alive(), including its 0.0
    "unknown create_time" sentinel."""
    if lease.get("_corrupt") or lease.get("pid") != pid:
        return False
    ct = float(lease.get("create_time") or 0.0)
    return ct == 0.0 or create_time == 0.0 or abs(ct - create_time) < 1e-6


def other_live_leases(global_dir: str, pid: Optional[int] = None,
                      create_time: Optional[float] = None,
                      is_alive: Callable[[int, float], bool] = lease_alive) -> list[dict]:
    """Every live runtime lease except the caller's own.

    The updater runs inside a live account, so "any lease at all" was never a
    question it could ask: its own lease made the answer always yes, which is
    why try_begin_update() sat unused while its docstring in updater.py claimed
    it gated the install. What actually matters is whether *another* process
    still holds the exe and its DLLs open — that is what makes xcopy fail with
    a sharing violation and relaunch the old build, the "atualiza e não muda"
    loop reported with two accounts open."""
    pid, create_time = _resolve_identity(pid, create_time)
    with updater_lock(global_dir):
        return [l for l in _live_leases_locked(global_dir, is_alive)
                if not _is_own_lease(l, pid, create_time)]


def try_begin_update(global_dir: str, pid: Optional[int] = None,
                     create_time: Optional[float] = None,
                     is_alive: Callable[[int, float], bool] = lease_alive):
    """Atomically claim the update slot. Returns an owner-token dict on success,
    or None if a live updater owns it OR any OTHER account lease is live (the
    caller's own lease is expected — see other_live_leases()). The token
    carries a random owner_token so only THIS install run can end it
    (GPT r2 #2 / r3 #2). All check+write under one updater_lock."""
    pid, create_time = _resolve_identity(pid, create_time)
    with updater_lock(global_dir):
        if _is_update_in_progress_locked(global_dir, is_alive):
            return None
        if any(not _is_own_lease(l, pid, create_time)
               for l in _live_leases_locked(global_dir, is_alive)):
            return None
        token = {"owner_pid": pid, "owner_create_time": create_time,
                 "owner_token": uuid.uuid4().hex}
        _write_state(global_dir, {"update_in_progress": True, **token})
        return token


def end_update(global_dir: str, token: dict) -> bool:
    """Clear the update slot. ``token`` is REQUIRED (GPT r3 #1): only the owner
    that holds the matching owner_token may clear it, so a stale updater (even
    the same PID on a later run) can't wipe a newer one's state. Returns True if
    cleared."""
    if not isinstance(token, dict) or not token.get("owner_token"):
        raise ValueError("end_update requires the owner-token from try_begin_update")
    with updater_lock(global_dir):
        state = _read_state(global_dir)
        if state is _CORRUPT:
            state = {}
        if state.get("owner_token") != token["owner_token"]:
            return False
        _clear_state(global_dir)
        return True


def is_update_in_progress(global_dir: str,
                          is_alive: Callable[[int, float], bool] = lease_alive) -> bool:
    """True iff an update is in progress AND its owner is alive (crashed owner
    auto-recovered). Corrupt state file -> True (fail-closed)."""
    with updater_lock(global_dir):
        return _is_update_in_progress_locked(global_dir, is_alive)


# ── update-prompt claim (one dialog per machine, not one per account) ────────
#
# ``try_begin_update`` guards the INSTALL. Nothing guarded the PROMPT, and the
# two are different moments: every account runs its own UpdateChecker in its own
# process, so a release that is newer than the running build is found N times
# and asked about N times — two accounts open meant two "a new version is
# available" dialogs, on two windows, for one update. The install could only
# ever have happened once (the second one's ``try_begin_update`` refuses while
# the first holds a live runtime lease), so the extra dialogs were never even
# useful; they were purely a second thing to dismiss.
#
# So the prompt is claimed the same way the install is, and released when the
# dialog closes. Shape deliberately mirrors update_state.json — same lock, same
# atomic write, same (pid, create_time) liveness so a crashed holder is
# recovered rather than blocking every account forever.
_PROMPT_FILE = "update_prompt.json"


def _prompt_path(global_dir: str) -> str:
    return os.path.join(global_dir, _PROMPT_FILE)


def _read_prompt(global_dir: str):
    """Return the claim dict, or _CORRUPT if the file exists but is unreadable
    or fails validation.

    Fails OPEN where the state file fails closed, and the asymmetry is
    deliberate: an unreadable update_state must block an install, because
    installing twice corrupts the installation. An unreadable prompt claim must
    NOT block the prompt, because the worst case it guards against is a second
    dialog — while treating it as held would silently suppress update prompts
    on every account, forever, with nothing to show the user why.
    """
    path = _prompt_path(global_dir)
    if not os.path.lexists(path):
        return None
    if not os.path.isfile(path):
        return _CORRUPT
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return _CORRUPT
    if not isinstance(data, dict):
        return _CORRUPT
    if not _valid_pid(data.get("owner_pid")) or not _valid_ct(data.get("owner_create_time")):
        return _CORRUPT
    tok = data.get("owner_token")
    if not isinstance(tok, str) or len(tok) != 32 or not all(c in "0123456789abcdef" for c in tok):
        return _CORRUPT
    return data


def _prompt_holder_locked(global_dir: str,
                          is_alive: Callable[[int, float], bool]) -> "dict | None":
    """Caller holds updater_lock. Returns the live holder, or None. A dead or
    corrupt claim is cleared on the way past, so it blocks nobody twice."""
    claim = _read_prompt(global_dir)
    if claim is None:
        return None
    if claim is _CORRUPT or not is_alive(int(claim["owner_pid"]),
                                         float(claim["owner_create_time"])):
        try:
            os.remove(_prompt_path(global_dir))
        except OSError:
            pass
        return None
    return claim


def try_claim_update_prompt(global_dir: str, version: str,
                            pid: Optional[int] = None,
                            create_time: Optional[float] = None,
                            is_alive: Callable[[int, float], bool] = prompt_owner_alive):
    """Claim the right to ask the user about an update. Returns an owner-token
    dict, or None when another live process is already asking.

    Held regardless of which version the holder is asking about: while a dialog
    is on screen there is nothing useful a second one can add, and the blocked
    account re-checks on its own retry timer anyway. Re-entrant for the SAME
    process, so a checker that somehow asks twice replaces its own claim rather
    than deadlocking against itself.
    """
    pid, create_time = _resolve_identity(pid, create_time)
    with updater_lock(global_dir):
        holder = _prompt_holder_locked(global_dir, is_alive)
        if holder is not None and int(holder["owner_pid"]) != pid:
            return None
        token = {"owner_pid": pid, "owner_create_time": create_time,
                 "owner_token": uuid.uuid4().hex}
        _atomic_write(_prompt_path(global_dir),
                      {"version": str(version), "claimed_at": int(time.time()), **token})
        return token


def release_update_prompt(global_dir: str, token: dict) -> bool:
    """Release a claim from try_claim_update_prompt. Only the matching
    owner_token may release, so a checker that claims again after a decline
    cannot be cleared by its own earlier dialog closing late."""
    if not isinstance(token, dict) or not token.get("owner_token"):
        return False
    with updater_lock(global_dir):
        claim = _read_prompt(global_dir)
        if claim is _CORRUPT or claim is None:
            return False
        if claim.get("owner_token") != token["owner_token"]:
            return False
        try:
            os.remove(_prompt_path(global_dir))
        except OSError:
            return False
        return True


def update_prompt_holder(global_dir: str,
                         is_alive: Callable[[int, float], bool] = prompt_owner_alive) -> "dict | None":
    with updater_lock(global_dir):
        return _prompt_holder_locked(global_dir, is_alive)


# ── pure decision helpers (unit-tested) ──────────────────────────────────────
def should_block_start(update_state: dict) -> bool:
    return bool(update_state.get("update_in_progress"))


def should_block_update(live_leases: list) -> bool:
    return len(live_leases) > 0
