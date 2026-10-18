"""Bridge WinZapp's screen-reader metadata to VoiceOver.

On macOS wxPython's wx.Accessible is a stub whose constructor raises
NotImplementedError, and SetAccessible() does nothing — the MSAA names,
descriptions and shortcuts WinZapp provides for NVDA/JAWS never reach
VoiceOver. Windows screen readers also label an unlabelled edit box or list
from the static text just before it; VoiceOver does not.

This module:
  * replaces wx.Accessible with an instantiable base class, so every
    upstream ``class X(wx.Accessible)`` works unchanged;
  * makes SetAccessible() remember the object and push its GetName /
    GetDescription / GetKeyboardShortcut / GetValue into the native
    NSView's accessibilityLabel / accessibilityHelp / accessibilityValue;
  * labels any still-unlabelled control from the wx.StaticText before it,
    as NVDA would;
  * refreshes those on focus, so names that depend on state (e.g. a
    playback position slider) are current when VoiceOver reads them.
"""

import logging

import wx

ACC_OK = getattr(wx, "ACC_OK", 0)
ACC_NOT_IMPLEMENTED = getattr(wx, "ACC_NOT_IMPLEMENTED", 1)

_OrigAccessible = wx.Accessible
_orig_set_accessible = wx.Window.SetAccessible
_orig_get_accessible = wx.Window.GetAccessible

_accessibles = {}   # id(window) -> accessible
KEY_FILTERS = []    # callables(wx.KeyEvent) run on every key event first
KEY_HANDLERS = []   # callables(wx.KeyEvent) -> True to consume, after KEY_FILTERS
TEXT_FILTERS = []   # callables(str) -> str applied to labels/help text


class MacAccessible:
    """Instantiable stand-in for wx.Accessible (MSAA-shaped API)."""

    def __init__(self, win=None):
        self._win = win

    def GetWindow(self):
        return self._win

    def SetWindow(self, win):
        self._win = win

    def GetName(self, childId):
        return (ACC_NOT_IMPLEMENTED, "")

    def GetDescription(self, childId):
        return (ACC_NOT_IMPLEMENTED, "")

    def GetHelpText(self, childId):
        return (ACC_NOT_IMPLEMENTED, "")

    def GetKeyboardShortcut(self, childId):
        return (ACC_NOT_IMPLEMENTED, "")

    def GetValue(self, childId):
        return (ACC_NOT_IMPLEMENTED, "")

    def GetRole(self, childId):
        return (ACC_NOT_IMPLEMENTED, 0)

    def GetState(self, childId):
        return (ACC_NOT_IMPLEMENTED, 0)

    @staticmethod
    def NotifyEvent(*args, **kwargs):
        pass


def _ns_views(win):
    """The NSView for *win* plus, for scroll-wrapped controls (list boxes,
    multi-line text), the document view VoiceOver actually lands on."""
    try:
        import objc
        handle = win.GetHandle()
        if not handle:
            return []
        view = objc.objc_object(c_void_p=handle)
    except Exception:
        return []
    views = [view]
    try:
        doc = view.documentView()
        if doc is not None:
            views.append(doc)
    except Exception:
        pass
    return views


def _result_text(result):
    if isinstance(result, tuple) and len(result) == 2 and result[0] == ACC_OK:
        return str(result[1] or "")
    return ""


def _apply(win):
    acc = _accessibles.get(id(win))
    label = help_text = value = ""
    if acc is not None:
        try:
            label = _result_text(acc.GetName(0))
            help_text = _result_text(acc.GetDescription(0))
            shortcut = _result_text(acc.GetKeyboardShortcut(0))
            if shortcut:
                help_text = f"{help_text} {shortcut}".strip()
            value = _result_text(acc.GetValue(0))
        except Exception:
            logging.debug("[mac-a11y] accessible callback failed", exc_info=True)
    static = _static_label_widget(win)
    static_text = _clean(static.GetLabel()) if static is not None else ""
    if not label:
        label = static_text
    for f in TEXT_FILTERS:
        label, help_text = f(label), f(help_text)
    # The visible label now names the control itself. On a Mac a label that
    # names a control is not a separate VoiceOver stop (NVDA skips it on
    # Windows the same way); leaving it in doubled "Main navigation, Main
    # navigation table". It stays on screen.
    if static is not None and static_text and label and \
            static_text.lower() in label.lower():
        _hide_from_voiceover(static)
    if not (label or help_text or value):
        return
    for view in _ns_views(win):
        try:
            if label:
                view.setAccessibilityLabel_(label)
            if help_text:
                view.setAccessibilityHelp_(help_text)
            if value and isinstance(win, wx.Slider):
                view.setAccessibilityValueDescription_(value)
        except Exception:
            pass


def _clean(text):
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&").strip().rstrip(":").strip()


