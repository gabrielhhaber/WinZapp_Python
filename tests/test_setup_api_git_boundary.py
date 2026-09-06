"""Which checkout setup_api.py performs on client/api/, and where git may run.

Two failures meet here, and both were invisible to the grep-of-the-source test
this file used to hold:

* An extracted/recovered client/api/ can carry a *partial* .git. Running git
  from it walks upward into WinZapp's own repository and checks the WPPConnect
  tag out there, so that directory may only ever be verified through its
  package.json.
* The very same signal was read *before* the clone, so a fresh client/api/ —
  every CI build, since the directory is git-ignored and absent on a clean
  checkout — was treated as unmanaged too. The homologated tag was never
  checked out, and depending on where upstream's default branch happened to
  sit the run either aborted with "reinstall it through WinZapp" on a build
  machine or built the default branch while reporting the pinned number.

plan_api_checkout() exists so both are two-line assertions instead of a
network clone followed by npm install.
"""

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# The tag every install path is supposed to land on, read from the same
# committed file setup_api.py reads rather than restated here — a pin bump
# must not need this test edited.
PIN = "v" + (ROOT / "client" / "wpp_minimum_version.txt").read_text(
    encoding="utf-8-sig"
).strip().lstrip("vV")


def _setup_api_module():
    """setup_api.py lives at the repo root, which is not on pytest's pythonpath
    (pytest.ini puts client/ there), so load it by path."""
    spec = importlib.util.spec_from_file_location("winzapp_setup_api", ROOT / "setup_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plan(exists, nonempty, git_ok, tag=PIN):
    return _setup_api_module().plan_api_checkout(
        api_dir_exists=exists,
        api_dir_nonempty=nonempty,
        git_has_head_and_config=git_ok,
        tag=tag,
    )


class TestAFreshCheckoutStillGetsThePin:
    def test_an_absent_client_api_clones_and_checks_the_tag_out(self):
        assert _plan(False, False, False) == {"clone": True, "action": "checkout"}

    def test_an_empty_directory_is_treated_as_absent(self):
        """A cancelled extraction leaves the folder behind, not an install."""
        assert _plan(True, False, False) == {"clone": True, "action": "checkout"}

    def test_a_fresh_checkout_with_nothing_pinned_tracks_the_latest_release(self):
        assert _plan(False, False, False, tag="") == {"clone": True, "action": "latest"}


class TestTheGitBoundaryIsStillHeld:
    def test_a_partial_git_directory_is_only_verified_never_checked_out(self):
        assert _plan(True, True, False) == {"clone": False, "action": "verify-snapshot"}

    def test_an_unmanaged_snapshot_with_no_pin_does_nothing(self):
        assert _plan(True, True, False, tag="") == {"clone": False, "action": "none"}

    def test_a_real_clone_on_disk_is_checked_out_in_place(self):
        assert _plan(True, True, True) == {"clone": False, "action": "checkout"}


class TestMainActuallyRunsThePlannedCheckout:
    """The plan is only worth anything if main() runs it, and main()'s wiring
    is what was wrong. Everything past the checkout (npm install, Chromium,
    the build) is cut off by making the restore raise: it is the first call
    after the branch, and it sits outside main()'s own try/except.
    """

    class _StopBeforeNpm(Exception):
        pass

    def _run_main(self, monkeypatch, api_dir):
        setup_api = _setup_api_module()
        commands = []

        def fake_run(cmd, cwd=None):
            commands.append([str(c) for c in cmd])
            if cmd[:2] == ["git", "clone"]:
                # Mirror what a real clone leaves behind: the .git the pinned
                # checkout below is decided against.
                git = Path(cmd[-1]) / ".git"
                git.mkdir(parents=True, exist_ok=True)
                (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
                (git / "config").write_text("[core]\n", encoding="utf-8")
                (Path(cmd[-1]) / "package.json").write_text(
                    '{"version": "2.10.17", "dependencies": {}}', encoding="utf-8"
                )

        def stop(*_args, **_kwargs):
            raise self._StopBeforeNpm

        monkeypatch.setattr(setup_api, "CLIENT_API_DIR", str(api_dir))
        monkeypatch.setattr(setup_api, "_load_env", lambda: {})
        monkeypatch.setattr(setup_api, "_run", fake_run)
        monkeypatch.setattr(setup_api, "_current_tag", lambda cwd: "")
        monkeypatch.setattr(setup_api, "_recover_upstream_package_json", stop)
        try:
            setup_api.main()
        except self._StopBeforeNpm:
            pass
        return commands

    def test_an_absent_client_api_is_cloned_and_pinned(self, tmp_path, monkeypatch):
        commands = self._run_main(monkeypatch, tmp_path / "api")

        assert ["git", "checkout", "-f", PIN] in commands, (
            f"the homologated tag was never checked out: {commands}"
        )

    def test_a_partial_git_directory_never_invokes_git(self, tmp_path, monkeypatch):
        api = tmp_path / "api"
        (api / ".git").mkdir(parents=True)
        (api / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (api / "package.json").write_text(
            '{"version": "%s"}' % PIN.lstrip("v"), encoding="utf-8"
        )

        commands = self._run_main(monkeypatch, api)

        assert not [c for c in commands if c[:1] == ["git"]], (
            f"git was run inside an unmanaged client/api/: {commands}"
        )
