"""How much history a session asks for when it pairs (pairing_history.py):
the user's choice, kept in the session's folder, read by every start."""
from types import SimpleNamespace as N

import pairing_history as ph
import server


def test_only_the_offered_choices_are_taken():
    for days in ph.CHOICES:
        assert ph.chosen(days) == days
        assert ph.chosen(str(days)) == days
    for bad in (7, -1, "x", None, True, [], 999999):
        assert ph.chosen(bad) is None


def test_the_choice_is_kept_in_the_session_folder(tmp_path):
    assert ph.load(str(tmp_path), 180) == 180          # nothing chosen: the default
    assert ph.save(str(tmp_path), 3650) is True
    assert ph.load(str(tmp_path), 180) == 3650
    (tmp_path / ph.FILE).write_text('{"days": 12}', encoding="utf-8")
    assert ph.load(str(tmp_path), 180) == 180          # not a choice: the default
    (tmp_path / ph.FILE).write_text("junk", encoding="utf-8")
    assert ph.load(str(tmp_path), 30) == 30


def test_a_qr_on_screen_is_redone_and_a_paired_session_is_left_alone():
    assert ph.answer("QRCODE", 180, 3650) == (True, True, "stored")
    assert ph.answer("CLOSED", 180, 3650) == (True, False, "stored")
    assert ph.answer("QRCODE", 3650, 3650) == (False, False, "unchanged")
    assert ph.answer("CONNECTED", 180, 3650) == (False, False, "paired")


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
