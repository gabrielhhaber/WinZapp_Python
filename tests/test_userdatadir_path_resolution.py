"""Tests for how WinZapp locates WPPConnect's Chrome profile directory.

Two places in main.py used to hand-roll the path to `client/api/userDataDir`
by walking up from `data_path("settings.json")` with two `..`:

    os.path.join(os.path.dirname(data_path("settings.json")),
                 "..", "..", "api", "userDataDir")

`data_path()` already returns `<data>/accounts/<account_id>/`, so two `..`
land on `<data>/` and the result is `<data>/api/userDataDir` — a directory
that has never existed. Every other api/ path in main.py is built with
`resource_path("api", ...)`; only these two were different.

The consequence in `_cleanup_abandoned_sessions_worker` was worse than a
no-op, because `safe_session_dir_to_delete()` is purely lexical and never
checks existence:

    isdir(target)  -> False  =>  nothing is deleted
    exists(target) -> False  =>  read as "confirmed gone"
    store.remove(name)       =>  row dropped, "cleaned abandoned session" logged

So the cleanup reported success while every profile survived — 12 directories
and 754 MB of them by the time this was found, against a store that listed one
session.
"""

import os

import pytest

import app_paths
from app_paths import data_path, resource_path
from tests.god_modules import main_window_source


ACCOUNT = "d5a019ff6d4545cb9b7bf05991e3fd62"


@pytest.fixture(autouse=True)
def _reset_account():
    app_paths.set_active_account(None)
    app_paths._allow_legacy_flat = False
    yield
    app_paths.set_active_account(None)
    app_paths._allow_legacy_flat = False


def _old_broken_expression():
    """The expression both call sites used before the fix."""
    return os.path.abspath(
        os.path.join(
            os.path.dirname(data_path("settings.json")),
            "..", "..", "api", "userDataDir",
        )
    )


class TestTheOldWalkWasWrong:
    def test_it_resolved_under_the_data_directory(self, monkeypatch, tmp_path):
        """It pointed inside data/, where profiles are never written."""
        monkeypatch.chdir(tmp_path)
        app_paths.set_active_account(ACCOUNT)

        broken = _old_broken_expression()

        assert broken == os.path.abspath(str(tmp_path / "data" / "api" / "userDataDir"))
        assert not os.path.exists(broken)

    def test_it_differs_from_the_real_location(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        app_paths.set_active_account(ACCOUNT)

        assert _old_broken_expression() != os.path.abspath(
            resource_path("api", "userDataDir")
        )


class TestResourcePathIsTheRealLocation:
    def test_it_sits_next_to_the_other_api_paths(self, monkeypatch, tmp_path):
        """dist/server.js, start.js and .cache are all resolved this way, and
        they demonstrably work — the profile dir must share their root."""
        monkeypatch.chdir(tmp_path)
        app_paths.set_active_account(ACCOUNT)

        udd = resource_path("api", "userDataDir")
        assert os.path.dirname(udd) == resource_path("api")
        assert os.path.dirname(os.path.dirname(udd)) == resource_path()

    def test_it_does_not_depend_on_the_active_account(self, monkeypatch, tmp_path):
        """userDataDir is shared across accounts — resolving it must not move
        when the active account changes, or one account's cleanup would target
        another account's idea of the directory."""
        monkeypatch.chdir(tmp_path)

        app_paths.set_active_account(ACCOUNT)
        first = resource_path("api", "userDataDir")
        app_paths.set_active_account("ffffffffffffffffffffffffffffffff")
        second = resource_path("api", "userDataDir")

        assert first == second


class TestBothCallSitesWereFixed:
    """A source-level guard, in the style this repo already uses for the
    node_modules patches: the two sites are 5000 lines apart and only one was
    ever noticed, so a future edit reintroducing the walk in either should
    fail here rather than in a user's cleanup log."""

    def test_no_two_dot_walk_to_userdatadir_remains(self):
        source = main_window_source()

        # Strip comments so the explanatory notes describing the old bug do
        # not themselves trip the guard.
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        for chunk in code.split("userDataDir")[:-1]:
            tail = chunk[-300:]
            assert '"..", ".."' not in tail, (
                "a userDataDir path is being built by walking up from data_path() "
                "again — use resource_path('api', 'userDataDir')"
            )

    def test_both_sites_use_resource_path(self):
        source = main_window_source()

        assert source.count('resource_path("api", "userDataDir"') >= 2
