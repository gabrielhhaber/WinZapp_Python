"""Suppress a screen reader's spoken focus announcement for ONE programmatic
focus move — at the source, before it can ever become speech.

Why this exists
---------------
Settings > Conteúdo Falado's "silence while recording a voice message" used to
be implemented purely as ``AccessibleSpeechOutput.silence_screen_reader_focus()``
fired right after ``SetFocus()``: cancel whatever the screen reader is saying.
That is inherently a race, and it is the wrong side of the race. Windows fires
``EVENT_OBJECT_FOCUS`` synchronously, but NVDA reads and speaks it on its own
thread a moment later, so a cancel issued at 0 ms cancels nothing (nothing is
queued yet) and one issued 80 ms later arrives after speech has already begun.
The audible result was the whole phrase — "enviar mensagem de voz, botão,
Ctrl+R" — clipped part-way. For a user recording for radio or production that
is exactly the failure the setting was supposed to prevent.

Blanking the control's accessible name was tried before this and removed: it
strips the button's identity from the accessibility tree for every consumer,
not just from the announcement we wanted gone. ``wx.Window.SetName()`` does not
even reach MSAA — it sets wx's internal window name, not the accessible name —
which is why it appeared to do nothing at all.

How it actually works
---------------------
NVDA decides whether a focus event is worth speaking *before* it speaks it, in
``IAccessibleHandler.processFocusNVDAEvent()``::

    if not obj.shouldAllowIAccessibleFocusEvent:
        return False        # event dropped, never queued, never spoken

and ``IAccessible._get_shouldAllowIAccessibleFocusEvent()`` answers by walking
the object and its ancestors looking for ``State.FOCUSED``; if none of them
reports it, the event is discarded. There is no later re-sync that would
recover it: NVDA never polls the system focus, it only reacts to events (the
one "fake focus" path in ``IAccessibleHandler.pumpAll()`` fires from menu and
task-switch events only).

So the control briefly reports its MSAA state *without*
``STATE_SYSTEM_FOCUSED``. NVDA drops the event outright — nothing is spoken, so
there is nothing left to cancel and no fragment to clip.

wxWidgets makes that reachable: a ``wx.Accessible`` attached to a window answers
``WM_GETOBJECT`` for it, and returning ``wx.ACC_NOT_IMPLEMENTED`` from any
method falls straight back to the standard MSAA implementation. That is the
whole reason the cloak is safe — outside the brief armed window the control is
byte-for-byte as accessible as it was before, because the override answers
"not implemented" and Windows' own object is used. Verified against oleacc:
uncloaked a focused wx.Button reports ``0x100104`` (FOCUSABLE|DEFAULT|FOCUSED),
cloaked it reports ``0x100000`` (FOCUSABLE only).

Two deliberate limits
---------------------
* The cloak is **armed for a few hundred milliseconds, not for the whole
  recording.** It exists to hide the focus move *WinZapp itself* performs. If
  the user then presses Tab, that is their own navigation and it must be
  announced normally — silence there would be far worse than the noise this
  module removes.
* The accessible object is installed **once per window and then reused**,
  toggled by a plain Python flag. ``SetAccessible()`` transfers ownership of the
  object to C++, so installing and removing one repeatedly is a lifetime hazard
  for no benefit.

This complements, and does not replace, the ``silence()`` burst at the call
sites: if the platform ever declines to route ``WM_GETOBJECT`` through wx, or
the screen reader reads the control over UIA rather than MSAA, the cancel path
is still there as the weaker fallback it always was.
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
