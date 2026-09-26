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
