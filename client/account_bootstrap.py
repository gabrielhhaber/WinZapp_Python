"""
Startup account resolution for WinZapp multi-account (client/account_bootstrap.py)
==================================================================================

Pure logic that decides which account (if any) this process should run, given
argv and the registry. Kept wx-free and side-effect-free so it is fully unit
tested; the actual wiring (set_active_account, mutex, AppUserModelID, wx.App)
lives in main.py's __main__ and calls resolve_startup() first.

Return shape: a dict with ``mode`` and mode-specific fields:
  * {"mode": "account", "account_id", "resume_pending": bool}
      -> run this account normally. resume_pending=True means it's a pending
         account we're resuming (finish pairing), so last_foreground must NOT
         be set until pairing succeeds (plan Zad 3.2 / GPT r7 #1).
  * {"mode": "autostart_boot", "foreground", "background": [...]}
      -> boot launcher: open foreground (source=autostart), spawn the rest with
         --background; none touch last_foreground (plan 1b / GPT r5 #2).
  * {"mode": "first_run"}   -> empty registry: create default + pair.
  * {"mode": "manager"}     -> only archived accounts, or registry in recovery:
         open the global manager (no account, no Node) (GPT r4 #6 / r5 #6).
  * {"mode": "error", "reason"} -> bad --account: hard error, never silently
         fall through to another account (GPT r2 #2).
"""

from __future__ import annotations


def _arg_value(argv, flag):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def parse_startup_source(argv) -> str:
    """--startup-source user|autostart (default 'user'). Only 'user' may later
    move last_foreground (GPT r5 #2)."""
    val = _arg_value(argv, "--startup-source")
    return "autostart" if val == "autostart" else "user"


def resolve_startup(argv, registry) -> dict:
    # Registry corruption -> manager mode; never first-run/mutate (GPT r5 #6).
    if registry.is_recovery_mode():
        return {"mode": "manager", "reason": "registry_recovery"}

    explicit = _arg_value(argv, "--account")
    if explicit is not None:
        acc = registry.get(explicit)
        if acc is None or acc["state"] in ("archived", "deleting"):
            return {"mode": "error", "reason": f"account {explicit} not runnable",
                    "account_id": explicit}
        return {"mode": "account", "account_id": explicit,
                "resume_pending": acc["state"] == "pending"}

    if "--autostart-boot" in argv:
        autostart = [a for a in registry.list_paired() if a.get("autostart")]
        if not autostart:
            # nothing marked autostart -> fall back to plain start
            return _plain_start(registry)
        return {"mode": "autostart_boot",
                "foreground": autostart[0]["id"],
                "background": [a["id"] for a in autostart[1:]]}

    return _plain_start(registry)


def _plain_start(registry) -> dict:
    paired = registry.list_paired()
    if paired:
        lf = registry.last_foreground()
        if lf and any(a["id"] == lf for a in paired):
            return {"mode": "account", "account_id": lf, "resume_pending": False}
        return {"mode": "account", "account_id": paired[0]["id"], "resume_pending": False}

    all_accounts = registry.list()
    if not all_accounts:
        return {"mode": "first_run"}

    # No paired accounts. Resume an existing pending one rather than creating a
    # duplicate (GPT r3 #9); if only archived remain, open the manager.
    pending = [a for a in all_accounts if a["state"] == "pending"]
    if pending:
        return {"mode": "account", "account_id": pending[0]["id"],
                "resume_pending": True}
    return {"mode": "manager", "reason": "only_archived"}
