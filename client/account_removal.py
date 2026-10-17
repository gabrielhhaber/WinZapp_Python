"""Removing an account: everything that belongs to it, and nothing that does not.

An account owns a data directory (messages, media, voice messages, settings,
its own key, logs) and, through its WPPConnect sessions, a profile of the
browser that carries the WhatsApp login plus that profile's restore points.
The data directory sits under ``accounts/<id>``; the profile and the restore
points sit in the directories SHARED by the whole install
(``global/api/userDataDir/<session>`` and ``global/api/userDataDirSnapshots/
<session>[.prev, .pending, ...]``), so they must be found by session name and
only the names of the account being removed may be touched.

A name still ``active`` or ``pairing`` in ANOTHER account's sessions.json is
never removed. If any sessions.json cannot be read the profile cleanup is
abandoned (fail-closed): the data directory is still removed, the profile is
left for a later start to retry (the account stays ``deleting``).

No wx here: the account manager and the shutdown path call it, and the tests
run it on a temporary directory.
"""

import json
import logging
import os
import shutil

import connection_state as cs

#: Directories, under global/api, that hold one entry per WPPConnect session
#: besides the browser profile: the credential store of a custom token folder.
_TOKEN_DIRS = ("tokens", "wppconnect_tokens")


def _sessions_file(account_dir: str) -> str:
    return os.path.join(account_dir, "sessions.json")


def session_names(account_dir: str) -> list:
    """Every well-formed session name the account has ever registered."""
    path = _sessions_file(account_dir)
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    names = []
    for entry in data.get("sessions", []):
        name = entry.get("name") if isinstance(entry, dict) else None
        if name and cs._valid_session_name(name) and name not in names:
            names.append(name)
    return names


def protected_names(accounts_root: str, excluding: str) -> set:
    """Session names that are active or pairing in any account but `excluding`.

    Raises when a sessions.json is unreadable: the caller must then delete
    nothing it cannot prove is its own."""
    protected = set()
    if not os.path.isdir(accounts_root):
        return protected
    for account_id in os.listdir(accounts_root):
        if account_id == excluding:
            continue
        path = _sessions_file(os.path.join(accounts_root, account_id))
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        for entry in data.get("sessions", []):
            if isinstance(entry, dict) and entry.get("status") in ("active", "pairing") \
                    and entry.get("name"):
                protected.add(entry["name"])
    return protected


def _remove_path(path: str) -> bool:
    """True when `path` no longer exists."""
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
    return not os.path.exists(path)


def _remove_all_but(directory: str, keep: str) -> None:
    if not os.path.isdir(directory):
        return
    for entry in os.listdir(directory):
        if entry != keep:
            _remove_path(os.path.join(directory, entry))


def remove_session_data(global_dir: str, names, protected=()) -> bool:
    """Delete the browser profile, the restore points and the token folders of
    each session in `names` (skipping `protected`). True when nothing is left."""
    from core import profile_recovery

    api_dir = os.path.join(global_dir, "api")
    profiles = os.path.join(api_dir, "userDataDir")
    clean = True
    for name in names:
        if name in protected:
            continue
        target = cs.safe_session_dir_to_delete(profiles, name)
        if target is None:
            logging.warning("[account removal] refusing an unsafe session name")
            clean = False
            continue
        if not _remove_path(target):
            logging.warning("[account removal] profile of session %s not fully removed",
                            name[:12])
            clean = False
        try:
            freed = profile_recovery.delete_snapshots(global_dir, name)
        except Exception:
            logging.exception("[account removal] restore points of %s", name[:12])
            freed = None
        if freed is None:
            clean = False
        for folder in _TOKEN_DIRS:
            root = os.path.join(api_dir, folder)
            for leaf in (name, f"{name}.data.json"):
                token_path = os.path.join(root, leaf)
                if os.path.exists(token_path) and not _remove_path(token_path):
                    clean = False
    return clean


def purge_account(registry, global_dir: str, account_id: str) -> bool:
    """Remove the account's data, its session profiles and restore points, then
    its registry entry. True when everything was removed.

    The account is marked ``deleting`` first (hidden, never started). If
    anything stays behind it stays ``deleting`` and the next start finishes the
    job (finish_pending_deletions); only a complete removal drops the entry.
    """
    import node_coord

    registry.set_state(account_id, "deleting")
    try:
        node_coord.release_node_lease(global_dir, account_id)
    except Exception:
        logging.exception("[account removal] releasing the node lease failed")
    data_dir = registry.data_dir_for(account_id)

    # The names must be read before the data directory goes: they live in it.
    names, protected, trusted = [], set(), True
    try:
        names = session_names(data_dir)
        protected = protected_names(os.path.dirname(data_dir), account_id)
    except Exception:
        logging.exception("[account removal] sessions unreadable - profile cleanup skipped")
        trusted = False

    complete = True
    if trusted:
        try:
            from coord_locks import LockTimeout, sessions_lock
            try:
                with sessions_lock(global_dir):
                    complete = remove_session_data(global_dir, names, protected)
            except LockTimeout:
                logging.info("[account removal] sessions lock busy - profile kept for retry")
                complete = False
        except Exception:
            logging.exception("[account removal] profile cleanup failed")
            complete = False
    else:
        complete = False

    if complete:
        if not _remove_path(data_dir):
            complete = False
    else:
        # The session names are what the retry needs to find the profile, and
        # they live in sessions.json: keep that one file, remove the rest.
        _remove_all_but(data_dir, "sessions.json")
    if complete:
        registry.remove(account_id)
    else:
        logging.warning("[account removal] account %s kept as 'deleting' to finish at the "
                        "next start", account_id[:8])
    return complete


def finish_pending_deletions(registry, global_dir: str) -> int:
    """Finish every account left ``deleting`` (a removal that was interrupted,
    or the one-and-only account removed while in use). Returns how many were
    completed."""
    done = 0
    for account in list(registry.list()):
        if account.get("state") != "deleting":
            continue
        try:
            if purge_account(registry, global_dir, account["id"]):
                done += 1
        except Exception:
            logging.exception("[account removal] finishing a pending removal failed")
    return done
