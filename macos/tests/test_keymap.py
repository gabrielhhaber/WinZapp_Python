"""Mac keymap rules. Run from the repo root:  python3 -m pytest macos/tests -q

The combos checked are harvested from WinZapp itself — every "\\tAccel" menu
label in client/ and every shortcut named in the en-US catalog — so a new upstream
shortcut that lands on a macOS/VoiceOver command fails here at update time.
"""

import os
import re
import sys

import pytest

from tests.locales import load_strings

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [os.path.join(ROOT, "macos"), os.path.join(ROOT, "client"), os.path.join(ROOT, ".pydeps")]

import wx  # noqa: E402
from winzapp_mac import keymap_mac as km  # noqa: E402

W = {"ctrl": km.W_CTRL, "alt": km.W_ALT, "shift": km.W_SHIFT}


def _parse_win(combo):
    parts = [p.strip() for p in combo.split("+") if p.strip()]
    mods = frozenset(W[p.lower()] for p in parts[:-1] if p.lower() in W)
    key = parts[-1].upper()
    key = {"DEL": "DELETE", "ENTER": "RETURN", "COMMA": ","}.get(key, key)
    return mods, key


def _harvest():
    combos = set()
    accel = re.compile(r"\\t((?:(?:Ctrl|Alt|Shift)\+)+(?!(?:Ctrl|Alt|Shift)\b)[A-Za-z0-9,.]+|F\d+|Delete)")
    for dirpath, _, files in os.walk(os.path.join(ROOT, "client")):
        if "api" in dirpath.split(os.sep) or "node_modules" in dirpath:
            continue
        for f in files:
            if f.endswith(".py"):
                with open(os.path.join(dirpath, f), encoding="utf-8") as fh:
                    combos.update(accel.findall(fh.read()))
    # Accelerator-table tuples: (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord('R'), ...)
    table = re.compile(r"\(\s*((?:wx\.ACCEL_(?:CTRL|ALT|SHIFT|NORMAL)\s*\|?\s*)+),\s*"
                       r"(?:ord\(\s*['\"](.)['\"]\s*\)|wx\.WXK_(\w+))")
    for dirpath, _, files in os.walk(os.path.join(ROOT, "client")):
        if "api" in dirpath.split(os.sep) or "node_modules" in dirpath:
            continue
        for f in files:
            if not f.endswith(".py"):
                continue
            with open(os.path.join(dirpath, f), encoding="utf-8") as fh:
                for flags, ch, wxk in table.findall(fh.read()):
                    mods = [m.capitalize() for m in re.findall(r"ACCEL_(CTRL|ALT|SHIFT)", flags)]
                    key = ch.upper() if ch else wxk
                    if key and (mods or key.startswith("F") or key == "DELETE"):
                        combos.add("+".join(mods + [key]))
    strings = load_strings("en-US")
    named = re.compile(r"\b((?:(?:Ctrl|Alt|Shift)\+)+(?:[A-Z0-9]|F\d+|Delete|Space|Tab)\b)")
    for k, v in strings.items():
        if k.startswith("shortcut_") and isinstance(v, str):
            combos.update(named.findall(v))
    return sorted(combos)


COMBOS = _harvest()


def test_harvest_found_shortcuts():
    assert len(COMBOS) > 60


@pytest.mark.parametrize("combo", COMBOS)
def test_no_shortcut_lands_on_a_system_command(combo):
    mac = km.win_to_mac(*_parse_win(combo))
    assert mac not in km.RESERVED or mac in km.INTENTIONAL, f"{combo} -> {mac} is reserved by macOS"


@pytest.mark.parametrize("combo", COMBOS)
def test_every_shortcut_is_reachable_from_its_mac_keys(combo):
    win = _parse_win(combo)
    mac = km.win_to_mac(*win)
    if win[0] <= {km.W_SHIFT} and mac == win:
        return  # unmodified / Shift-only keys pass through untouched
    assert km.mac_to_win_table().get(mac) == win, f"{mac} does not map back to {combo}"


