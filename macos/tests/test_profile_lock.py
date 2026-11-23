"""Waiting for the Mac's WhatsApp browser to release its profile (profile_lock_mac)."""

import os
import sys
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [os.path.join(ROOT, "macos"), os.path.join(ROOT, "client"), os.path.join(ROOT, ".pydeps")]

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS layer")

from winzapp_mac import profile_lock_mac as pl  # noqa: E402

S = "052b261da0916814f48fe8072cbfb3e4"
PS = (
    "  101 /Applications/WinZapp.app/Contents/MacOS/WinZapp --background\n"
    "  102 /x/node start.js\n"
    f"  103 /x/chrome-headless-shell --headless=new --user-data-dir=/u/WinZapp/data/global/api/userDataDir/{S} about:blank\n"
    "  104 /x/chrome-headless-shell --user-data-dir=/u/WinZapp/data/global/api/userDataDir/otheraccount\n"
    "  105 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome\n"
)


def test_only_this_sessions_browser_matches():
    assert pl.owning_pids(PS, S) == [103]


def test_no_session_name_matches_nothing():
    assert pl.owning_pids(PS, "") == []


def _win(holders_seq):
    seq = list(holders_seq)
    win = SimpleNamespace(audit=[], global_dir="/nonexistent", token=f"{S}:x")
    win._shutdown_audit = win.audit.append
    return win, seq


def test_wait_polls_until_the_browser_is_gone(monkeypatch):
    win, seq = _win([[103], [103], []])
    monkeypatch.setattr(pl, "chrome_pids_owning_session", lambda self, s: seq.pop(0))
    monkeypatch.setattr(pl.time, "sleep", lambda _s: None)
    monkeypatch.setattr("core.profile_recovery.profile_is_local", lambda self: True)
    assert pl.wait_for_profile_release(win, S, timeout=5) is True
    assert seq == []
    assert "3 poll(s)" in win.audit[0]


def test_wait_stops_the_holder_after_the_timeout(monkeypatch):
    win, _ = _win([])
    killed = []
    clock = iter([0.0, 0.0, 10.0, 10.0, 10.0])
    monkeypatch.setattr(pl.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(pl.time, "sleep", lambda _s: None)
    monkeypatch.setattr("core.profile_recovery.profile_is_local", lambda self: True)
    state = {"held": True}
    monkeypatch.setattr(pl, "chrome_pids_owning_session",
                        lambda self, s: [103] if state["held"] else [])

    def kill(self, s):
        killed.append(s)
        state["held"] = False
    monkeypatch.setattr(pl, "kill_orphaned_chrome_for_session", kill)
    assert pl.wait_for_profile_release(win, S, timeout=5) is True
    assert killed == [S]


def test_install_replaces_the_windows_only_methods():
    from main_window.connection import ConnectionMixin
    pl.install()
    assert ConnectionMixin.wait_for_profile_release is pl.wait_for_profile_release
    assert ConnectionMixin._chrome_pids_owning_session is pl.chrome_pids_owning_session
    assert ConnectionMixin._kill_orphaned_chrome_for_session is pl.kill_orphaned_chrome_for_session
