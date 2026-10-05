"""A WPPConnect Server built next to the running one, then swapped in.

An update in place has to stop the server first: ApiSetupDialog wipes api/ and
rebuilds it there, so WinZapp is offline for the whole download, `npm install`
and build — minutes. With "download updates in the background" on
(main_window/wpp_background_update.py) the new server is built in a sibling
directory while the old one keeps running, and only the swap needs the server
stopped: two directory renames.

A tree built in one directory runs from another: nothing in dist/ or
node_modules/ records the absolute path it was built in (checked on a real
install, 920 MB of node_modules, before this was written), and the release
ZIP's own dist/ has always been built on CI and run somewhere else.

Plain functions over paths, no wx and no Node, so the swap is tested on
temporary directories.
"""

import logging
import os
import shutil
import stat
import time
import uuid

#: Per-install state that lives inside api/ and has to follow it. The same
#: names ApiSetupDialog keeps through an in-place reinstall (_KEEP_RUNTIME),
#: plus .env, which that flow preserves by never extracting over it.
CARRIED_OVER = ("tokens", "wppconnect_tokens", "userDataDir", "wppconnect.log", ".env")

#: node_modules (~0.9 GB) plus the browser (~0.4 GB) exist twice until the
#: swap, and the download and build need room of their own.
STAGING_MIN_FREE_BYTES = 3 * 1024 ** 3

_STAGING_SUFFIX = "_staging"
_OLD_SUFFIX = "_old"
_ASIDE_MARK = ".stale-"


class SwapError(Exception):
    """The staged server could not be put in place. api/ is as it was."""


def staging_dir_for(api_dir: str) -> str:
    """Where the new server is built: a sibling of api/, so the swap is a
    rename on the same volume."""
    return os.path.normpath(api_dir) + _STAGING_SUFFIX


def replaced_dir_for(api_dir: str) -> str:
    """Where the old server sits between the swap and its deletion."""
    return os.path.normpath(api_dir) + _OLD_SUFFIX


def has_room_for_staging(api_dir: str, minimum: int = STAGING_MIN_FREE_BYTES,
                         disk_usage=shutil.disk_usage) -> bool:
    """Whether the volume can hold a second server next to the first. When it
    cannot be told, the answer is no: the in-place update needs far less."""
    parent = os.path.dirname(os.path.normpath(api_dir)) or "."
    try:
        return disk_usage(parent).free >= minimum
    except Exception as exc:
        logging.warning("[api-staging] could not read the free space of %s: %s", parent, exc)
        return False


def leftovers_for(api_dir: str) -> list:
    """Every directory an interrupted build or swap can leave next to api/:
    the staging tree, the replaced server and any replaced server that had to
    be moved aside because it could not be deleted (see _clear_replaced)."""
    api_dir = os.path.normpath(api_dir)
    found = [staging_dir_for(api_dir), replaced_dir_for(api_dir)]
    parent, name = os.path.split(api_dir)
    try:
        entries = sorted(os.listdir(parent or "."))
    except OSError:
        entries = []
    prefix = name + _OLD_SUFFIX
    found.extend(os.path.join(parent, entry) for entry in entries
                 if entry.startswith(prefix + _ASIDE_MARK))
    return found


def _long_path(path: str) -> str:
    """The extended-length form on Windows: node_modules nests deeper than the
    260 characters a plain path may have, and rmtree stops at the first one."""
    path = os.path.abspath(path)
    if os.name != "nt" or path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def _unlock_and_retry(func, path, exc) -> None:
    """rmtree's onexc: Windows refuses to delete a read-only file, and npm
    leaves some in node_modules. Make it writable and try once more."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)
    except OSError:
        pass    # counted by discard() when the directory is still there


def discard(path: str) -> None:
    """Remove a staging or replaced directory. Never raises, but says when
    something stayed: a silent failure here is what made the swap find an old
    server it could not clear."""
    if not path or not os.path.exists(path):
        return
    try:
        shutil.rmtree(_long_path(path), onexc=_unlock_and_retry)
    except Exception as exc:
        logging.warning("[api-staging] could not delete %s: %s", path, exc)
    if os.path.exists(path):
        logging.warning("[api-staging] could not fully delete %s", path)


def _clear_replaced(old_dir: str) -> None:
    """Free the name the old server is about to be moved to. A leftover that
    cannot be deleted (permissions, a path too deep) is moved out of the way
    instead: the update then goes through and the leftover is swept by the
    next one, rather than the update failing every time. A directory with a
    file held open cannot be renamed either; that still raises SwapError."""
    discard(old_dir)
    if not os.path.exists(old_dir):
        return
    aside = f"{old_dir}{_ASIDE_MARK}{int(time.time())}-{uuid.uuid4().hex[:6]}"
    try:
        os.replace(old_dir, aside)
    except OSError as exc:
        raise SwapError(f"could not clear {old_dir}: {exc}") from exc
    logging.warning("[api-staging] %s could not be deleted; moved to %s", old_dir, aside)


def staged_server_is_built(staged_dir: str) -> bool:
    return os.path.isfile(os.path.join(staged_dir, "dist", "server.js"))


def _rename(source: str, target: str, attempts: int, pause: float) -> None:
    """os.replace with a few retries: right after Node and Chrome exit, an
    antivirus or the indexer can still hold a file for a moment."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(pause)


def swap_in_staged_api(api_dir: str, staged_dir: str, attempts: int = 5,
                       pause: float = 1.0) -> str:
    """Put *staged_dir* in place of *api_dir*; returns the directory holding
    the old server, for the caller to delete.

    The server must be stopped. On a failure api/ is the old server again
    and SwapError is raised: the outcome is "not updated", not "no server".
    The one exception is a double failure — the new tree could not move in
    AND the old one could not be moved back — which is logged with where the
    old server is (api_old); the caller's rollback is the net for that. The
    staged directory is left for the caller to discard.
    """
    api_dir = os.path.normpath(api_dir)
    if not staged_server_is_built(staged_dir):
        raise SwapError(f"no built server in {staged_dir}")
    old_dir = replaced_dir_for(api_dir)
    _clear_replaced(old_dir)        # left by a swap whose clean-up never ran

    had_old = os.path.isdir(api_dir)
    if had_old:
        try:
            _rename(api_dir, old_dir, attempts, pause)
        except OSError as exc:
            raise SwapError(f"could not move the current server aside: {exc}") from exc
    try:
        _rename(staged_dir, api_dir, attempts, pause)
    except OSError as exc:
        if had_old:
            try:
                _rename(old_dir, api_dir, attempts, pause)
            except OSError:
                logging.exception("[api-staging] could not put the old server back "
                                  "after a failed swap; it is in %s", old_dir)
        raise SwapError(f"could not move the new server into place: {exc}") from exc

    if had_old:
        for name in CARRIED_OVER:
            source = os.path.join(old_dir, name)
            target = os.path.join(api_dir, name)
            if not os.path.exists(source) or os.path.exists(target):
                continue
            try:
                shutil.move(source, target)
            except Exception as exc:
                # The session lives in the Chrome profile under data/, not
                # here; a token file that did not follow is regenerated.
                logging.warning("[api-staging] could not carry %s over: %s", name, exc)
    logging.info("[api-staging] new server swapped into %s", api_dir)
    return old_dir if had_old else ""
