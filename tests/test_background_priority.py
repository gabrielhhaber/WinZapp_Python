"""Heavy update children run below normal priority (issue #564)."""

import subprocess

from core.background_priority import background_creationflags


def test_adds_below_normal_and_keeps_existing_flags(monkeypatch):
    monkeypatch.setattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000, raising=False)
    assert background_creationflags(0x08000000) == 0x08004000


def test_platform_without_the_constant_is_unchanged(monkeypatch):
    monkeypatch.delattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", raising=False)
    assert background_creationflags(8) == 8


def test_npm_and_installer_call_sites_use_it():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "client"
    for rel in ("ui/dialogs/api_setup.py", "main_window/wpp_server.py", "updater.py"):
        assert "background_creationflags(" in (root / rel).read_text(encoding="utf-8"), rel


def test_relaunch_script_restores_normal_priority():
    from update_relaunch import relaunch_lines
    assert 'start "" /normal "C:\\WinZapp\\WinZapp.exe"' in relaunch_lines(
        "C:\\WinZapp\\WinZapp.exe", "C:\\log.txt")
