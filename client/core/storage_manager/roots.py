"""Partition known application locations; never search the user's whole disk."""
from __future__ import annotations

from dataclasses import replace
import os
import re

from .types import Root, canonical, inside


def build_roots(*, program, resources, data, account, include_other=False,
                external_models=(), compile_cache_owned=True, other_accounts=None):
    """Pure layout description. More-specific roots take precedence in scan().

    Other accounts are aggregated without reading the registry or publishing
    their names. They remain excluded unless the user explicitly opts in.
    """
    shared = os.path.join(data, "global")
    accounts = os.path.join(data, "accounts")
    roots = [Root("program", program, "program", "installation", exclude=(data,), required=True),
             Root("resources", resources, "program", "installation", exclude=(data,), required=True),
             Root("global", shared, "other", "shared"),
             Root("account", account, "other", "account", managed=True, required=True)]
    # The accounts container is a completeness scope, never an authorization
    # to act in sibling accounts. Only the current account has managed roots.
    if include_other:
        if other_accounts is None:
            roots.append(Root("other_accounts", accounts, "other", "other_accounts"))
        else:
            for index, path in enumerate(other_accounts):
                if canonical(os.path.dirname(path)) == canonical(accounts) and canonical(path) != canonical(account):
                    roots.append(Root("other_account_" + str(index), path, "other", "other_accounts", required=True))
    for name, category in (("media", "media"), ("voice_messages", "voice"),
                           ("logs", "logs"), ("storage_quarantine", "quarantine")):
        roots.append(Root("account_" + name, os.path.join(account, name), category,
                          "account", managed=category in ("media", "voice", "quarantine")))
    for name in ("whisper_models", "noise_models", "cuda_runtime", "whisper_cpp_runtime"):
        roots.append(Root("global_" + name, os.path.join(shared, name), "models", "shared"))
    for name, category in (("api/userDataDir", "sessions"),
                           ("api/userDataDirSnapshots", "backups"),
                           ("node-compile-cache", "compile_cache")):
        roots.append(Root("global_" + category, os.path.join(shared, *name.split("/")),
                          category, "shared", managed=category == "compile_cache" and compile_cache_owned))
    for index, path in enumerate(external_models):
        if path and not inside(path, data) and not inside(path, program) and not inside(path, resources):
            roots.append(Root("external_model_" + str(index), path, "models", "external", file_only=True))
    # Parent data metadata (including legacy leftovers) is counted separately.
    # An unselected sibling account is excluded rather than silently scanned.
    roots.append(Root("data", data, "other", "installation",
                      exclude=() if canonical(account) == canonical(data) else (accounts,)))
    # A configured external model can overlap another external model root.
    # scan() partitions it and counts a shared inode once.
    return roots


def classify(entry):
    """Classify protected database/log files without opening their contents."""
    name = os.path.basename(entry.relative).casefold()
    if entry.category == "other":
        if name.endswith((".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3")):
            return replace(entry, category="database", reason="critical")
        if name.endswith((".log", ".log.1", ".log.2")):
            return replace(entry, category="logs")
    if entry.category in ("sessions", "database", "program", "models", "backups"):
        return replace(entry, reason="critical")
    if entry.category == "compile_cache" and entry.root.managed:
        # Node v22's version tag and CRC32 filename layout, not a generic
        # "everything under a folder called cache" deletion entitlement.
        # https://github.com/nodejs/node/blob/v22.22.2/src/compile_cache.cc
        parts = entry.relative.replace("\\", "/").split("/")
        if (len(parts) == 2 and re.fullmatch(r"v\d+\.\d+\.\d+-[a-z0-9]+-[0-9a-f]{8}(?:-\d+)?", parts[0])
                and re.fullmatch(r"[0-9a-f]{8}", parts[1])):
            return replace(entry, reason="reproducible")
        return replace(entry, reason="protected")
    return entry


def current_roots(main_window, preferences):
    """Resolve paths only when the user/saved preference requests a scan."""
    import app_paths

    shared = app_paths.global_dir()
    data = os.path.dirname(shared)
    account = app_paths.data_path()
    external = []
    app = getattr(main_window, "_app_settings", None)
    if preferences.include_external_models and app is not None:
        from .model_roots import external_model_files
        external = external_model_files(app)
    cache = os.environ.get("NODE_COMPILE_CACHE")
    owned = not cache or canonical(cache) == canonical(os.path.join(shared, "node-compile-cache"))
    roots = build_roots(program=app_paths._outer_exe_dir(), resources=app_paths.resource_path(),
                       data=data, account=account,
                       include_other=preferences.include_other_accounts,
                       other_accounts=preferences.other_accounts,
                       external_models=external, compile_cache_owned=owned)
    # Ask the bridge's owner for its locations, never for its secret/session.
    from core import neonize_bridge_process as bridge_paths
    bridge = bridge_paths.bridge_dir()
    roots.extend([Root("bridge_program", bridge, "program", "installation"),
                  Root("bridge_data", bridge_paths.data_dir(bridge), "sessions", "shared"),
                  Root("bridge_run", bridge_paths.run_dir(bridge), "sessions", "shared")])
    roots.append(Root("bridge_log", os.path.join(bridge_paths.run_dir(bridge), "bridge.log"),
                      "logs", "shared", file_only=True))
    return roots


def capture_other_accounts():
    import app_paths
    from .types import unsafe
    folder = app_paths.accounts_root()
    from .scan import _safe_ancestors
    if not _safe_ancestors(folder):
        raise OSError("unsafe account container")
    try:
        with os.scandir(folder) as children:
            result = []
            for child in children:
                if child.is_dir(follow_symlinks=False) and not unsafe(child.stat(follow_symlinks=False)):
                    result.append(child.path)
                    if len(result) > 100:
                        raise ValueError("too many account folders")
            return result
    except FileNotFoundError:
        return []