@pytest.mark.parametrize("win, spoken", [
    ("Alt+1: go to chats", "Command-1: go to chats"),
    ("Locked chats alt+7", "Locked chats Command-7"),
    ("Ctrl+Shift+R: react", "Shift-Command-R: react"),
    ("Alt+R: reply", "Option-Command-R: reply"),
    ("Ctrl+Alt+Shift+Q: exit", "Command-Q: exit"),
    ("Ctrl+Shift+Q: archive", "Control-Command-A: archive"),
    ("ctrl+comma", "Command-comma"),
    ("Alt+{letter}: focus", "Option-Command-{letter}: focus"),
    ("Ctrl+Tab and Ctrl+Shift+Tab: switch", "Control-Tab and Control-Shift-Tab: switch"),
    ("Delete", "Delete"),
    ("Ctrl+Shift+3: remove bookmark 3", "Control-Shift-3: remove bookmark 3"),
])
def test_spoken_shortcuts(win, spoken):
    assert km.mac_shortcut_text(win) == spoken


@pytest.mark.parametrize("win, mac", [
    ("Reply\tAlt+R", "Reply\tAlt+Ctrl+R"),
    ("Exit\tCtrl+Alt+Shift+Q", "Exit\tCtrl+Q"),
    ("Delete\tDelete", "Delete\tCtrl+Back"),
    ("Settings\tCtrl+,", "Settings\tCtrl+,"),
])
def test_menu_labels(win, mac):
    got = km.mac_menu_text(win)
    label, accel = got.split("\t")
    want_label, want_accel = mac.split("\t")
    e1, e2 = wx.AcceleratorEntry(), wx.AcceleratorEntry()
    assert e1.FromString(accel) and e2.FromString(want_accel)
    assert (label, e1.GetFlags(), e1.GetKeyCode()) == (want_label, e2.GetFlags(), e2.GetKeyCode())


@pytest.fixture(scope="module", autouse=True)
def _app():
    app = wx.App(False)
    yield app


def _key(code, cmd=False, ctrl=False, opt=False, shift=False):
    evt = wx.KeyEvent(wx.wxEVT_KEY_DOWN)
    evt.SetKeyCode(code)
    evt.SetControlDown(cmd)
    evt.SetRawControlDown(ctrl)
    evt.SetAltDown(opt)
    evt.SetShiftDown(shift)
    return evt


def _state(evt):
    return (evt.ControlDown(), evt.AltDown(), evt.ShiftDown(), evt.RawControlDown(),
            evt.MetaDown(), evt.GetKeyCode())


def test_command_option_r_arrives_as_alt_r():
    evt = _key(ord("R"), cmd=True, opt=True)
    km.translate_key_event(evt)
    assert _state(evt) == (False, True, False, False, False, ord("R"))


def test_command_digit_arrives_as_alt_digit():
    evt = _key(ord("1"), cmd=True)
    km.translate_key_event(evt)
    assert _state(evt)[:2] == (False, True)


def test_control_digit_arrives_as_ctrl_digit_bookmark():
    evt = _key(ord("3"), ctrl=True)
    km.translate_key_event(evt)
    assert _state(evt)[:4] == (True, False, False, False)


def test_option_letter_is_typing_not_a_shortcut():
    evt = _key(ord("E"), opt=True)
    km.translate_key_event(evt)
    assert not evt.AltDown() and evt.MetaDown()


def test_command_delete_outside_text_is_delete():
    evt = _key(wx.WXK_BACK, cmd=True)
    km.translate_key_event(evt)
    assert _state(evt) == (False, False, False, False, False, wx.WXK_DELETE)


def test_command_eight_is_the_communities_section_not_a_bookmark():
    evt = _key(ord("8"), cmd=True)
    km.translate_key_event(evt)
    assert _state(evt)[:2] == (False, True)
