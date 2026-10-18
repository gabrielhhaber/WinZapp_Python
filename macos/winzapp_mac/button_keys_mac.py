"""A button's underlined letter never takes a WinZapp shortcut.

wxOSX turns a button label's mnemonic ("&Read more") into the native
button's key equivalent with the bare Command modifier
(wxButtonCocoaImpl::SetAcceleratorFromLabel, src/osx/cocoa/button.mm), and
re-does it on every SetLabel. AppKit offers a key equivalent to the views
before wx sees the key at all, so while "Read more" was on screen Command-R
pressed it instead of recording a voice message, a media "Save &as" button
took Command-A, and so on: a shortcut did something different depending on
which buttons the screen happened to show.

On Windows a mnemonic is Alt+letter, and WinZapp's handlers and accelerator
tables get the key before the dialog's mnemonics do. Both halves are
reproduced here:

  * every wx.Button's native key equivalent is cleared after wx sets it
    (Return/Escape, the default and cancel buttons' keys, are kept);
  * a mnemonic is pressed by the Mac form of Alt+letter, Command-Option-letter
    (keymap_mac rewrites it into Alt+letter), from the wxEVT_KEY_DOWN that wx
    sends only after wxEVT_CHAR_HOOK handlers and accelerator tables have
    passed on the key (wxWindowMac::OSXHandleKeyEvent, 3.2.8) - the point
    where Windows' IsDialogMessage runs. A shortcut WinZapp binds to the same
    Alt+letter therefore still wins, as on Windows.

As IsDialogMessage does: a button is clicked without moving focus; a check
box, radio button or toggle button gets focus and is toggled; a label's
mnemonic focuses the next control after it; when several visible controls
share the letter, focus moves to the next of them and nothing is pressed.
"""

import logging

import objc
import wx

_KEEP = {"\r", "\x1b"}       # default button's Return, cancel's Escape
_CLICKABLE = (wx.Button, wx.ToggleButton, wx.CheckBox, wx.RadioButton)


def mnemonic(label):
    """The lower-case mnemonic letter of a wx label, or None ("&&" is a
    literal ampersand)."""
    i = 0
    while i < len(label) - 1:
        if label[i] == "&":
            if label[i + 1] != "&":
                return label[i + 1].lower()
            i += 1
        i += 1
    return None


def clear_native_key(button):
    """Drop the bare-Command key equivalent wx gave *button*."""
    try:
        ns = objc.objc_object(c_void_p=button.GetHandle())
        if str(ns.keyEquivalent()) not in _KEEP:
            ns.setKeyEquivalent_("")
    except Exception:
        logging.debug("[mac-keys] clearing a button key failed", exc_info=True)


def _wrap(cls, name):
    orig = getattr(cls, name)
    if getattr(orig, "_wz_clears_key", False):
        return

    def wrapped(self, *args, **kwargs):
        result = orig(self, *args, **kwargs)
        clear_native_key(self)
        return result

    wrapped.__name__ = name
    wrapped._wz_clears_key = True
    setattr(cls, name, wrapped)


# ------------------------------------------------------------- pressing --

def _walk(top):
    """Every window under *top* in tab order (depth first)."""
    out = []
    stack = list(reversed(top.GetChildren()))
    while stack:
        w = stack.pop()
        out.append(w)
        stack.extend(reversed(w.GetChildren()))
    return out


def _usable(w):
    return w.IsShownOnScreen() and w.IsEnabled()


def plan(windows, letter, focused, usable=_usable, label=lambda w: w.GetLabel()):
    """What Alt+*letter* does among *windows* (tab order):
    ("click", w), ("focus", w) or None when no control has that mnemonic."""
    hits = [w for w in windows
            if isinstance(w, _CLICKABLE + (wx.StaticText,)) and usable(w)
            and mnemonic(label(w)) == letter]
    if not hits:
        return None
    if len(hits) > 1:
        after = [w for w in hits if focused in windows and windows.index(w) > windows.index(focused)]
        target = (after or hits)[0]
        if isinstance(target, wx.StaticText):
            target = _next_focusable(windows, target, usable)
        return ("focus", target) if target is not None else None
    w = hits[0]
    if isinstance(w, wx.StaticText):
        target = _next_focusable(windows, w, usable)
        return ("focus", target) if target is not None else None
    return ("click", w)


def _next_focusable(windows, label, usable):
    for w in windows[windows.index(label) + 1:]:
        if not isinstance(w, (wx.StaticText, wx.Panel, wx.StaticBox)) and usable(w) \
                and w.AcceptsFocus():
            return w
    return None


def _send(w, evt_type, **setters):
    evt = wx.CommandEvent(evt_type, w.GetId())
    evt.SetEventObject(w)
    for name, value in setters.items():
        getattr(evt, name)(value)
    w.GetEventHandler().ProcessEvent(evt)


def press(w):
    if isinstance(w, wx.CheckBox):
        w.SetFocus()
        w.SetValue(not w.GetValue())
        _send(w, wx.wxEVT_CHECKBOX, SetInt=int(w.GetValue()))
    elif isinstance(w, wx.RadioButton):
        w.SetFocus()
        w.SetValue(True)
        _send(w, wx.wxEVT_RADIOBUTTON, SetInt=1)
    elif isinstance(w, wx.ToggleButton):
        w.SetFocus()
        w.SetValue(not w.GetValue())
        _send(w, wx.wxEVT_TOGGLEBUTTON, SetInt=int(w.GetValue()))
    else:
        _send(w, wx.wxEVT_BUTTON)


def handle_key_down(evt):
    """KEY_HANDLERS hook: True when the key pressed a mnemonic."""
    if evt.GetEventType() != wx.wxEVT_KEY_DOWN:
        return False
    if not evt.AltDown() or evt.ControlDown() or evt.ShiftDown() or evt.RawControlDown():
        return False
    code = evt.GetKeyCode()
    if not (ord("A") <= code <= ord("Z") or ord("0") <= code <= ord("9")):
        return False
    focused = wx.Window.FindFocus()
    top = wx.GetTopLevelParent(focused) if focused is not None else None
    if top is None:
        return False
    action = plan(_walk(top), chr(code).lower(), focused)
    if action is None:
        return False
    kind, w = action
    if kind == "focus":
        w.SetFocus()
    else:
        press(w)
    return True


def install():
    for cls in (wx.Button, wx.BitmapButton):
        for name in ("__init__", "Create", "SetLabel", "SetLabelText", "SetLabelMarkup"):
            if name in vars(cls) or (cls is wx.Button and hasattr(cls, name)):
                _wrap(cls, name)
    from . import accessibility_mac
    if handle_key_down not in accessibility_mac.KEY_HANDLERS:
        accessibility_mac.KEY_HANDLERS.append(handle_key_down)
