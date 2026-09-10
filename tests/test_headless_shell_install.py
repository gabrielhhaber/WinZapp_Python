"""The browser is part of "is the install complete?", and it never used to be.

Every check in _ensure_api_modules_installed() looks at client/api/ — is
dist/server.js there, is node_modules there — and not one of them looks at
whether a browser exists. So "the API is fully set up" was true for an install
with no browser at all, and the only thing that would fetch one was start.js's
own execSync fallback, running inside the API process at boot.

That placement is the problem this module's code guards:

  * ApiStartupDialog gives the API 300 s to come up (_TIMEOUT_S). A download
    happening inside start.js spends that budget before the server has even
    started listening, so a slow connection turns a one-off browser download
    into "WinZapp failed to start the API".
  * ensure_api_modules_installed() runs *before* ensure_wpp_running(), i.e.
    before that timer starts. Downloading here costs the same wait but cannot
    trip the timeout.

Who actually reaches it: not fresh installs — ApiSetupDialog's step 4.5 fetches
the shell as part of its own progress UI. It is everyone *updating* from a
build that shipped full Chrome. Their .cache already holds a chrome.exe, so
every existing check passes, and without this nothing would ever fetch the
shell that start.js now looks for.

Best-effort on purpose: a failure is logged, not fatal, because start.js's
fallback still gets its turn and a full Chrome already in the cache still
works.
"""

import os
import sys
from pathlib import Path

import pytest

from main import MainWindow

SHELL = "chrome-headless-shell.exe"


class _Stub:
    """MainWindow is a wx.Frame and cannot be instantiated without a wx.App,
    so the methods under test are bound onto a plain object carrying only what
    they touch."""

    _HEADLESS_SHELL_NAMES = MainWindow._HEADLESS_SHELL_NAMES
    _WINDOWS_CHROME_NAMES = getattr(MainWindow, "_WINDOWS_CHROME_NAMES", ("chrome.exe",))
    find_headless_shell = MainWindow.find_headless_shell
    find_incomplete_browser = MainWindow.find_incomplete_browser
    ensure_headless_shell_installed = MainWindow.ensure_headless_shell_installed
    ensure_api_modules_installed = MainWindow.ensure_api_modules_installed


@pytest.fixture
def api_tree(tmp_path, monkeypatch):
    """Stands in for resource_path(), which resolves dev vs. frozen layouts."""
    import main

    api = tmp_path / "api"
    (api / ".cache").mkdir(parents=True)
    (tmp_path / "node").mkdir()

    def fake_resource_path(*parts):
        return str(tmp_path.joinpath(*parts))

    monkeypatch.setattr(main, "resource_path", fake_resource_path)
    return api


def _install_shell(api, version="win64-1.2.3", complete=True):
    """Mirror puppeteer's real nesting, so the walk is genuinely exercised.

    `complete` lays down the payload files a browser cannot start without —
    see core/browser_payload.py. Default True because every test here is about
    *finding* a browser, and one that cannot start no longer counts as found.
    """
    import sys
    if sys.platform == "win32":
        d = api / ".cache" / "puppeteer" / "chrome" / version / "chrome-win64"
        d.mkdir(parents=True, exist_ok=True)
        exe = d / "chrome.exe"
    else:
        d = api / ".cache" / "chrome-headless-shell" / version / "chrome-headless-shell-win64"
        d.mkdir(parents=True, exist_ok=True)
        exe = d / SHELL
    exe.write_text("binary", encoding="utf-8")
    if complete:
        (d / "icudtl.dat").write_bytes(b"icu" * 8)
    return exe


class TestFindingTheShell:
    def test_it_is_found_however_deeply_puppeteer_nested_it(self, api_tree):
        exe = _install_shell(api_tree)
        assert _Stub().find_headless_shell() == str(exe)

    def test_an_empty_cache_reports_nothing(self, api_tree):
        assert _Stub().find_headless_shell() is None

    def test_a_leftover_full_chrome_is_not_mistaken_for_the_shell(self, api_tree, monkeypatch):
        """On non-Windows, a full Chrome in cache is not mistaken for headless shell."""
        monkeypatch.setattr("sys.platform", "linux")
        d = api_tree / ".cache" / "puppeteer" / "chrome" / "win64-148" / "chrome-win64"
        d.mkdir(parents=True, exist_ok=True)
        (d / "chrome.exe").write_text("binary", encoding="utf-8")
        # Complete on purpose: this must fail on the binary NAME, not on a
        # payload check that would make it pass for the wrong reason.
        (d / "icudtl.dat").write_bytes(b"icu" * 8)
        assert _Stub().find_headless_shell() is None

    def test_a_missing_cache_directory_is_not_an_error(self, tmp_path, monkeypatch):
        import main
        monkeypatch.setattr(main, "resource_path", lambda *p: str(tmp_path.joinpath(*p)))
        assert _Stub().find_headless_shell() is None


