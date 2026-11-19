"""A portable WinZapp can start on a copy of the installed WinZapp's accounts.

Issue #824: someone testing a portable build had to link it as one more
device, and WhatsApp's linked-device slots ran out. On the first start of a
portable copy (no account of its own yet) WinZapp now looks for the installed
copy and, when it has paired accounts, asks whether to use them.

**The installation is only read.** Everything is copied into the portable's
own ``data`` folder; nothing in the installed folder is written, locked or
removed (not even the stale leases update_coord would tidy away). What is
copied, per account:

* ``data/accounts/<id>/`` (messages, settings, its own key and the encrypted
  WhatsApp token), without ``logs/``;
* each session the account uses (``sessions.json``, status ``active``): the
  WPPConnect browser profile ``data/global/api/userDataDir/<session>`` (its
  caches left behind), its token files, and the Neonize session
  ``data/global/neonize_bridge/data/<session>/``;
* ``data/global/app.json`` (the engine and the API key the token was minted
  with, the language).

Both copies are the same linked device. They must never run at the same
time, and the copy is only consistent when the installed WinZapp is closed
while it is taken (open databases and a live browser profile), so the copy
waits for it to close. The question says so.

The registry, the message boxes and the clock are parameters: the tests run
this on temporary folders, never on the machine's real installation.
"""

import json
import logging
import os
import shutil
import sys

from accounts import is_valid_account_id

UNINSTALL_KEY = "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\WinZapp"
INSTALLED_MARKER = "installed_files.dat"
#: Chrome's caches: rebuilt on demand, and the bulk of a profile.
_PROFILE_CACHES = ("Cache", "Code Cache", "GPUCache", "GrShaderCache",
                   "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache", "ShaderCache")
_TOKEN_DIRS = ("tokens", "wppconnect_tokens")

# Windows MessageBox flags and answers.
MB_YESNO = 0x04
MB_RETRYCANCEL = 0x05
MB_ICONQUESTION = 0x20
MB_ICONWARNING = 0x30
MB_ICONERROR = 0x10
IDYES = 6
IDRETRY = 4


def registry_install_locations() -> list:
    """``InstallLocation`` of the installer's uninstall entry, from the user's
    and the machine's hive (the installer writes whichever it could)."""
    try:
        import winreg
    except ImportError:
        return []
    found = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, UNINSTALL_KEY, 0, winreg.KEY_READ) as key:
                value = winreg.QueryValueEx(key, "InstallLocation")[0]
        except OSError:
            continue
        if isinstance(value, str) and value:
            found.append(value)
    return found


def candidate_install_dirs(registry_locations, local_app_data) -> list:
    """Where an installed WinZapp may be: the registry's answer, then the
    installer's default folder (%LOCALAPPDATA%\\WinZapp)."""
    candidates = list(registry_locations or [])
    if local_app_data:
        candidates.append(os.path.join(local_app_data, "WinZapp"))
    return candidates


def _same_dir(a, b) -> bool:
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except (OSError, ValueError):
        return False


def find_installed_dir(own_dir, candidates):
    """The first candidate that is an installed WinZapp with a registry of
    accounts, other than this very copy; None when there is none."""
    for folder in candidates:
        if not folder or _same_dir(folder, own_dir):
            continue
        if (os.path.isfile(os.path.join(folder, INSTALLED_MARKER))
                and os.path.isfile(os.path.join(folder, "data", "global", "accounts.json"))):
            return folder
    return None


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def read_registry(install_dir) -> dict:
    """The installed copy's accounts.json, read only; {} when unusable."""
    data = _read_json(os.path.join(install_dir, "data", "global", "accounts.json"))
    return data if isinstance(data, dict) and isinstance(data.get("accounts"), list) else {}


def paired_accounts(install_dir) -> list:
    """The installed copy's paired accounts that have a data folder, in the
    order of its account switcher."""
    accounts = []
    for account in read_registry(install_dir).get("accounts", []):
        if not isinstance(account, dict) or account.get("state") != "paired":
            continue
        account_id = account.get("id")
        if not is_valid_account_id(account_id):
            continue
        if not os.path.isdir(os.path.join(install_dir, "data", "accounts", account_id)):
            continue
        name = account.get("name") if isinstance(account.get("name"), str) else ""
        order = account.get("order") if isinstance(account.get("order"), int) else 0
        accounts.append({"id": account_id, "name": name, "order": order})
    return sorted(accounts, key=lambda a: a["order"])


def installed_running(install_dir, is_alive) -> bool:
    """Whether a WinZapp process of the installed copy is open: one of its
    runtime leases (update_coord.py) names a live process. Read only."""
    folder = os.path.join(install_dir, "data", "global", "runtime")
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    for name in names:
        if name.endswith(".tmp"):
            continue
        lease = _read_json(os.path.join(folder, name))
        if not isinstance(lease, dict):
            continue
        pid, created = lease.get("pid"), lease.get("create_time")
        if isinstance(pid, bool) or not isinstance(pid, int) or not isinstance(created, (int, float)):
            continue
        try:
            if is_alive(pid, float(created)):
                return True
        except Exception:
            return True   # cannot tell: wait rather than copy an open database
    return False


def active_session_names(account_dir) -> list:
    """The WhatsApp sessions the account uses now (sessions.json, ``active``)."""
    data = _read_json(os.path.join(account_dir, "sessions.json"))
    names = []
    for entry in (data or {}).get("sessions", []) if isinstance(data, dict) else []:
        name = entry.get("name") if isinstance(entry, dict) else None
        if (entry.get("status") == "active" and isinstance(name, str) and name
                and os.path.basename(name) == name and name not in (".", "..")
                and name not in names):
            names.append(name)
    return names


