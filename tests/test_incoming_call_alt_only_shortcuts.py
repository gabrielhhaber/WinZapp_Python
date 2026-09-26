"""The incoming-call popup's shortcuts must fire on Alt+<letter> only.

A `&` mnemonic on a button also fires on the BARE letter while a button has
focus (Win32 IsDialogMessage), so a letter typed into the composer while a
call popup stole focus answered or rejected the call. The dialog now strips
the `&` from the label and registers an Alt accelerator instead.
"""

import json
import re
from pathlib import Path

import pytest

from ui.accessible import split_mnemonic

ROOT = Path(__file__).parents[1]
CALL_KEYS = (
    "incoming_call_answer_button",
    "incoming_call_answer_with_video_button",
    "incoming_call_answer_without_video_button",
    "incoming_call_reject_button",
    "incoming_call_silence_button",
    "incoming_call_close_button",
)


@pytest.mark.parametrize(
    "label, expected",
    [
        ("&Answer", ("Answer", "A")),
        ("Answer without &video", ("Answer without video", "V")),
        ("Fish && &Chips", ("Fish & Chips", "C")),
        ("No shortcut", ("No shortcut", None)),
        ("Trailing &", ("Trailing &", None)),
        ("&Ünicode", ("Ünicode", "Ü")),
    ],
)
def test_split_mnemonic(label, expected):
    assert split_mnemonic(label) == expected


def test_every_locale_gives_each_call_button_a_letter_and_a_clean_label():
    languages = ROOT / "client" / "languages"
    for path in sorted(languages.glob("*.json")):
        if path.name == "language_map.json":
            continue
        entries = json.loads(path.read_text(encoding="utf-8"))
        for key in CALL_KEYS:
            label, letter = split_mnemonic(entries[key])
            assert letter, (path.name, key)
            assert "&" not in label, (path.name, key)


def test_dialog_never_hands_an_ampersand_label_to_a_button():
    """Buttons are created with the raw locale string only to be relabelled by
    _apply_labels(); nothing may call SetLabel with an unstripped string."""
    source = (ROOT / "client" / "ui" / "dialogs" / "incoming_call.py").read_text(
        encoding="utf-8"
    )
    assert "wx.ACCEL_ALT" in source
    assert "split_mnemonic" in source
    for match in re.finditer(r"\.SetLabel\((.*)\)", source):
        assert match.group(1) in ("label", "message"), match.group(0)


def test_accelerator_keycode_ascii_and_unmappable():
    from ui.accessible import accelerator_keycode

    assert accelerator_keycode("A") == ord("A")
    assert accelerator_keycode("a") == ord("A")
    assert accelerator_keycode("7") == ord("7")
    assert accelerator_keycode(None) is None
    assert accelerator_keycode("") is None
    assert accelerator_keycode("SS") is None


def test_split_mnemonic_keeps_letters_whose_uppercase_is_longer():
    assert split_mnemonic("&ßeta") == ("ßeta", "ß")


class _Button:
    def __init__(self, enabled):
        self._enabled = enabled

    def IsEnabled(self):
        return self._enabled


class _Event:
    def __init__(self, event_id):
        self._id = event_id
        self.skipped = False

    def GetId(self):
        return self._id

    def Skip(self):
        self.skipped = True


def _dialog_stub(accel_ids):
    from ui.dialogs.incoming_call import IncomingCallDialog

    class Stub:
        _on_accelerator = IncomingCallDialog._on_accelerator

    stub = Stub()
    stub._accel_ids = accel_ids
    return stub


def test_accelerator_runs_the_handler_of_an_enabled_button():
    calls = []
    stub = _dialog_stub({7: (_Button(True), calls.append)})
    event = _Event(7)
    stub._on_accelerator(event)
    assert calls == [event]


def test_accelerator_ignores_a_disabled_button():
    calls = []
    stub = _dialog_stub({7: (_Button(False), calls.append)})
    stub._on_accelerator(_Event(7))
    assert calls == []


def test_accelerator_skips_an_unknown_id():
    stub = _dialog_stub({})
    event = _Event(99)
    stub._on_accelerator(event)
    assert event.skipped
