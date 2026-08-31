"""The one-time move of a paired session out of the install dir.

PR #141 pointed WPPConnect's Chrome profile (customUserDataDir) and its file
token store at <global_dir>/api/... via WINZAPP_USER_DATA_DIR /
WINZAPP_TOKEN_STORE_DIR, because the previous cwd-relative defaults land in
PyInstaller's per-launch extraction temp dir in a --onefile build and are
orphaned on every exit.

That is right, but on its own it also aims every ALREADY PAIRED install — all
of which keep a working profile at <install>/api/userDataDir/<session> — at a
folder that does not exist. Chrome would start on a virgin profile and
WhatsApp would ask for a fresh QR: the exact "closed WinZapp, relaunched, told
the device was disconnected" report the new path exists to remove, delivered
once to everybody as the fix for itself.

migrate_legacy_api_state() closes that. It runs against a real paired session,
so the tests below are mostly about what it must refuse to do.
"""

import os

from main import (
    LEGACY_API_STATE_DIRS,
    LEGACY_API_STATE_MARKER,
    MainWindow,
    migrate_legacy_api_state,
)


def _make_legacy_install(root, session="422e4a19812cc15a4c84219ff2eac63d"):
    """A pre-migration install: a Chrome profile and a token file where the
    cwd-relative defaults used to put them."""
    legacy = os.path.join(root, "install", "api")
    profile = os.path.join(legacy, "userDataDir", session)
    os.makedirs(profile)
    with open(os.path.join(profile, "Cookies"), "w", encoding="utf-8") as fh:
        fh.write("credentials")
    tokens = os.path.join(legacy, "tokens")
    os.makedirs(tokens)
    with open(os.path.join(tokens, f"{session}.data.json"), "w", encoding="utf-8") as fh:
        fh.write('{"WABrowserId":"x"}')
    return legacy, session


class TestMigration:
    def test_an_existing_paired_session_is_carried_over(self, tmp_path):
        root = str(tmp_path)
        legacy, session = _make_legacy_install(root)
        new = os.path.join(root, "data", "global", "api")

        moved = migrate_legacy_api_state(legacy, new)

        assert sorted(moved) == ["tokens", "userDataDir"]
        # The profile — the real credential store — is what must survive.
        assert os.path.isfile(os.path.join(new, "userDataDir", session, "Cookies"))
        assert os.path.isfile(os.path.join(new, "tokens", f"{session}.data.json"))
        # And it must be gone from the old place, so nothing keeps reading it.
        assert not os.path.exists(os.path.join(legacy, "userDataDir"))

    def test_it_runs_only_once(self, tmp_path):
        """The marker stops the scan repeating on every launch (and stops two
        accounts' processes fighting over the same folders at startup)."""
        root = str(tmp_path)
        legacy, _ = _make_legacy_install(root)
        new = os.path.join(root, "data", "global", "api")

        assert migrate_legacy_api_state(legacy, new)
        assert os.path.isfile(os.path.join(new, LEGACY_API_STATE_MARKER))

        # A profile reappearing at the old path later is NOT a migration
        # candidate any more — by then the new location is the live one.
        _make_legacy_install(root + "_ignored")
        os.makedirs(os.path.join(legacy, "userDataDir", "later"))
        assert migrate_legacy_api_state(legacy, new) == []
        assert not os.path.exists(os.path.join(new, "userDataDir", "later"))

    def test_it_never_overwrites_a_session_already_at_the_new_path(self, tmp_path):
        """A folder already at the destination means this install is paired
        under the new layout. Clobbering it with a stale copy from the install
        dir would destroy a working session to restore a dead one."""
        root = str(tmp_path)
        legacy, session = _make_legacy_install(root)
        new = os.path.join(root, "data", "global", "api")
        live = os.path.join(new, "userDataDir", session)
        os.makedirs(live)
        with open(os.path.join(live, "Cookies"), "w", encoding="utf-8") as fh:
            fh.write("the live one")

        moved = migrate_legacy_api_state(legacy, new)

        assert "userDataDir" not in moved
        with open(os.path.join(live, "Cookies"), encoding="utf-8") as fh:
            assert fh.read() == "the live one"
        # The unrelated tokens/ folder still migrates — per folder, not all
        # or nothing, so a half-done previous attempt completes.
        assert "tokens" in moved

    def test_a_fresh_install_is_a_no_op_but_still_stops_scanning(self, tmp_path):
        root = str(tmp_path)
        legacy = os.path.join(root, "install", "api")
        os.makedirs(legacy)
        new = os.path.join(root, "data", "global", "api")

        assert migrate_legacy_api_state(legacy, new) == []
        assert os.path.isfile(os.path.join(new, LEGACY_API_STATE_MARKER))

    def test_same_source_and_destination_is_refused(self, tmp_path):
        """A dev checkout can legitimately resolve both to the same folder;
        shutil.move onto itself would raise."""
        root = str(tmp_path)
        legacy, _ = _make_legacy_install(root)

        assert migrate_legacy_api_state(legacy, legacy) == []
        assert not os.path.exists(os.path.join(legacy, LEGACY_API_STATE_MARKER))

    def test_missing_paths_are_refused(self, tmp_path):
        assert migrate_legacy_api_state("", str(tmp_path)) == []
        assert migrate_legacy_api_state(str(tmp_path), "") == []


