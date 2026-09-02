"""Tests for core.focus_cloak — suppressing the screen reader's focus
announcement when a voice recording starts.

The point of the module is a very specific claim about the platform: that a
wx.Accessible attached to a native wx.Button really does answer MSAA's
WM_GETOBJECT, and that returning a state without STATE_SYSTEM_FOCUSED is
therefore visible to a screen reader. NVDA drops a focus event whose object
(and none of its ancestors) reports FOCUSED — IAccessibleHandler
.processFocusNVDAEvent -> IAccessible._get_shouldAllowIAccessibleFocusEvent —
so that is the whole mechanism, and it is worth verifying against the real
oleacc rather than only against our own Python.

None of this opens a window on the desktop: the frames come from
tests.conftest.hidden_frame() and are never shown. MSAA answers for an unshown
window just fine.
"""

import ctypes
from ctypes import wintypes as wt

import pytest

import wx

from core.focus_cloak import (
    FocusCloakAccessible,
    cloak_focus_announcement,
    uncloak_focus_announcement,
)
from tests.conftest import hidden_frame

# Real MSAA bits, deliberately spelled out rather than taken from wx: wx has
# its own wxACC_STATE_SYSTEM_* enum with different numeric values and
# translates on the way out, so asserting against wx's constants would not
# prove what a screen reader actually receives.
MSAA_STATE_SYSTEM_FOCUSED = 0x00000004
MSAA_STATE_SYSTEM_FOCUSABLE = 0x00100000
OBJID_CLIENT = 0xFFFFFFFC


class _VARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("r1", ctypes.c_ushort),
        ("r2", ctypes.c_ushort),
        ("r3", ctypes.c_ushort),
        ("val", ctypes.c_longlong),
        ("pad", ctypes.c_longlong),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [
        ("d1", ctypes.c_ulong),
        ("d2", ctypes.c_ushort),
        ("d3", ctypes.c_ushort),
        ("d4", ctypes.c_ubyte * 8),
    ]


# IID_IAccessible {618736E0-3C3D-11CF-810C-00AA00389B71}
_IID_IACCESSIBLE = _GUID(
    0x618736E0,
    0x3C3D,
    0x11CF,
    (ctypes.c_ubyte * 8)(0x81, 0x0C, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71),
)

# IAccessible::get_accState is slot 14: 7 IUnknown+IDispatch slots, then
# accParent, accChildCount, accChild, accName, accValue, accDescription,
# accRole.
_GET_ACC_STATE_SLOT = 14


def _msaa_state(hwnd):
    """Read a window's MSAA state the way a screen reader would."""
    obj = ctypes.c_void_p()
    hr = ctypes.windll.oleacc.AccessibleObjectFromWindow(
        wt.HWND(hwnd),
        ctypes.c_ulong(OBJID_CLIENT),
        ctypes.byref(_IID_IACCESSIBLE),
        ctypes.byref(obj),
    )
    assert hr == 0 and obj, "AccessibleObjectFromWindow failed: %#x" % (hr & 0xFFFFFFFF)
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
    proto = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, _VARIANT, ctypes.POINTER(_VARIANT)
    )
    get_acc_state = proto(vtable.contents[_GET_ACC_STATE_SLOT])
    child = _VARIANT()
    child.vt = 3  # VT_I4
    child.val = 0  # CHILDID_SELF
    out = _VARIANT()
    assert get_acc_state(obj, child, ctypes.byref(out)) == 0
    return int(out.val)


@pytest.fixture
def button(wx_app):
    """A real, never-shown wx.Button with a live HWND."""
    frame = hidden_frame()
    panel = wx.Panel(frame)
    btn = wx.Button(panel, label="Enviar mensagem de voz")
    yield btn
    frame.Destroy()


# ── The accessible itself ────────────────────────────────────────────────────


def test_uncloaked_defers_to_the_standard_implementation(button):
    """Not cloaked must mean "as if this object were not installed" — that is
    what keeps the button fully accessible outside the armed window."""
    cloak = FocusCloakAccessible(button)
    assert cloak.GetState(0) == (wx.ACC_NOT_IMPLEMENTED, 0)


def test_cloaked_reports_focusable_but_not_focused(button):
    cloak = FocusCloakAccessible(button)
    cloak.cloaked = True
    status, state = cloak.GetState(0)
    assert status == wx.ACC_OK
    assert state == wx.ACC_STATE_SYSTEM_FOCUSABLE
    assert not state & wx.ACC_STATE_SYSTEM_FOCUSED


def test_cloak_only_covers_childid_self(button):
    """The cloak is about the control that took focus, nothing beneath it."""
    cloak = FocusCloakAccessible(button)
    cloak.cloaked = True
    assert cloak.GetState(1) == (wx.ACC_NOT_IMPLEMENTED, 0)


# ── The claim that actually matters: what MSAA reports ───────────────────────


