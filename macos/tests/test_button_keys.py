"""A button's mnemonic never takes a WinZapp shortcut (button_keys_mac).
Frames are created but never Show()n (WinZapp rule 1)."""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [os.path.join(ROOT, "macos"), os.path.join(ROOT, "client"), os.path.join(ROOT, ".pydeps")]

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS layer")

import objc  # noqa: E402
import wx  # noqa: E402


@pytest.fixture(scope="module")
def app():
    from winzapp_mac import button_keys_mac
    button_keys_mac.install()   # only this part: a full install patches hotkeys etc. twice
    a = wx.App(False)
    yield a


@pytest.fixture
def frame(app):
    f = wx.Frame(None)          # never shown
    yield f
    f.Destroy()


def _key_equivalent(button):
    return str(objc.objc_object(c_void_p=button.GetHandle()).keyEquivalent())


def _key(code, alt=False, cmd=False, shift=False):
    evt = wx.KeyEvent(wx.wxEVT_KEY_DOWN)
    evt.SetKeyCode(code)
    evt.SetAltDown(alt)
    evt.SetControlDown(cmd)
    evt.SetShiftDown(shift)
    return evt


def everything_usable(_w):
    return True


# ----------------------------------------------------- native key equivalents

def test_a_mnemonic_never_becomes_a_command_key(frame):
    from winzapp_mac import button_keys_mac  # noqa: F401
    b = wx.Button(frame, label="&Read more")
    assert _key_equivalent(b) == ""          # wx alone gives it Command-R


def test_relabelling_does_not_bring_the_command_key_back(frame):
    b = wx.Button(frame, label="Plain")
    b.SetLabel("&Open")
    assert _key_equivalent(b) == ""
    b.SetLabelText("Save as")
    b.SetLabel("Save &as")                   # Command-A is select all
    assert _key_equivalent(b) == ""


def test_a_bitmap_button_and_a_two_step_button_are_cleared_too(frame):
    bb = wx.BitmapButton(frame, bitmap=wx.Bitmap(8, 8))
    bb.SetLabel("&Download")
    assert _key_equivalent(bb) == ""
    b = wx.Button()
    b.Create(frame, label="&Video call")
    assert _key_equivalent(b) == ""


def test_installing_twice_wraps_once(app):
    from winzapp_mac import button_keys_mac
    before = wx.Button.SetLabel
    button_keys_mac.install()
    assert wx.Button.SetLabel is before


def test_the_default_buttons_return_key_is_kept(frame):
    b = wx.Button(frame, label="&Send")
    b.SetDefault()
    assert _key_equivalent(b) == "\r"


# ------------------------------------------------------------- mnemonics --

def test_mnemonic_letter():
    from winzapp_mac.button_keys_mac import mnemonic
    assert mnemonic("&Read more") == "r"
    assert mnemonic("Save &as") == "a"
    assert mnemonic("Tom && Jerry") is None
    assert mnemonic("Tom && &Jerry") == "j"
    assert mnemonic("Plain") is None
    assert mnemonic("trailing&") is None


def test_one_button_with_the_letter_is_clicked(frame):
    from winzapp_mac.button_keys_mac import plan
    read_more = wx.Button(frame, label="&Read more")
    other = wx.Button(frame, label="&Open")
    windows = [read_more, other]
    assert plan(windows, "r", None, usable=everything_usable) == ("click", read_more)
    assert plan(windows, "x", None, usable=everything_usable) is None


def test_a_hidden_or_disabled_button_is_not_pressed(frame):
    from winzapp_mac.button_keys_mac import plan
    read_more = wx.Button(frame, label="&Read more")
    assert plan([read_more], "r", None, usable=lambda w: False) is None


def test_several_controls_with_the_letter_move_focus_to_the_next_one(frame):
    from winzapp_mac.button_keys_mac import plan
    a = wx.Button(frame, label="&Reply")
    b = wx.Button(frame, label="&Read more")
    windows = [a, b]
    assert plan(windows, "r", None, usable=everything_usable) == ("focus", a)
    assert plan(windows, "r", a, usable=everything_usable) == ("focus", b)
    assert plan(windows, "r", b, usable=everything_usable) == ("focus", a)   # wraps


def test_a_labels_mnemonic_focuses_the_control_after_it(frame):
    from winzapp_mac.button_keys_mac import plan
    label = wx.StaticText(frame, label="&Message:")
    field = wx.TextCtrl(frame)
    assert plan([label, field], "m", None, usable=everything_usable) == ("focus", field)


def test_pressing_a_button_sends_its_click(frame):
    from winzapp_mac.button_keys_mac import press
    b = wx.Button(frame, label="&Read more")
    clicks = []
    b.Bind(wx.EVT_BUTTON, lambda e: clicks.append(e.GetEventObject()))
    press(b)
    assert clicks == [b]


def test_pressing_a_check_box_toggles_it(frame):
    from winzapp_mac.button_keys_mac import press
    cb = wx.CheckBox(frame, label="&Mute")
    seen = []
    cb.Bind(wx.EVT_CHECKBOX, lambda e: seen.append(e.IsChecked()))
    press(cb)
    assert cb.GetValue() and seen == [True]


# ------------------------------------------------------------- the keys --

def test_command_letter_is_never_a_mnemonic(app):
    # Command-R reaches WinZapp as Ctrl+R (record) whatever buttons are on screen.
    from winzapp_mac.button_keys_mac import handle_key_down
    assert handle_key_down(_key(ord("R"), cmd=True)) is False
    assert handle_key_down(_key(ord("R"))) is False
    assert handle_key_down(_key(ord("R"), alt=True, shift=True)) is False


def test_a_handled_mnemonic_stops_the_key_in_the_app_filter(app, monkeypatch):
    from winzapp_mac import accessibility_mac
    monkeypatch.setattr(accessibility_mac, "KEY_FILTERS", [])
    monkeypatch.setattr(accessibility_mac, "KEY_HANDLERS", [lambda e: True])
    assert accessibility_mac._Filter.filter(_key(ord("R"), alt=True)) == 1
    monkeypatch.setattr(accessibility_mac, "KEY_HANDLERS", [lambda e: False])
    assert accessibility_mac._Filter.filter(_key(ord("R"), alt=True)) == -1
