"""Tests for client/account_bootstrap.py — startup resolution (pure logic)."""

import pytest

import account_bootstrap as boot


class FakeRegistry:
    """Minimal stand-in for AccountRegistry (pure-logic bootstrap test)."""

    def __init__(self, accounts, last_fg=None, recovery=False):
        self._accounts = accounts
        self._last_fg = last_fg
        self._recovery = recovery

    def is_recovery_mode(self):
        return self._recovery

    def list(self):
        return list(self._accounts)

    def get(self, aid):
        return next((a for a in self._accounts if a["id"] == aid), None)

    def last_foreground(self):
        return self._last_fg

    def list_paired(self):
        return sorted([a for a in self._accounts if a["state"] == "paired"],
                      key=lambda a: a["order"])


def _acc(aid, state="paired", order=1):
    return {"id": aid, "state": state, "order": order, "name": aid, "autostart": False}


A = "a" * 32
B = "b" * 32


def test_explicit_account_ok():
    reg = FakeRegistry([_acc(A)])
    r = boot.resolve_startup(["--account", A], reg)
    assert r["mode"] == "account" and r["account_id"] == A


def test_explicit_account_unknown_is_error():
    reg = FakeRegistry([_acc(A)])
    r = boot.resolve_startup(["--account", B], reg)
    assert r["mode"] == "error"
    # main.py names the account in the (translated) startup_invalid_account box.
    assert r["account_id"] == B


def test_explicit_account_archived_is_error():
    reg = FakeRegistry([_acc(A, state="archived")])
    r = boot.resolve_startup(["--account", A], reg)
    assert r["mode"] == "error"


def test_explicit_account_deleting_is_error():
    reg = FakeRegistry([_acc(A, state="deleting")])
    r = boot.resolve_startup(["--account", A], reg)
    assert r["mode"] == "error"


def test_autostart_boot_plan():
    reg = FakeRegistry([_acc(A, order=1), _acc(B, order=2)])
    reg._accounts[0]["autostart"] = True
    reg._accounts[1]["autostart"] = True
    r = boot.resolve_startup(["--autostart-boot"], reg)
    assert r["mode"] == "autostart_boot"
    assert r["foreground"] == A  # lowest order
    assert r["background"] == [B]


def test_plain_start_uses_last_foreground():
    reg = FakeRegistry([_acc(A, order=1), _acc(B, order=2)], last_fg=B)
    r = boot.resolve_startup([], reg)
    assert r["mode"] == "account" and r["account_id"] == B


def test_plain_start_lowest_order_when_no_last_fg():
    reg = FakeRegistry([_acc(B, order=2), _acc(A, order=1)])
    r = boot.resolve_startup([], reg)
    assert r["account_id"] == A


def test_resume_existing_pending():
    reg = FakeRegistry([_acc(A, state="pending")])
    r = boot.resolve_startup([], reg)
    assert r["mode"] == "account" and r["account_id"] == A and r["resume_pending"] is True


def test_empty_registry_first_run():
    reg = FakeRegistry([])
    r = boot.resolve_startup([], reg)
    assert r["mode"] == "first_run"


def test_only_archived_opens_manager():
    reg = FakeRegistry([_acc(A, state="archived")])
    r = boot.resolve_startup([], reg)
    assert r["mode"] == "manager"


def test_recovery_opens_manager():
    reg = FakeRegistry([], recovery=True)
    r = boot.resolve_startup([], reg)
    assert r["mode"] == "manager"


def test_startup_source_parsed():
    reg = FakeRegistry([_acc(A)], last_fg=A)
    assert boot.parse_startup_source(["--startup-source", "autostart"]) == "autostart"
    assert boot.parse_startup_source([]) == "user"