def _copy_tree(source, target, ignore=None) -> bool:
    if not os.path.isdir(source):
        return False
    shutil.copytree(source, target, ignore=ignore, dirs_exist_ok=False)
    return True


def copy_account(install_dir, data_root, account_id, written) -> None:
    """Copy one account into the portable's *data_root*, noting in *written*
    every path before it is written, so a failure halfway takes them back."""
    source = os.path.join(install_dir, "data")
    account_target = os.path.join(data_root, "accounts", account_id)
    account_source = os.path.join(source, "accounts", account_id)
    written.append(account_target)
    _copy_tree(account_source, account_target,
               ignore=lambda folder, names: ["logs"] if _same_dir(folder, account_source) else [])
    for name in active_session_names(account_source):
        profile_target = os.path.join(data_root, "global", "api", "userDataDir", name)
        written.append(profile_target)
        if not _copy_tree(os.path.join(source, "global", "api", "userDataDir", name), profile_target,
                          ignore=shutil.ignore_patterns(*_PROFILE_CACHES)):
            written.pop()
        for tokens in _TOKEN_DIRS:
            folder = os.path.join(source, "global", "api", tokens)
            if not os.path.isdir(folder):
                continue
            for entry in os.listdir(folder):
                if entry != name and not entry.startswith(name + "."):
                    continue
                target = os.path.join(data_root, "global", "api", tokens, entry)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                written.append(target)
                if os.path.isdir(os.path.join(folder, entry)):
                    shutil.copytree(os.path.join(folder, entry), target)
                else:
                    shutil.copy2(os.path.join(folder, entry), target)
        bridge_target = os.path.join(data_root, "global", "neonize_bridge", "data", name)
        written.append(bridge_target)
        if not _copy_tree(os.path.join(source, "global", "neonize_bridge", "data", name),
                          bridge_target):
            written.pop()


def _remove(paths) -> None:
    for path in reversed(paths):
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def copy_accounts(install_dir, data_root, accounts) -> None:
    """Copy *accounts* and the installed app.json; all or nothing. Refuses to
    overwrite anything the portable already has."""
    for account in accounts:
        if os.path.exists(os.path.join(data_root, "accounts", account["id"])):
            raise FileExistsError(account["id"])
    written = []
    try:
        for account in accounts:
            copy_account(install_dir, data_root, account["id"], written)
        app_source = os.path.join(install_dir, "data", "global", "app.json")
        if os.path.isfile(app_source):
            app_target = os.path.join(data_root, "global", "app.json")
            os.makedirs(os.path.dirname(app_target), exist_ok=True)
            shutil.copy2(app_source, app_target)
    except Exception:
        _remove(written)
        raise


def foreground_account(install_dir, accounts):
    """The account to open first: the installed copy's last one in front."""
    last = read_registry(install_dir).get("last_foreground")
    ids = [a["id"] for a in accounts]
    return last if last in ids else (ids[0] if ids else None)


def _message_box(text, flags) -> int:
    import ctypes
    return ctypes.windll.user32.MessageBoxW(0, text, "WinZapp", flags)


def is_portable_build(executable_dir=None, frozen=None) -> bool:
    """A compiled copy the installer did not put in place."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    folder = executable_dir or os.path.dirname(os.path.abspath(sys.argv[0]))
    return bool(frozen) and not os.path.isfile(os.path.join(folder, INSTALLED_MARKER))


def offer_installed_accounts(registry, data_root, i18n, *, own_dir, candidates,
                             is_alive, ask=_message_box):
    """First start of a portable copy: offer the installed copy's accounts.

    Returns the id of the account to open, or None to link a new one (no
    installation, no paired account, the user declined, or the copy failed).
    """
    install_dir = find_installed_dir(own_dir, candidates)
    if not install_dir:
        return None
    accounts = paired_accounts(install_dir)
    if not accounts:
        return None
    names = ", ".join(a["name"] or "WinZapp" for a in accounts)
    if ask(i18n.t("portable_use_installed_account").format(accounts=names),
           MB_YESNO | MB_ICONQUESTION) != IDYES:
        logging.info("[portable] the installed accounts were declined")
        return None
    while installed_running(install_dir, is_alive):
        if ask(i18n.t("portable_close_installed"), MB_RETRYCANCEL | MB_ICONWARNING) != IDRETRY:
            return None
    try:
        copy_accounts(install_dir, data_root, accounts)
        for account in accounts:
            registry.adopt(account["id"], account["name"])
        first = foreground_account(install_dir, accounts)
        registry.set_last_foreground(first)
    except Exception:
        logging.exception("[portable] copying the installed accounts failed")
        ask(i18n.t("portable_copy_installed_failed"), MB_ICONERROR)
        return None
    logging.info("[portable] %d account(s) copied from the installed WinZapp", len(accounts))
    return first


def start_portable_on_installed_accounts(registry, global_dir, i18n):
    """main.py's first run: the offer, for a portable build only. Never
    raises: anything unexpected means a new account is linked as before."""
    try:
        own_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        if not is_portable_build(own_dir):
            return None
        from update_coord import lease_alive
        return offer_installed_accounts(
            registry, os.path.dirname(os.path.abspath(global_dir)), i18n,
            own_dir=own_dir,
            candidates=candidate_install_dirs(registry_install_locations(),
                                              os.environ.get("LOCALAPPDATA", "")),
            is_alive=lease_alive)
    except Exception:
        logging.exception("[portable] the offer of the installed accounts failed")
        return None