class TestInstallingTheShell:
    def test_an_existing_shell_downloads_nothing(self, api_tree, monkeypatch):
        import main
        _install_shell(api_tree)

        def boom(*a, **k):
            raise AssertionError("must not spawn an installer when the shell is present")

        monkeypatch.setattr(main.subprocess, "Popen", boom)
        assert _Stub().ensure_headless_shell_installed() is True

    def test_a_missing_shell_triggers_the_puppeteer_cli(self, api_tree, monkeypatch):
        import main
        calls = {}

        class FakeProc:
            returncode = 0

            def communicate(self, timeout=None):
                # A real install lands the binary on disk; do the same so the
                # post-check can confirm it.
                _install_shell(api_tree)
                return (b"chrome-headless-shell@1.2.3 /path", b"")

        def fake_popen(cmd, **kwargs):
            calls["cmd"] = cmd
            calls["env"] = kwargs.get("env", {})
            calls["cwd"] = kwargs.get("cwd")
            return FakeProc()

        monkeypatch.setattr(main.subprocess, "Popen", fake_popen)
        assert _Stub().ensure_headless_shell_installed() is True

        expected_product = "chrome" if sys.platform == "win32" else "chrome-headless-shell"
        assert calls["cmd"][-5:] == [
            "exec", "puppeteer", "browsers", "install", expected_product,
        ], calls["cmd"]
        assert calls["cwd"] == str(api_tree)

    def test_it_downloads_into_the_tree_start_js_searches(self, api_tree, monkeypatch):
        """start.js sets PUPPETEER_CACHE_DIR to client/api/.cache. Installing
        into .cache/puppeteer instead puts the browser in a tree the server is
        not the one looking in."""
        import main
        calls = {}

        class FakeProc:
            returncode = 0

            def communicate(self, timeout=None):
                _install_shell(api_tree)
                return (b"", b"")

        monkeypatch.setattr(
            main.subprocess, "Popen",
            lambda cmd, **kw: (calls.update(env=kw.get("env", {})), FakeProc())[1],
        )
        _Stub().ensure_headless_shell_installed()
        assert calls["env"]["PUPPETEER_CACHE_DIR"] == str(api_tree / ".cache")

    def test_a_failed_download_is_reported_but_not_fatal(self, api_tree, monkeypatch):
        """start.js still has its own fallback, and a full Chrome already in
        the cache still launches — refusing to start would be worse."""
        import main

        class FakeProc:
            returncode = 1

            def communicate(self, timeout=None):
                return (b"", b"network unreachable")

        monkeypatch.setattr(main.subprocess, "Popen", lambda cmd, **kw: FakeProc())
        assert _Stub().ensure_headless_shell_installed() is False

    def test_a_crashing_installer_is_swallowed(self, api_tree, monkeypatch):
        import main

        def boom(*a, **k):
            raise OSError("node.exe is missing")

        monkeypatch.setattr(main.subprocess, "Popen", boom)
        assert _Stub().ensure_headless_shell_installed() is False

    def test_no_api_directory_yet_is_skipped_quietly(self, tmp_path, monkeypatch):
        import main
        monkeypatch.setattr(main, "resource_path", lambda *p: str(tmp_path.joinpath(*p)))
        monkeypatch.setattr(
            main.subprocess, "Popen",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("nothing to install into")),
        )
        assert _Stub().ensure_headless_shell_installed() is False


def test_the_browser_check_runs_on_every_setup_path(monkeypatch):
    """The api-modules check has several early returns (already set up, markers
    repaired, dialog shown). The browser check must not sit behind any of
    them — that is the whole bug."""
    seen = []

    class S(_Stub):
        def _ensure_api_modules_installed(self):
            seen.append("modules")

        def ensure_headless_shell_installed(self):
            seen.append("browser")

    S().ensure_api_modules_installed()
    assert seen == ["modules", "browser"]
