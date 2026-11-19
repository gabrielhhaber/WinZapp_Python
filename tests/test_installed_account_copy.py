"""A portable copy starting on the installed copy's accounts (issue #824).
Everything runs on temporary folders; the machine's own installation and
registry are never read."""

import json
import os

import pytest

import installed_account_copy as copy
from accounts import AccountRegistry

ACCOUNT = "a" * 32
OTHER = "b" * 32
SESSION = "c" * 32


def make_install(root, *, accounts=None, running_pid=None):
    """An installed WinZapp with one paired account on both engines."""
    data = root / "data"
    (root).mkdir(parents=True)
    (root / copy.INSTALLED_MARKER).write_text("x")
    gd = data / "global"
    gd.mkdir(parents=True)
    accounts = accounts if accounts is not None else [
        {"id": ACCOUNT, "name": "Trabalho", "state": "paired", "autostart": False, "order": 1},
        {"id": OTHER, "name": "Velha", "state": "archived", "autostart": False, "order": 2},
    ]
    (gd / "accounts.json").write_text(json.dumps(
        {"schema": 1, "last_foreground": ACCOUNT, "accounts": accounts}))
    (gd / "app.json").write_text(json.dumps({"wpp_engine": "neonize"}))
    acc = data / "accounts" / ACCOUNT
    (acc / "logs").mkdir(parents=True)
    (acc / "logs" / "log.log").write_text("big")
    (acc / "messages.db").write_text("db")
    (acc / "sessions.json").write_text(json.dumps({"sessions": [
        {"name": SESSION, "status": "active"}, {"name": "d" * 32, "status": "abandoned"}]}))
    profile = gd / "api" / "userDataDir" / SESSION
    (profile / "Default" / "IndexedDB").mkdir(parents=True)
    (profile / "Default" / "IndexedDB" / "keys").write_text("k")
    (profile / "Cache").mkdir()
    (profile / "Cache" / "blob").write_text("cache")
    (gd / "api" / "tokens").mkdir(parents=True)
    (gd / "api" / "tokens" / f"{SESSION}.data.json").write_text("{}")
    (gd / "api" / "tokens" / "other.data.json").write_text("{}")
    (gd / "neonize_bridge" / "data" / SESSION).mkdir(parents=True)
    (gd / "neonize_bridge" / "data" / SESSION / "session.db").write_text("keys")
    if running_pid is not None:
        (gd / "runtime").mkdir()
        (gd / "runtime" / "lease").write_text(json.dumps({"pid": running_pid, "create_time": 1.0}))
    return root


def snapshot(folder):
    return sorted((str(p.relative_to(folder)), p.read_bytes() if p.is_file() else b"")
                  for p in folder.rglob("*"))


class _I18n:
    def t(self, key):
        return key + " {accounts}"


def test_finds_the_installation_but_never_itself(tmp_path):
    install = make_install(tmp_path / "inst")
    assert copy.find_installed_dir(str(tmp_path / "portable"), [str(install)]) == str(install)
    assert copy.find_installed_dir(str(install), [str(install)]) is None
    assert copy.find_installed_dir(str(tmp_path), [str(tmp_path / "nothing"), ""]) is None
    assert copy.candidate_install_dirs(["X"], "L") == ["X", os.path.join("L", "WinZapp")]


def test_only_paired_accounts_are_offered(tmp_path):
    install = make_install(tmp_path / "inst")
    assert [a["id"] for a in copy.paired_accounts(str(install))] == [ACCOUNT]


def test_running_is_read_from_the_leases(tmp_path):
    install = make_install(tmp_path / "inst", running_pid=42)
    assert copy.installed_running(str(install), lambda pid, ct: pid == 42)
    assert not copy.installed_running(str(install), lambda pid, ct: False)
    assert not copy.installed_running(str(make_install(tmp_path / "idle")), lambda *a: True)


