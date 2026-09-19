"""Reusable MSAA helper for suppressing one programmatic focus event.

This helper briefly removes ``STATE_SYSTEM_FOCUSED`` from a wx control's MSAA
state. NVDA's IAccessible path can then discard the corresponding focus event
before speech is queued. It is useful when a focus move is unavoidable.

It is intentionally **not** the voice-recording start mechanism anymore. NVDA
can receive focus through UIA as well as MSAA, and cancelling speech after
``SetFocus()`` is racy (the user-visible symptom was the clipped ``"env..."``
from the Send button). Voice recording now avoids the synthetic Send/Discard
focus move entirely whenever recording-focus suppression is requested.

Outside the short armed interval the accessible object answers
``wx.ACC_NOT_IMPLEMENTED``, so wx/Windows supplies the normal accessibility
state. The object is installed once per window and reused because
``SetAccessible()`` transfers ownership to C++.
"""

import logging

import wx

# Where the cloak object is parked on its window. Keeping our own reference
# also guarantees the Python wrapper outlives the C++ object's use.
_CLOAK_ATTR = "_winzapp_focus_cloak"

# Long enough for NVDA to have pumped and processed the focus event even on a
# loaded machine (its event pump runs every few tens of milliseconds), short
# enough that a Tab the user presses immediately afterwards is still announced.
DEFAULT_CLOAK_MS = 500


class FocusCloakAccessible(wx.Accessible):
    """MSAA shim that can hide ``STATE_SYSTEM_FOCUSED`` on demand.

    While ``cloaked`` is False every query answers ``wx.ACC_NOT_IMPLEMENTED``,
    which makes wxWidgets fall back to the standard system implementation — the
    control behaves exactly as if this object were not installed.
    """

    def __init__(self, window):
        super().__init__(window)
        self.cloaked = False

    def GetState(self, childId):
        # childId 0 is CHILDID_SELF. A wx.Button has no MSAA children of its
        # own, but answering for them would be wrong regardless: the cloak is
        # about the control that just took focus, nothing else.
        if not self.cloaked or childId != 0:
            return (wx.ACC_NOT_IMPLEMENTED, 0)
        # Focusable, but explicitly not focused. Reporting the control as
        # unavailable or invisible instead would also drop the announcement,
        # but it lies about the control to every other consumer; "not the
        # focus" is the single fact we are actually suppressing.
        return (wx.ACC_OK, wx.ACC_STATE_SYSTEM_FOCUSABLE)


def _get_or_install_cloak(window):
    """Return the window's cloak, installing it the first time."""
    cloak = getattr(window, _CLOAK_ATTR, None)
    if cloak is not None:
        return cloak
    cloak = FocusCloakAccessible(window)
    window.SetAccessible(cloak)
    setattr(window, _CLOAK_ATTR, cloak)
    return cloak


def cloak_focus_announcement(window, duration_ms=DEFAULT_CLOAK_MS):
    """Arm the cloak on ``window`` so the next focus event on it is dropped.

    Must be called *before* ``window.SetFocus()`` — the state has to already be
    hiding FOCUSED by the time the screen reader reads it back.

    Returns True if the cloak was armed. Every failure path is non-fatal and
    returns False: the caller's ``silence()`` fallback still runs, and a focus
    move that gets announced is a far better outcome than a crash on the way to
    recording a voice message.
    """
    try:
        cloak = _get_or_install_cloak(window)
        cloak.cloaked = True
    except Exception:
        logging.debug("[focus_cloak] could not arm the cloak", exc_info=True)
        return False

    def _uncloak():
        try:
            cloak.cloaked = False
        except Exception:
            pass

    try:
        wx.CallLater(max(0, int(duration_ms)), _uncloak)
    except Exception:
        # No timer means no automatic disarm, which would leave the control
        # permanently unannounceable. Undo rather than leave it armed.
        _uncloak()
        return False
    return True



# Where the transient parent-panel role cloak is parked.
_PANEL_CLOAK_ATTR = "_winzapp_panel_focus_cloak"


class PanelFocusCloakAccessible(wx.Accessible):
    """Make one automatic panel-focus fallback silent to NVDA.

    wx.Panel normally exposes the MSAA PANEL role. NVDA deliberately does
    not suppress that role on focus, while its PANE role is listed in
    controlTypes.silentRolesOnFocus. When wx hides the currently focused
    recording trigger, Windows can focus the parent panel automatically; that
    is the source of the otherwise isolated "Panel" announcement.

    While armed, expose this structural container as an unnamed PANE.
    Focus still exists, so keyboard accelerators and subsequent Tab navigation
    keep working. Outside the short armed interval every method falls back to
    wx's standard accessible object.
    """

    def __init__(self, window):
        super().__init__(window)
        self.cloaked = False

    def GetRole(self, childId):
        if not self.cloaked or childId != 0:
            return (wx.ACC_NOT_IMPLEMENTED, wx.ROLE_NONE)
        return (wx.ACC_OK, wx.ROLE_SYSTEM_PANE)

    def GetName(self, childId):
        if not self.cloaked or childId != 0:
            return (wx.ACC_NOT_IMPLEMENTED, "")
        return (wx.ACC_OK, "")

    def GetDescription(self, childId):
        if not self.cloaked or childId != 0:
            return (wx.ACC_NOT_IMPLEMENTED, "")
        return (wx.ACC_OK, "")


def _get_or_install_panel_cloak(window):
    cloak = getattr(window, _PANEL_CLOAK_ATTR, None)
    if cloak is not None:
        return cloak
    cloak = PanelFocusCloakAccessible(window)
    window.SetAccessible(cloak)
    setattr(window, _PANEL_CLOAK_ATTR, cloak)
    return cloak


def _focus_is_within_any(focus, windows):
    """Return True when focus is one of windows or below one of them."""
    current = focus
    while current is not None:
        if any(current is window for window in windows if window is not None):
            return True
        try:
            current = current.GetParent()
        except Exception:
            return False
    return False


def cloak_panel_focus_fallback(panel, *about_to_hide, duration_ms=DEFAULT_CLOAK_MS):
    """Silence the parent-panel focus produced by hiding a focused child.

    Nothing is armed unless the current focus is actually inside one of the
    controls that is about to be hidden. This means Ctrl+R from a control that
    remains visible keeps its existing focus untouched; only the automatic
    wx/Windows fallback caused by the UI swap is affected.
    """
    try:
        focus = wx.Window.FindFocus()
        if focus is None or not _focus_is_within_any(focus, about_to_hide):
            return False
        cloak = _get_or_install_panel_cloak(panel)
        cloak.cloaked = True
    except Exception:
        logging.debug(
            "[focus_cloak] could not arm parent-panel fallback cloak",
            exc_info=True,
        )
        return False

    def _uncloak():
        try:
            cloak.cloaked = False
        except Exception:
            pass

    try:
        wx.CallLater(max(0, int(duration_ms)), _uncloak)
    except Exception:
        _uncloak()
        return False
    return True

def uncloak_focus_announcement(window):
    """Disarm the cloak on ``window`` immediately, if it has one."""
    try:
        cloak = getattr(window, _CLOAK_ATTR, None)
    except Exception:
        return
    if cloak is not None:
        try:
            cloak.cloaked = False
        except Exception:
            pass
