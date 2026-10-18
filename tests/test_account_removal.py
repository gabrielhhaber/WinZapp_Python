"""Removing an account takes everything it owns - and nothing another account owns.

The data directory, the browser profile of its sessions and that profile's
restore points (kept apart, in directories shared by the whole install), all
gone; a session another account still uses is left alone; whatever cannot be
proved safe or cannot be removed keeps the account 'deleting' for the next start.
No window is opened.
"""

import json
import os

import pytest

import account_removal
from account_ui import can_hard_delete, is_only_account
from accounts import AccountRegistry

SESSION_A = "a" * 32
SESSION_A2 = "b" * 32
SESSION_B = "c" * 32


@pytest.fixture
def install(tmp_path):
    global_dir = tmp_path / "global"
    registry = AccountRegistry(str(global_dir))
    return registry, str(global_dir), tmp_path


def _write_sessions(registry, account_id, sessions):
    data_dir = registry.data_dir_for(account_id)
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "sessions.json"), "w", encoding="utf-8") as handle:
        json.dump({"sessions": sessions}, handle)
    with open(os.path.join(data_dir, "messages.db"), "wb") as handle:
        handle.write(b"x")
    os.makedirs(os.path.join(data_dir, "media"), exist_ok=True)


def _make_profile(global_dir, name):
    """The profile and every generation of restore point of one session."""
    api = os.path.join(global_dir, "api")
    paths = [os.path.join(api, "userDataDir", name)]
    for suffix in ("", ".prev", ".pending"):
        paths.append(os.path.join(api, "userDataDirSnapshots", name + suffix))
    for path in paths:
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "Cookies"), "wb") as handle:
            handle.write(b"profile")
    return paths


def test_everything_the_account_owns_is_removed(install):
    registry, global_dir, _tmp = install
    account = registry.add("one", state="paired")
    _write_sessions(registry, account["id"], [
        {"name": SESSION_A, "status": "active"},
        {"name": SESSION_A2, "status": "abandoned"},
    ])
    owned = _make_profile(global_dir, SESSION_A) + _make_profile(global_dir, SESSION_A2)

    assert account_removal.purge_account(registry, global_dir, account["id"]) is True

    assert not os.path.exists(registry.data_dir_for(account["id"]))
    assert not any(os.path.exists(path) for path in owned)
    assert registry.get(account["id"]) is None


def test_another_accounts_profile_and_data_are_left_alone(install):
    registry, global_dir, _tmp = install
    gone = registry.add("gone", state="paired")
    kept = registry.add("kept", state="paired")
    _write_sessions(registry, gone["id"], [{"name": SESSION_A, "status": "active"}])
    _write_sessions(registry, kept["id"], [{"name": SESSION_B, "status": "active"}])
    removed = _make_profile(global_dir, SESSION_A)
    survivors = _make_profile(global_dir, SESSION_B)

    assert account_removal.purge_account(registry, global_dir, gone["id"]) is True

    assert not any(os.path.exists(path) for path in removed)
    assert all(os.path.exists(path) for path in survivors)
    assert os.path.exists(registry.data_dir_for(kept["id"]))
    assert registry.get(kept["id"]) is not None


def test_a_session_another_account_still_uses_is_not_removed(install):
    registry, global_dir, _tmp = install
    gone = registry.add("gone", state="paired")
    other = registry.add("other", state="paired")
    shared = {"name": SESSION_A, "status": "active"}
    _write_sessions(registry, gone["id"], [shared])
    _write_sessions(registry, other["id"], [shared])
    profile = _make_profile(global_dir, SESSION_A)

    account_removal.purge_account(registry, global_dir, gone["id"])

    assert all(os.path.exists(path) for path in profile)