class TestItIsWiredIntoStartup:
    def test_start_wpp_background_migrates_before_spawning_node(self):
        """Order matters twice over: the move must happen before Popen (moving
        a userDataDir out from under a live Chrome corrupts exactly what this
        protects), and it must happen at all — the env vars alone are the
        regression."""
        import inspect

        from main import MainWindow

        src = inspect.getsource(MainWindow._start_wpp_background)
        assert "migrate_legacy_api_state(" in src, (
            "_start_wpp_background() sets WINZAPP_USER_DATA_DIR but never "
            "migrates an existing install's session to it"
        )
        assert src.index("migrate_legacy_api_state(") < src.index("subprocess.Popen("), (
            "the migration must run before Node is spawned"
        )


class TestNothingMayPreCreateTheDestination:
    """The migration skips any folder already present at the destination —
    deliberately, since one being there means this install is already on the
    new location. It then writes its run-once marker even when nothing moved.

    So creating those folders before it runs does not merely skip the move,
    it makes the skip PERMANENT. An already-paired install gets a virgin
    Chrome profile, is asked to pair again, and its real profile is stranded
    under the old location with the marker guaranteeing no retry — the exact
    report this migration was written to prevent, delivered by its own fix.

    This nearly shipped: two `os.makedirs` calls were added just above the
    call, to satisfy a Win32 path-shortening helper that needs a real
    directory entry. Ordering was the only thing wrong with them.
    """

    def test_an_empty_pre_created_destination_permanently_blocks_the_move(self, tmp_path):
        """States the bug as a test: this is what pre-creating causes."""
        legacy = tmp_path / "install" / "api"
        (legacy / "userDataDir" / "session-a").mkdir(parents=True)
        (legacy / "tokens").mkdir(parents=True)
        persistent = tmp_path / "global" / "api"
        for name in LEGACY_API_STATE_DIRS:
            (persistent / name).mkdir(parents=True)

        moved = migrate_legacy_api_state(str(legacy), str(persistent))

        assert moved == []
        assert (legacy / "userDataDir" / "session-a").is_dir(), "profile stranded"
        assert (persistent / LEGACY_API_STATE_MARKER).is_file(), (
            "and the marker is written anyway, so no launch ever retries"
        )

    def test_the_directories_are_created_only_after_the_migration(self):
        """A source-order assertion because the failure is invisible at
        runtime: nothing raises, nothing logs, the session simply comes up
        unpaired once and never recovers."""
        import inspect
        src = inspect.getsource(MainWindow._start_wpp_background)
        assert src.index("migrate_legacy_api_state(") < src.index("os.makedirs(_udd"), (
            "os.makedirs(_udd) runs before migrate_legacy_api_state(), which "
            "makes the migration skip every folder and then write its marker"
        )

    def test_the_env_vars_are_set_before_anything_that_can_raise(self):
        """They are assigned twice on purpose: the long forms first and
        unconditionally, the shortened ones after the directories exist. A
        launch that reaches Node with neither set falls back to a cwd-relative
        profile, which a --onefile build loses on every exit."""
        import inspect
        src = inspect.getsource(MainWindow._start_wpp_background)
        assert src.index('os.environ["WINZAPP_USER_DATA_DIR"]') < src.index("os.makedirs(_udd")