def test_cloak_removes_the_focused_bit_from_the_real_msaa_state(button, monkeypatch):
    """End to end through oleacc, which is the path NVDA takes.

    Without this the module could pass every other test and still do nothing:
    the whole design rests on wxWidgets routing WM_GETOBJECT through our
    wx.Accessible for a *native* Win32 BUTTON.
    """
    timers = []
    monkeypatch.setattr(
        wx, "CallLater", lambda ms, fn, *a, **kw: timers.append(fn) or None
    )

    hwnd = button.GetHandle()
    baseline = _msaa_state(hwnd)
    assert baseline & MSAA_STATE_SYSTEM_FOCUSABLE, "a wx.Button should be focusable"

    assert cloak_focus_announcement(button) is True
    cloaked = _msaa_state(hwnd)
    assert not cloaked & MSAA_STATE_SYSTEM_FOCUSED
    assert cloaked & MSAA_STATE_SYSTEM_FOCUSABLE

    # And the standard implementation comes back once disarmed.
    assert timers, "the cloak must schedule its own disarm"
    for disarm in timers:
        disarm()
    assert _msaa_state(hwnd) == baseline


# ── Arming / disarming ───────────────────────────────────────────────────────


def test_the_accessible_is_installed_once_and_reused(button, monkeypatch):
    """SetAccessible() hands ownership to C++; installing a fresh object on
    every recording would be a lifetime hazard for no benefit."""
    monkeypatch.setattr(wx, "CallLater", lambda ms, fn, *a, **kw: None)

    cloak_focus_announcement(button)
    first = button._winzapp_focus_cloak
    cloak_focus_announcement(button)
    assert button._winzapp_focus_cloak is first


def test_the_cloak_disarms_itself(button, monkeypatch):
    """A cloak left armed would make the control permanently unannounceable —
    strictly worse than the noise it removes."""
    scheduled = []
    monkeypatch.setattr(
        wx, "CallLater", lambda ms, fn, *a, **kw: scheduled.append((ms, fn)) or None
    )

    cloak_focus_announcement(button, duration_ms=500)
    cloak = button._winzapp_focus_cloak
    assert cloak.cloaked is True
    assert [ms for ms, _ in scheduled] == [500]

    scheduled[0][1]()
    assert cloak.cloaked is False


def test_uncloak_is_safe_on_a_window_that_was_never_cloaked(button):
    uncloak_focus_announcement(button)  # must not raise


def test_uncloak_disarms_immediately(button, monkeypatch):
    monkeypatch.setattr(wx, "CallLater", lambda ms, fn, *a, **kw: None)
    cloak_focus_announcement(button)
    uncloak_focus_announcement(button)
    assert button._winzapp_focus_cloak.cloaked is False


def test_a_failure_to_arm_is_reported_not_raised(button, monkeypatch):
    """Recording a voice message must never fail because the accessibility
    shim could not be installed — the silence() fallback still runs."""
    monkeypatch.setattr(
        wx.Window, "SetAccessible", lambda self, acc: (_ for _ in ()).throw(RuntimeError)
    )
    assert cloak_focus_announcement(button) is False


def test_no_timer_means_the_cloak_is_undone_rather_than_left_armed(button, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("no timers here")

    monkeypatch.setattr(wx, "CallLater", _boom)
    assert cloak_focus_announcement(button) is False
    assert button._winzapp_focus_cloak.cloaked is False


# ── The panels' use of it ────────────────────────────────────────────────────


class _PanelStub:
    """ConversationsPanel/StatusPanel are wx classes that cannot be built
    without a full app, so the methods under test are bound to a stub carrying
    only what they touch — the pattern the rest of this suite uses."""

    def __init__(self, silence_while_recording):
        self.main_window = type(
            "MW",
            (),
            {
                "settings": {
                    "speech_content": {
                        "silence_while_recording": silence_while_recording
                    }
                },
                "speak_output": None,
            },
        )()
        self.focused = None

    def _focus(self):
        self.focused = True


class _FakeButton:
    def __init__(self):
        self.focused = False

    def SetFocus(self):
        self.focused = True


@pytest.mark.parametrize("panel_module", ["ui.conversations", "status_panel"])
@pytest.mark.parametrize("enabled", [True, False])
def test_focus_helper_arms_the_cloak_only_when_the_setting_is_on(
    panel_module, enabled, monkeypatch
):
    """Both panels carry their own copy of this; both must key on the same
    single toggle. StatusPanel's copy used to also fire when
    extended_sr_compat_enabled was off — i.e. it interrupted the screen reader
    of a user who had asked WinZapp never to speak to it."""
    import importlib

    module = importlib.import_module(panel_module)
    panel_cls = (
        module.ConversationsPanel if panel_module == "ui.conversations" else module.StatusPanel
    )

    armed = []
    monkeypatch.setattr(module, "cloak_focus_announcement", lambda w: armed.append(w))

    stub = _PanelStub(enabled)
    stub._voice_recording_silence_enabled = (
        panel_cls._voice_recording_silence_enabled.__get__(stub)
    )
    stub._silence_send_voice_focus_if_enabled = lambda: None

    btn = _FakeButton()
    panel_cls._focus_recording_button_silently(stub, btn)

    assert btn.focused, "focus must move regardless of the setting"
    assert armed == ([btn] if enabled else [])
