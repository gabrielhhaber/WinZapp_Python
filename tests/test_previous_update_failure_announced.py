"""A failed update must be told to the user, not only logged.

The batch installer writes update_failed.marker exactly so the next launch
can say the update did not land. MainWindow.__init__ read it, logged it at
ERROR and deleted it — nobody was told. Pedro's logs (2026-09-17): two
installs in a row failed on "Violação de compartilhamento" over WinZapp.exe,
both runs logged "[UPDATER_STATUS] WARNING: Found update_failed.marker",
and the user's report was "it updates and nothing changes".
"""

import types

import wx

from main import MainWindow


class _Sound:
    def __init__(self):
        self.played = 0

    def play(self):
        self.played += 1


def _stub():
    spoken = []
    stub = types.SimpleNamespace(
        error_sound=_Sound(),
        i18n=types.SimpleNamespace(t=lambda k: f"<{k}>"),
        output=lambda text, interrupt=False: spoken.append(text),
        spoken=spoken,
    )
    stub._announce_previous_update_failure = (
        MainWindow._announce_previous_update_failure.__get__(stub)
    )
    return stub


def test_it_plays_the_error_sound_speaks_and_shows_the_message(monkeypatch):
    shown = []
    monkeypatch.setattr(wx, "MessageBox", lambda msg, title, *a, **k: shown.append((msg, title)))
    stub = _stub()
    stub._announce_previous_update_failure()
    assert stub.error_sound.played == 1
    assert stub.spoken == ["<update_failed_previous>"]
    assert shown == [("<update_failed_previous>", "<update_error_title>")]


def test_a_missing_sound_does_not_stop_the_message(monkeypatch):
    shown = []
    monkeypatch.setattr(wx, "MessageBox", lambda msg, title, *a, **k: shown.append(msg))
    stub = _stub()
    del stub.error_sound
    stub._announce_previous_update_failure()
    assert shown == ["<update_failed_previous>"]
