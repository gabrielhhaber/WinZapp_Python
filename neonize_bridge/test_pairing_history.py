"""How much history a session asks for when it pairs (pairing_history.py):
the user's choice, kept in the session's folder, read by every start."""
from types import SimpleNamespace as N

import pairing_history as ph
import server


def test_any_number_of_days_up_to_twenty_years_is_taken():
    for days in ph.CHOICES + (1, 7, 548, ph.MAX_DAYS):
        assert ph.chosen(days) == days
        assert ph.chosen(str(days)) == days
    for bad in (-1, ph.MAX_DAYS + 1, "x", None, True, [], 999999, 0.5, "2.5"):
        assert ph.chosen(bad) is None


def test_the_choice_is_kept_in_the_session_folder(tmp_path):
    assert ph.load(str(tmp_path), 180) == 180          # nothing chosen: the default
    assert ph.save(str(tmp_path), 3650) is True
    assert ph.load(str(tmp_path), 180) == 3650
    (tmp_path / ph.FILE).write_text('{"days": -12}', encoding="utf-8")
    assert ph.load(str(tmp_path), 180) == 180          # not a choice: the default
    (tmp_path / ph.FILE).write_text("junk", encoding="utf-8")
    assert ph.load(str(tmp_path), 30) == 30


def test_a_qr_on_screen_is_redone_and_a_paired_session_is_left_alone():
    assert ph.answer("QRCODE", 180, 3650) == (True, True, "stored")
    assert ph.answer("CLOSED", 180, 3650) == (True, False, "stored")
    assert ph.answer("QRCODE", 3650, 3650) == (False, False, "unchanged")
    assert ph.answer("CONNECTED", 180, 3650) == (False, False, "paired")


def test_a_choice_made_while_the_session_starts_reaches_its_qr():
    """Chosen in the dialog's first seconds (INITIALIZING): stored, and the
    pairing is redone; choosing it again is compared with what the running
    client registered with, not with the file."""
    assert ph.answer("INITIALIZING", 180, 3650, running=180) == (True, True, "stored")
    # stored already, but the QR on screen still carries 180 days
    assert ph.answer("QRCODE", 3650, 3650, running=180) == (False, True, "stored")
    assert ph.answer("QRCODE", 3650, 3650, running=3650) == (False, False, "unchanged")


def test_the_device_asks_for_the_chosen_history():
    full = server.device_props(3650)
    assert full.requireFullSync and full.historySyncConfig.fullSyncDaysLimit == 3650
    # "recent only" says so outright: neonize merges these into one value per process
    recent = server.device_props(0)
    assert recent.HasField("requireFullSync") and recent.requireFullSync is False


def test_a_restart_never_touches_a_paired_session():
    stopped = []
    s = N(status="CONNECTED", _lifecycle_lock=__import__("threading").RLock(),
          _has_identity=lambda: False, stop=lambda *a: stopped.append(a))
    assert server.Session.restart_pairing(s) is False
    s.status, s._has_identity = "QRCODE", lambda: True
    assert server.Session.restart_pairing(s) is False
    assert stopped == []


def _redo(session, stored):
    import asyncio
    restarts = []

    async def restart():
        restarts.append(session.status)
        return True

    async def sleep(_seconds):
        session.steps.pop(0)(session) if session.steps else None

    done = asyncio.run(ph.redo_when_qr(session, lambda: stored, restart, sleep, wait=5, step=1))
    return done, restarts


def test_a_choice_made_while_starting_redoes_the_pairing_once_the_qr_shows():
    s = N(status="INITIALIZING", pairing_days=180,
          steps=[lambda s: None, lambda s: setattr(s, "status", "QRCODE")])
    assert _redo(s, 3650) == (True, ["QRCODE"])


def test_nothing_is_redone_when_the_client_already_asks_for_the_choice():
    s = N(status="INITIALIZING", pairing_days=3650, steps=[lambda s: setattr(s, "status", "QRCODE")])
    assert _redo(s, 3650) == (False, [])


def test_nothing_is_redone_once_paired_or_when_no_code_ever_shows():
    paired = N(status="INITIALIZING", pairing_days=180, steps=[lambda s: setattr(s, "status", "CONNECTED")])
    assert _redo(paired, 3650) == (False, [])
    stuck = N(status="INITIALIZING", pairing_days=180, steps=[])
    assert _redo(stuck, 3650) == (False, [])
