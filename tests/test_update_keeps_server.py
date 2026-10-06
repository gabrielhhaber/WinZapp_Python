"""A WinZapp update must not put back an older WPPConnect Server.

Reported live with background downloads on: the server updated to 2.10.36
(its own log said so), then the next alpha of WinZapp xcopied the release's
api/package.json (2.10.30) over it, and the same update was offered again.
See core/update_keeps_server.py.
"""

import json
import os

import updater
from core.update_keeps_server import (
    is_newer_server,
    keep_newer_installed_server,
    merged_package,
)

PINS = ["@wppconnect-team/wppconnect", "@wppconnect/wa-js", "zod"]


def _write_pkg(api_dir, **pkg):
    os.makedirs(api_dir, exist_ok=True)
    with open(os.path.join(api_dir, "package.json"), "w", encoding="utf-8") as fh:
        json.dump(pkg, fh)


def _read_pkg(api_dir):
    with open(os.path.join(api_dir, "package.json"), encoding="utf-8") as fh:
        return json.load(fh)


SHIPPED = {
    "version": "2.10.30",
    "dependencies": {
        "@wppconnect-team/wppconnect": "2.3.5",
        "@wppconnect/wa-js": "4.6.2",
        "axios": "^1.14.0",
    },
}
INSTALLED = {
    "version": "2.10.37",
    "scripts": {"start": "node dist/server.js"},
    "dependencies": {
        "@wppconnect-team/wppconnect": "2.3.4",
        "@wppconnect/wa-js": "4.6.1",
        "axios": "^1.20.0",
        "zod": "^3.25.0",
    },
}


class TestIsNewerServer:
    def test_strictly_newer(self):
        assert is_newer_server("2.10.37", "2.10.30")
        assert is_newer_server("2.10.100", "2.10.37")

    def test_equal_or_older_is_not(self):
        assert not is_newer_server("2.10.30", "2.10.30")
        assert not is_newer_server("2.10.29", "2.10.30")

    def test_cannot_tell_is_not(self):
        assert not is_newer_server("", "2.10.30")
        assert not is_newer_server("2.10.37", "")
        assert not is_newer_server("garbage", "2.10.30")


class TestMergedPackage:
    def test_keeps_the_installed_version_and_ranges(self):
        merged = merged_package(INSTALLED, SHIPPED, PINS)
        assert merged["version"] == "2.10.37"
        assert merged["scripts"] == INSTALLED["scripts"]
        # The installed node_modules was resolved from these, not the release's.
        assert merged["dependencies"]["axios"] == "^1.20.0"

    def test_takes_the_releases_homologated_pins(self):
        merged = merged_package(INSTALLED, SHIPPED, PINS)
        # So the library drift gate sees the pin the new build moved.
        assert merged["dependencies"]["@wppconnect-team/wppconnect"] == "2.3.5"
        assert merged["dependencies"]["@wppconnect/wa-js"] == "4.6.2"

    def test_a_pin_the_release_does_not_declare_stays_as_installed(self):
        merged = merged_package(INSTALLED, SHIPPED, PINS)
        assert merged["dependencies"]["zod"] == "^3.25.0"

    def test_does_not_mutate_its_inputs(self):
        installed = json.loads(json.dumps(INSTALLED))
        merged_package(installed, SHIPPED, PINS)
        assert installed == INSTALLED


class TestKeepNewerInstalledServer:
    def test_a_newer_installed_server_is_written_into_the_payload(self, tmp_path):
        payload, installed = str(tmp_path / "payload" / "api"), str(tmp_path / "install" / "api")
        _write_pkg(payload, **SHIPPED)
        _write_pkg(installed, **INSTALLED)

        assert keep_newer_installed_server(payload, installed, PINS) == "2.10.37"

        pkg = _read_pkg(payload)
        assert pkg["version"] == "2.10.37"
        assert pkg["dependencies"]["@wppconnect-team/wppconnect"] == "2.3.5"
        assert not os.path.exists(os.path.join(payload, "package.json.winzapp-tmp"))
        # The installed file itself is xcopy's to overwrite, never touched here.
        assert _read_pkg(installed) == INSTALLED

    def test_a_release_at_or_above_the_installed_server_is_left_alone(self, tmp_path):
        payload, installed = str(tmp_path / "payload" / "api"), str(tmp_path / "install" / "api")
        _write_pkg(payload, **{**SHIPPED, "version": "2.10.40"})
        _write_pkg(installed, **INSTALLED)

        assert keep_newer_installed_server(payload, installed, PINS) == ""
        assert _read_pkg(payload)["version"] == "2.10.40"

    def test_nothing_installed_yet_is_left_alone(self, tmp_path):
        payload, installed = str(tmp_path / "payload" / "api"), str(tmp_path / "install" / "api")
        _write_pkg(payload, **SHIPPED)

        assert keep_newer_installed_server(payload, installed, PINS) == ""
        assert _read_pkg(payload) == SHIPPED

    def test_an_unreadable_installed_package_is_left_alone(self, tmp_path):
        payload, installed = str(tmp_path / "payload" / "api"), str(tmp_path / "install" / "api")
        _write_pkg(payload, **SHIPPED)
        os.makedirs(installed)
        with open(os.path.join(installed, "package.json"), "w", encoding="utf-8") as fh:
            fh.write("{ truncated")

        assert keep_newer_installed_server(payload, installed, PINS) == ""
        assert _read_pkg(payload) == SHIPPED

    def test_a_release_without_api_is_left_alone(self, tmp_path):
        installed = str(tmp_path / "install" / "api")
        _write_pkg(installed, **INSTALLED)

        assert keep_newer_installed_server(str(tmp_path / "payload" / "api"),
                                           installed, PINS) == ""


class TestTheInstallerKeepsTheNewerServer:
    def test_run_batch_installer_rewrites_the_payload_before_the_script(
        self, tmp_path, monkeypatch
    ):
        extracted = tmp_path / "extracted"
        install_dir = tmp_path / "install"
        _write_pkg(str(extracted / "WinZapp" / "api"), **SHIPPED)
        _write_pkg(str(install_dir / "api"), **INSTALLED)

        monkeypatch.setattr(updater, "log_path", lambda *parts: str(tmp_path / "logs"))
        monkeypatch.setattr(updater, "_needs_admin", lambda: False)
        monkeypatch.setattr(updater.sys, "platform", "win32")
        monkeypatch.setattr(updater.subprocess, "Popen", lambda *a, **kw: None)
        seen = {}

        def _fake_write(bat_path, script):
            # What xcopy will copy is decided before the script exists.
            seen["version"] = _read_pkg(str(extracted / "WinZapp" / "api"))["version"]
            return True

        monkeypatch.setattr(updater, "_write_installer_script", _fake_write)

        assert updater._run_batch_installer(str(extracted), str(install_dir),
                                            "WinZapp.exe", pid=1234) is True
        assert seen["version"] == "2.10.37"

    def test_uses_the_same_pins_as_every_server_install(self):
        from ui.dialogs.api_setup import _PATCHED_DEPENDENCY_KEYS
        assert "@wppconnect-team/wppconnect" in _PATCHED_DEPENDENCY_KEYS
        assert "@wppconnect/wa-js" in _PATCHED_DEPENDENCY_KEYS
