"""Chats a sync round skipped while the session was down are asked again in
the same round once it is back (client/core/disconnect_skips.py).

Report of 2026-11-11 (Neonize): after a resume the session flapped for 7 s,
~45 chats failed without a request, and a second "Atualizando conversas"
round ran two and a half minutes later."""
from types import SimpleNamespace

from core import disconnect_skips as ds


def _chat(jid):
    return {"remoteJid": jid}


class _Window(SimpleNamespace):
    def __init__(self, fail_again=()):
        super().__init__(_wa_connected=True, calls=[])
        self._fail_again = set(fail_again)

    def _normalize_jid(self, jid):
        return jid.replace("@c.us", "@s.whatsapp.net")

    def sync_remote_chats(self, chats, incremental=False, expected_run_id=None, quiet=False):
        self.calls.append(([c["remoteJid"] for c in chats], incremental, expected_run_id, quiet))
        return {c["remoteJid"] for c in chats} & self._fail_again


def _connected(mw, still_current):
    return True


def test_note_and_take_empties_the_note():
    mw = SimpleNamespace()
    ds.note_skipped_while_disconnected(mw, "1@s.whatsapp.net")
    ds.note_skipped_while_disconnected(mw, "")
    assert ds.take_skipped_while_disconnected(mw) == {"1@s.whatsapp.net"}
    assert ds.take_skipped_while_disconnected(mw) == set()


def test_split_keeps_each_chat_in_its_planned_pass():
    full = [_chat("1@c.us"), _chat("2@g.us")]
    incremental = [_chat("3@g.us"), "not a chat"]
    picked = ds.split_retry_targets(
        full, incremental, {"1@s.whatsapp.net", "3@g.us"},
        lambda j: j.replace("@c.us", "@s.whatsapp.net"))
    assert picked == ([_chat("1@c.us")], [_chat("3@g.us")])


def test_skipped_chats_are_asked_again_and_leave_the_failures():
    mw = _Window()
    ds.note_skipped_while_disconnected(mw, "1@s.whatsapp.net")
    ds.note_skipped_while_disconnected(mw, "3@g.us")
    failures = {"1@s.whatsapp.net", "3@g.us", "9@g.us"}
    remaining = ds.retry_skipped_while_disconnected(
        mw, [_chat("1@c.us"), _chat("9@g.us")], [_chat("3@g.us")], failures,
        7, lambda: True, wait=_connected)
    # 9@g.us was asked and really failed: it is the health checker's
    assert remaining == {"9@g.us"}
    assert mw.calls == [(["1@c.us"], False, 7, True), (["3@g.us"], True, 7, True)]


def test_a_chat_that_fails_again_stays_a_failure():
    mw = _Window(fail_again={"3@g.us"})
    ds.note_skipped_while_disconnected(mw, "3@g.us")
    remaining = ds.retry_skipped_while_disconnected(
        mw, [], [_chat("3@g.us")], {"3@g.us"}, 1, lambda: True, wait=_connected)
    assert remaining == {"3@g.us"}


def test_nothing_is_asked_while_the_session_is_still_down():
    mw = _Window()
    ds.note_skipped_while_disconnected(mw, "3@g.us")
    remaining = ds.retry_skipped_while_disconnected(
        mw, [], [_chat("3@g.us")], {"3@g.us"}, 1, lambda: True,
        wait=lambda mw, still_current: False)
    assert remaining == {"3@g.us"} and mw.calls == []


def test_a_skip_that_did_not_fail_is_not_asked_again():
    mw = _Window()
    ds.note_skipped_while_disconnected(mw, "3@g.us")
    failures = {"9@g.us"}
    assert ds.retry_skipped_while_disconnected(
        mw, [_chat("9@g.us")], [_chat("3@g.us")], failures, 1, lambda: True,
        wait=_connected) is failures
    assert mw.calls == []


def test_wait_until_connected_gives_up_on_timeout_or_a_newer_round():
    now = [0.0]

    def clock():
        return now[0]

    def sleep(seconds):
        now[0] += seconds

    down = SimpleNamespace(_wa_connected=False)
    assert not ds.wait_until_connected(down, lambda: True, timeout=2, clock=clock, sleep=sleep)
    assert now[0] >= 2
    assert not ds.wait_until_connected(down, lambda: False, clock=clock, sleep=sleep)
    up = SimpleNamespace(_wa_connected=True)
    assert ds.wait_until_connected(up, lambda: True, clock=clock, sleep=sleep)


def test_the_round_and_the_chat_fetch_are_wired():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "client" / "main_window"
    sync = (root / "sync.py").read_text(encoding="utf-8")
    fetch = (root / "conversation_sync.py").read_text(encoding="utf-8")
    assert "retry_skipped_while_disconnected(" in sync
    assert "take_skipped_while_disconnected(self)" in sync
    assert fetch.count("note_skipped_while_disconnected(self, remote_jid)") == 2