_NEEDS_LABEL = (wx.TextCtrl, wx.ListBox, wx.Choice, wx.ComboBox, wx.Slider,
                wx.SpinCtrl, wx.SpinCtrlDouble, wx.Gauge, wx.TreeCtrl)


def _static_label_widget(win):
    """The wx.StaticText directly before *win* among its siblings (with no
    other control in between) — the label a Windows screen reader reads."""
    if not isinstance(win, _NEEDS_LABEL):
        return None
    parent = win.GetParent()
    if parent is None:
        return None
    prev = None
    for sib in parent.GetChildren():
        if sib is win:
            break
        if isinstance(sib, wx.StaticText):
            prev = sib
        elif sib.AcceptsFocus() or isinstance(sib, _NEEDS_LABEL):
            prev = None
    return prev


def _static_label_for(win):
    static = _static_label_widget(win)
    return _clean(static.GetLabel()) if static is not None else ""


def _hide_from_voiceover(static):
    """A wx.StaticText is an NSTextField whose *cell* is what VoiceOver
    reads (verified against the AX tree: flagging the view, or
    accessibilityHidden, leaves it visible)."""
    for view in _ns_views(static):
        try:
            cell = view.cell() if hasattr(view, "cell") else None
            if cell is not None:
                cell.setAccessibilityElement_(False)
        except Exception:
            pass


def _label_box(box):
    """wx.RadioBox / wx.StaticBox are NSBoxes: VoiceOver otherwise finds an
    unnamed group with its title as a stray text after the choices. Name
    the group with the title and hide the title cell."""
    title = _clean(box.GetLabel())
    if not title:
        return
    for f in TEXT_FILTERS:
        title = f(title)
    for view in _ns_views(box)[:1]:
        try:
            view.setAccessibilityLabel_(title)
            # The title reaches VoiceOver through a cell proxy among the
            # box's children; keep every child except that one.
            kids = list(view.accessibilityChildren() or [])
            # The box's content view shows up as an empty unnamed group
            # after the choices (they are reported directly under the box).
            keep = [k for k in kids if k.className() not in
                    ("NSAccessibilityReparentingCellProxy", "wxNSBoxContentView")]
            if len(keep) != len(kids):
                view.setAccessibilityChildren_(keep)
        except Exception:
            pass


def _label_tree(top):
    try:
        stack = [top]
        while stack:
            w = stack.pop()
            if isinstance(w, (wx.RadioBox, wx.StaticBox)):
                _label_box(w)
            if id(w) in _accessibles or isinstance(w, _NEEDS_LABEL):
                _apply(w)
            stack.extend(w.GetChildren())
    except Exception:
        logging.debug("[mac-a11y] label pass failed", exc_info=True)


def _set_accessible(self, accessible):
    if accessible is None:
        _accessibles.pop(id(self), None)
        return
    try:
        accessible.SetWindow(self)
    except Exception:
        pass
    _accessibles[id(self)] = accessible
    _apply(self)


def _get_accessible(self):
    return _accessibles.get(id(self))


class _Filter:
    """App-wide event filter: relabel a control as it takes focus, and label
    a whole window tree when a top-level window is shown."""

    @staticmethod
    def filter(evt):
        et = evt.GetEventType()
        if et in (wx.wxEVT_CHAR_HOOK, wx.wxEVT_KEY_DOWN):
            for f in KEY_FILTERS:
                f(evt)
            for handler in KEY_HANDLERS:
                if handler(evt):
                    return 1  # wx.Event_Processed
        elif et == wx.wxEVT_SET_FOCUS:
            win = evt.GetEventObject()
            if isinstance(win, wx.Window):
                _apply(win)
        elif et == wx.wxEVT_DESTROY:
            win = evt.GetEventObject()
            _accessibles.pop(id(win), None)
        elif et == wx.wxEVT_SHOW and getattr(evt, "IsShown", lambda: False)():
            win = evt.GetEventObject()
            if isinstance(win, wx.TopLevelWindow):
                wx.CallAfter(_label_tree, win)
        return -1  # wx.Event_Skip: let the event through untouched


def _install_filter():
    # FilterEvent must be defined on the App instance's class; WinZapp uses
    # plain wx.App, so patch the method onto wx.App itself.
    def FilterEvent(self, evt):
        try:
            return _Filter.filter(evt)
        except Exception:
            return -1

    wx.App.FilterEvent = FilterEvent
    orig_oninit = getattr(wx.App, "OnPreInit", None)

    def OnPreInit(self):
        if orig_oninit:
            orig_oninit(self)
        try:
            self.SetCallFilterEvent(True)
        except Exception:
            pass

    wx.App.OnPreInit = OnPreInit


def install():
    wx.Accessible = MacAccessible
    wx.Window.SetAccessible = _set_accessible
    wx.Window.GetAccessible = _get_accessible
    _install_filter()
