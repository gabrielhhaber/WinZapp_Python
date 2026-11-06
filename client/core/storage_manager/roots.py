"""Partition known application locations; never search the user's whole disk."""
from __future__ import annotations

from dataclasses import replace
import os

from .types import Root, canonical, inside


def build_roots(*, program, resources, data, account, include_other=False,
                external_models=(), compile_cache_owned=True):
    """Pure layout description. More-specific roots take precedence in scan().

    Other accounts are aggregated without reading the registry or publishing
    their names. They remain excluded unless the user explicitly opts in.
    """
    shared = os.path.join(data, "global")
    accounts = os.path.join(data, "accounts")
    roots = [Root("program", program, "program", "installation", exclude=(data,)),
             Root("resources", resources, "program", "installation", exclude=(data,)),
             Root("global", shared, "other", "shared"),
             Root("account", account, "other", "account", managed=True)]
    # The accounts container is a completeness scope, never an authorization
    # to act in sibling accounts. Only the current account has managed roots.
    if include_other:
        roots.append(Root("other_accounts", accounts, "other", "other_accounts"))
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
            roots.append(Root("external_model_" + str(index), path, "models", "external"))
    # Parent data metadata (including legacy leftovers) is counted separately.
    # An unselected sibling account is excluded rather than silently scanned.
    roots.append(Root("data", data, "other", "installation",
                      exclude=() if include_other or canonical(account) == canonical(data) else (accounts,)))
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
        return replace(entry, reason="reproducible")
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
        for key in ("transcription_models_dir", "transcription_cuda_extra_dir",
                    "transcription_whisper_cpp_extra_dir"):
            value = app.get(key)
            if isinstance(value, str) and value:
                external.append(value)
    cache = os.environ.get("NODE_COMPILE_CACHE")
    owned = not cache or canonical(cache) == canonical(os.path.join(shared, "node-compile-cache"))
    return build_roots(program=app_paths._outer_exe_dir(), resources=app_paths.resource_path(),
                       data=data, account=account,
                       include_other=preferences.include_other_accounts,
                       external_models=external, compile_cache_owned=owned)