def test_an_unreadable_sessions_file_keeps_the_account_for_the_next_start(install):
    registry, global_dir, _tmp = install
    gone = registry.add("gone", state="paired")
    other = registry.add("other", state="paired")
    _write_sessions(registry, gone["id"], [{"name": SESSION_A, "status": "active"}])
    other_dir = registry.data_dir_for(other["id"])
    os.makedirs(other_dir, exist_ok=True)
    with open(os.path.join(other_dir, "sessions.json"), "w", encoding="utf-8") as handle:
        handle.write("{not json")
    profile = _make_profile(global_dir, SESSION_A)

    assert account_removal.purge_account(registry, global_dir, gone["id"]) is False

    # Nothing it could not prove was its own was touched, and it is retried.
    assert all(os.path.exists(path) for path in profile)
    assert registry.get(gone["id"])["state"] == "deleting"

    os.remove(os.path.join(other_dir, "sessions.json"))
    assert account_removal.finish_pending_deletions(registry, global_dir) == 1
    assert registry.get(gone["id"]) is None
    assert not any(os.path.exists(path) for path in profile)


def test_an_unsafe_session_name_is_never_turned_into_a_path(install):
    registry, global_dir, tmp_path = install
    account = registry.add("one", state="paired")
    outside = tmp_path / "precious"
    outside.mkdir()
    _write_sessions(registry, account["id"], [{"name": "../precious", "status": "active"}])

    account_removal.purge_account(registry, global_dir, account["id"])

    assert outside.exists()


def test_session_names_lists_only_well_formed_names(install):
    registry, _global_dir, _tmp = install
    account = registry.add("one", state="paired")
    _write_sessions(registry, account["id"], [
        {"name": SESSION_A}, {"name": SESSION_A}, {"name": "../x"}, {"status": "active"},
    ])
    assert account_removal.session_names(registry.data_dir_for(account["id"])) == [SESSION_A]


def test_finishing_pending_deletions_ignores_live_accounts(install):
    registry, global_dir, _tmp = install
    live = registry.add("live", state="paired")
    _write_sessions(registry, live["id"], [{"name": SESSION_B, "status": "active"}])
    profile = _make_profile(global_dir, SESSION_B)

    assert account_removal.finish_pending_deletions(registry, global_dir) == 0

    assert registry.get(live["id"]) is not None
    assert all(os.path.exists(path) for path in profile)


class TestTheAccountInUse:
    def test_it_can_be_removed_only_when_it_is_the_only_account(self):
        account = {"id": "a" * 32, "state": "paired"}
        other = {"id": "b" * 32, "state": "paired"}
        assert is_only_account([account], account["id"]) is True
        assert is_only_account([account, other], account["id"]) is False
        # an account already being removed does not count
        assert is_only_account([account, {**other, "state": "deleting"}], account["id"]) is True

        assert can_hard_delete(account, account["id"])[0] is False
        assert can_hard_delete(account, account["id"], only_account=True)[0] is True
        assert can_hard_delete(other, account["id"])[0] is True


class TestPurgeOnExit:
    """WindowLifecycleMixin._purge_account_if_requested, on a plain stub."""

    @staticmethod
    def _call(stub):
        pytest.importorskip("wx")
        from main_window.window_lifecycle import WindowLifecycleMixin
        WindowLifecycleMixin._purge_account_if_requested(stub)

    def test_the_account_marked_for_removal_is_purged(self, install):
        registry, global_dir, _tmp = install
        account = registry.add("one", state="paired")
        _write_sessions(registry, account["id"], [{"name": SESSION_A, "status": "active"}])
        profile = _make_profile(global_dir, SESSION_A)
        stub = type("Stub", (), {})()
        stub.registry, stub.global_dir = registry, global_dir
        stub._purge_account_on_exit = account["id"]

        self._call(stub)

        assert registry.get(account["id"]) is None
        assert not any(os.path.exists(path) for path in profile)

    def test_nothing_is_removed_when_no_removal_was_requested(self, install):
        registry, global_dir, _tmp = install
        account = registry.add("one", state="paired")
        stub = type("Stub", (), {})()
        stub.registry, stub.global_dir = registry, global_dir

        self._call(stub)

        assert registry.get(account["id"]) is not None

    def test_a_failure_does_not_stop_the_shutdown(self):
        stub = type("Stub", (), {})()
        stub.registry, stub.global_dir = None, None
        stub._purge_account_on_exit = "x" * 32

        self._call(stub)  # must not raise