def test_copy_takes_the_account_and_its_sessions_and_leaves_the_install_alone(tmp_path):
    install = make_install(tmp_path / "inst")
    before = snapshot(install)
    portable = tmp_path / "portable" / "data"
    registry = AccountRegistry(str(portable / "global"))
    answers = []

    def ask(text, flags):
        answers.append(flags)
        return copy.IDYES

    first = copy.offer_installed_accounts(
        registry, str(portable), _I18n(), own_dir=str(tmp_path / "portable"),
        candidates=[str(install)], is_alive=lambda *a: False, ask=ask)
    assert first == ACCOUNT
    assert snapshot(install) == before
    acc = portable / "accounts" / ACCOUNT
    assert (acc / "messages.db").read_text() == "db"
    assert not (acc / "logs").exists()
    profile = portable / "global" / "api" / "userDataDir" / SESSION
    assert (profile / "Default" / "IndexedDB" / "keys").exists()
    assert not (profile / "Cache").exists()
    assert not (portable / "global" / "api" / "userDataDir" / ("d" * 32)).exists()
    assert (portable / "global" / "api" / "tokens" / f"{SESSION}.data.json").exists()
    assert not (portable / "global" / "api" / "tokens" / "other.data.json").exists()
    assert (portable / "global" / "neonize_bridge" / "data" / SESSION / "session.db").exists()
    assert json.loads((portable / "global" / "app.json").read_text())["wpp_engine"] == "neonize"
    entry = registry.get(ACCOUNT)
    assert entry["state"] == "paired" and entry["name"] == "Trabalho"
    assert registry.last_foreground() == ACCOUNT


def test_declining_copies_nothing(tmp_path):
    install = make_install(tmp_path / "inst")
    portable = tmp_path / "portable" / "data"
    registry = AccountRegistry(str(portable / "global"))
    assert copy.offer_installed_accounts(
        registry, str(portable), _I18n(), own_dir=str(tmp_path / "portable"),
        candidates=[str(install)], is_alive=lambda *a: False,
        ask=lambda text, flags: 7) is None
    assert registry.list() == []
    assert not (portable / "accounts").exists()


def test_waits_for_the_installed_copy_to_close_or_gives_up(tmp_path):
    install = make_install(tmp_path / "inst", running_pid=42)
    portable = tmp_path / "portable" / "data"
    registry = AccountRegistry(str(portable / "global"))
    asked = []

    def ask(text, flags):
        asked.append(flags)
        return copy.IDYES if flags & copy.MB_YESNO == copy.MB_YESNO and len(asked) == 1 else 2

    assert copy.offer_installed_accounts(
        registry, str(portable), _I18n(), own_dir=str(tmp_path / "portable"),
        candidates=[str(install)], is_alive=lambda *a: True, ask=ask) is None
    assert asked[1] & copy.MB_RETRYCANCEL == copy.MB_RETRYCANCEL
    assert registry.list() == []


def test_a_failed_copy_leaves_nothing_behind(tmp_path, monkeypatch):
    install = make_install(tmp_path / "inst")
    portable = tmp_path / "portable" / "data"

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(copy.shutil, "copy2", broken)
    with pytest.raises(OSError):
        copy.copy_accounts(str(install), str(portable), copy.paired_accounts(str(install)))
    assert not (portable / "accounts" / ACCOUNT).exists()
    assert not (portable / "global" / "api" / "userDataDir" / SESSION).exists()


def test_only_a_compiled_copy_without_the_installer_marker_is_portable(tmp_path):
    assert copy.is_portable_build(str(tmp_path), frozen=True)
    assert not copy.is_portable_build(str(tmp_path), frozen=False)
    (tmp_path / copy.INSTALLED_MARKER).write_text("x")
    assert not copy.is_portable_build(str(tmp_path), frozen=True)


def test_adopt_keeps_the_id_and_refuses_a_duplicate(tmp_path):
    registry = AccountRegistry(str(tmp_path / "global"))
    registry.add("default", state="pending")
    acc = registry.adopt(ACCOUNT, "Trabalho")
    assert acc["id"] == ACCOUNT and acc["order"] == 2 and acc["state"] == "paired"
    with pytest.raises(ValueError):
        registry.adopt(ACCOUNT, "again")
    with pytest.raises(ValueError):
        registry.adopt("../escape", "x")
