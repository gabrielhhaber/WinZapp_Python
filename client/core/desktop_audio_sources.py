"""Windows render-session discovery for live call sharing."""
from contextlib import ExitStack
import ctypes as C
from dataclasses import dataclass
import logging
import os

from .wasapi_capture import GUID, I32, P, U32, WasapiBackend, WasapiError
from .wasapi_process_capture import _PinnedProcess, _ProcessAPI

SESSION_MANAGER = GUID.parse("77aa99a0-1bd6-484f-8bc7-2c654c9a9b6f")
SESSION_CONTROL = GUID.parse("bfb7ff88-7239-4fc9-8fa2-07c950be9c6d")


def active_render_pids(backend, *, include_idle=False):
    """Enumerate process-owned active sessions on all active render endpoints."""
    def pointer(owner, method, types, *args):
        result = P()
        owner.call(method, [*types, C.POINTER(P)], *args, C.byref(result))
        obj = backend._pointer_factory(result)
        stack.callback(obj.release)
        return obj

    def number(owner, method, kind=I32):
        result = kind()
        status = owner.call(method, [C.POINTER(kind)], C.byref(result))
        return status, result.value

    pids = set()
    with ExitStack() as stack:
        devices = pointer(backend.enumerator, 3, [I32, U32], 0, 1)
        for index in range(number(devices, 3, U32)[1]):
            device = pointer(devices, 4, [U32], index)
            manager = pointer(device, 3, [C.POINTER(GUID), U32, P],
                              C.byref(SESSION_MANAGER), 23, None)
            sessions = pointer(manager, 5, [])
            for idx in range(number(sessions, 3)[1]):
                control = pointer(sessions, 4, [I32], idx)
                state = number(control, 3)[1]
                if state != 1 and not (include_idle and state == 0):
                    continue
                extended = pointer(control, 0, [C.POINTER(GUID)], C.byref(SESSION_CONTROL))
                status, pid = number(extended, 14, U32)
                if status == 0 and pid:
                    pids.add(pid)
    return pids


@dataclass(frozen=True)
class DesktopAudioApp:
    pid: int
    creation_time: int
    name: str


def list_audio_apps(backend):
    """Applications with a Windows audio session, including paused playback."""
    api = _ProcessAPI()
    pids = active_render_pids(backend, include_idle=True)
    rows = api.process_tree()
    names = {pid: name for pid, _, name in rows}
    roots = app_render_roots(rows, pids, os.getpid())
    choices = []
    for pid in roots:
        try:
            target = pin_process(api, pid)
        except WasapiError:
            continue
        try:
            if api.session(pid) == api.current_session():
                choices.append(DesktopAudioApp(pid, target.creation_time, names[pid]))
        finally:
            target.release()
    return sorted(choices, key=lambda app: (app.name.casefold(), app.pid))


def load_audio_apps(backend_factory=WasapiBackend):
    """list_audio_apps() with its own WASAPI backend, never raising.

    Call it from a worker thread: WasapiBackend asks COM for the
    multithreaded apartment, and wx's UI thread is already single-threaded
    (OleInitialize), so building it there fails with RPC_E_CHANGED_MODE and
    the picker would never list an application.
    """
    try:
        backend = backend_factory()
        try:
            return list_audio_apps(backend)
        finally:
            backend.close()
    except (OSError, RuntimeError):
        logging.exception("[call] desktop audio app list unavailable")
        return []


def app_render_roots(rows, pids, current_pid):
    """Find the application root for renderers such as Chromium helpers."""
    processes = {pid: (parent, name.casefold()) for pid, parent, name in rows}
    roots = set()
    for pid in shareable_processes(rows, pids, current_pid):
        visited = {pid}
        parent, name = processes[pid]
        while parent in processes and parent not in visited and processes[parent][1] == name:
            pid = parent
            visited.add(pid)
            parent = processes[pid][0]
        roots.add(pid)
    return shareable_processes(rows, roots, current_pid)


def pin_process(api, pid):
    handle = api.open(pid)
    try:
        target = _PinnedProcess(api, pid, handle, api.identity(handle))
        target.check()
        return target
    except BaseException:
        api.close(handle)
        raise


def shareable_processes(rows, pids, current_pid):
    """Choose disjoint roots, never capturing the call or an NVDA tree as system sound."""
    parents = {pid: parent for pid, parent, _ in rows}
    excluded = {pid for pid, _, name in rows if name.casefold() in {"nvda.exe", "winzapp.exe"}}
    excluded.add(current_pid)

    def ancestors(pid):
        seen = set()
        parent = parents.get(pid, 0)
        while parent and parent in parents and parent not in seen and parent != pid:
            seen.add(parent)
            parent = parents[parent]
        return seen

    forbidden = excluded | set().union(*(ancestors(pid) for pid in excluded))
    eligible = {pid for pid in pids if pid in parents and pid not in forbidden
                and not ancestors(pid) & excluded}
    return {pid for pid in eligible if not ancestors(pid) & eligible}
