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
import time

#: Per-install state that lives inside api/ and has to follow it. The same
#: names ApiSetupDialog keeps through an in-place reinstall (_KEEP_RUNTIME),
#: plus .env, which that flow preserves by never extracting over it.
CARRIED_OVER = ("tokens", "wppconnect_tokens", "userDataDir", "wppconnect.log", ".env")

#: node_modules (~0.9 GB) plus the browser (~0.4 GB) exist twice until the
#: swap, and the download and build need room of their own.
STAGING_MIN_FREE_BYTES = 3 * 1024 ** 3

_STAGING_SUFFIX = "_staging"
_OLD_SUFFIX = "_old"


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


def discard(path: str) -> None:
    """Remove a staging or replaced directory. Never raises."""
    if path and os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)


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
    discard(old_dir)        # left by a swap whose clean-up never ran
    if os.path.exists(old_dir):
        raise SwapError(f"could not clear {old_dir}")

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
