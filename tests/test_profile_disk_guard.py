"""A nearly full disk closes WhatsApp Web cleanly before its profile breaks
(client/core/profile_disk_guard.py).

Report of 2026-11-11: with ~3 GB free the Chrome profile came back "as if
wiped". Chrome keeps ~3 GB of the free disk from the page (measured), so the
page's IndexedDB writes fail first and WhatsApp Web reloads logged out."""
from types import SimpleNamespace

import pytest

import connection_state as cs
from core import profile_disk_guard as guard
from core.connection_lifecycle import connection_context_is_current

GIB = guard.GIB


@pytest.mark.parametrize("free, halted, expected", [
    (3 * GIB, False, guard.HALT),
    (guard.HALT_FREE_BYTES, False, None),
    (int(4.5 * GIB), True, None),          # between the two: stays as it is
    (guard.RESUME_FREE_BYTES, True, guard.RESUME),
    (None, False, None),
    (None, True, None),
])
def test_the_decision_has_a_gap_between_closing_and_reopening(free, halted, expected):
    assert guard.decision(free, halted) == expected


class _Window(SimpleNamespace):
    def __init__(self, tmp_path, **kw):
        said, states = [], []
        kw.setdefault("_uses_neonize", lambda: False)
        kw.setdefault("wpp_custom_api", False)
        super().__init__(
            global_dir=str(tmp_path), token="acc:secret", said=said, states=states,
            i18n=SimpleNamespace(t=lambda key: key + " {free} {needed}"),
            output=lambda text, interrupt=False: said.append(text),
            error_sound=SimpleNamespace(play=lambda: said.append("sound")),
            _set_wa_connected=lambda c, reason, announce=True: states.append((c, announce)),
            **kw)


def _usage(free):
    return lambda path: SimpleNamespace(free=free)


@pytest.fixture
def inline_wx(monkeypatch):
    import wx
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))


def test_low_space_closes_cleanly_goes_offline_and_says_why(tmp_path, inline_wx):
    mw = _Window(tmp_path)
    closing = []
    assert guard.check(mw, _usage(3 * GIB), start=closing.append) == guard.HALT
    assert mw._low_disk_halted is True
    assert mw.states == [(False, False)]           # offline, without the generic speech
    assert mw.said == ["sound", "low_disk_session_closed 3.0 5.0"]
    assert len(closing) == 1                       # close-session + wait, off the poll
    # the next poll does not close again
    assert guard.check(mw, _usage(3 * GIB), start=closing.append) is None
    assert len(closing) == 1


def test_room_again_releases_the_latch_and_says_so(tmp_path, inline_wx):
    mw = _Window(tmp_path, _low_disk_halted=True)
    assert guard.check(mw, _usage(6 * GIB)) == guard.RESUME
    assert mw._low_disk_halted is False
    assert mw.said == ["low_disk_session_resumed {free} {needed}"]


def test_neonize_and_a_custom_api_are_not_guarded(tmp_path, inline_wx):
    neonize = _Window(tmp_path, _uses_neonize=lambda: True)
    assert guard.check(neonize, _usage(1 * GIB)) is None
    remote = _Window(tmp_path, wpp_custom_api=True, _low_disk_halted=True)
    assert guard.check(remote, _usage(1 * GIB)) == guard.RESUME   # a latch left over is dropped
    assert remote._low_disk_halted is False and remote.said == []


def test_the_profile_disk_is_read_even_before_the_profile_exists(tmp_path):
    seen = []
    mw = _Window(tmp_path)
    assert guard.free_bytes_of(mw, lambda p: seen.append(p) or SimpleNamespace(free=7)) == 7
    assert seen == [str(tmp_path)]


def test_the_session_close_asks_chrome_and_waits_for_the_profile(tmp_path, monkeypatch):
    from core import api_client
    posted, waited = [], []
    monkeypatch.setattr(api_client, "api_post", lambda url, **kw: posted.append(url))
    mw = _Window(tmp_path, wpp_server="http://127.0.0.1", wpp_port=6300,
                 wait_for_profile_release=lambda name, timeout: waited.append(name) or True)
    guard._close_session(mw)
    assert posted == ["http://127.0.0.1:6300/api/acc:secret/close-session"]
    assert waited == ["acc"]


def test_the_latch_blocks_the_auto_start_and_delayed_recoveries():
    assert cs.auto_start_block_reason(
        pairing_dialog_active=False, qr_flood_halted=False, recovery_restart_active=False,
        self_inflicted_teardown=False, low_disk_halted=True) == cs.AUTO_START_BLOCKED_LOW_DISK
    assert cs.auto_start_block_reason(
        pairing_dialog_active=False, qr_flood_halted=False, recovery_restart_active=False,
        self_inflicted_teardown=False) is None
    import core.connection_lifecycle as cl
    window = SimpleNamespace(_low_disk_halted=True)
    original = cl.connection_context_is_owned
    cl.connection_context_is_owned = lambda w, c: True
    try:
        assert connection_context_is_current(window, object()) is False
    finally:
        cl.connection_context_is_owned = original


def test_the_health_poll_runs_the_guard_first():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "client" / "main_window"
              / "connection.py").read_text(encoding="utf-8")
    start = source.index("def check_wa_connection_http")
    body = source[start:source.index("status-session", start)]
    assert "profile_disk_guard.check(self)" in body
    assert 'low_disk_halted=getattr(self, "_low_disk_halted", False)' in source
