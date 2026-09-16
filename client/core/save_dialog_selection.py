"""
WinZapp - Save As dialog: keep the extension out of the initial selection
==========================================================================
Windows' modern Save dialog auto-completes the visible filename with the
extension from whichever file-type filter is active, and — measured, on a
real Windows 11 box, with screenshots — selects the WHOLE thing, extension
included, REGARDLESS of whether the extension was present in the
``defaultFile`` passed to ``wx.FileDialog``. Leaving the extension off the
suggested name (see the three call sites of ``schedule_deselect_extension``)
only changes what gets typed if the user retypes everything; it does not
change what the dialog highlights, because Windows still auto-completes it
into the box either way.

This is not cosmetic. A user who starts typing over the (fully selected)
suggestion to shorten or fix it wipes the extension along with the name —
harmless when the typed replacement has no dot of its own (Windows
re-appends the same extension from the active filter, confirmed live), but
wrong the moment the replacement DOES contain a dot (e.g. renaming a voice
message to something with a date in it, "16.9.2026") — WhatsApp's own
convention, and the one every other Explorer-style rename box and every
browser's own download dialog honours, is that the extension is never part
of what a plain "start typing" gesture overwrites. Screen-reader users are
the ones most exposed: they cannot see that the extension is highlighted,
and the discoverable, decades-old convention this dialog silently breaks is
exactly the one they rely on the most when editing a suggested name.

wx.FileDialog exposes no public API for this — SetDefaultExtension (the one
lever wx.FileDialog does drive from the wildcard, internally, correctly)
only governs what gets APPENDED when the user saves with no extension typed;
it has no effect on what is initially SELECTED. Chromium's own download
dialog reaches the identical native call sequence WITHOUT this problem
because it manages the edit control's selection itself, entirely outside
IFileDialog's public surface — reverse-engineered here in miniature via
pywin32 (already a WinZapp dependency, no new one added):

- The dialog's own window class is the classic, locale-independent
  "#32770" ("Dialog Box"), so matching on it (plus owning it — filtered by
  process id, since multiple accounts each run their own process, and
  requiring the window to actually contain a filename Edit control) never
  depends on the dialog's translated title.
- The filename box's own "Edit" child answers GetWindowText() with "" —
  measured — because it is a DirectUI-hosted preview element, not a plain
  Win32 edit control; there's nothing to match by text. It IS, reliably,
  the bottommost "Edit"-class child window in the dialog (the address bar's
  own Edit sits far above it), which is resolution/DPI independent because
  it compares window rects to each other, not to a hardcoded pixel value.
- EM_SETSEL sent straight to that control is silently ignored unless the
  dialog is genuinely the foreground window first (measured: identical,
  ignored, both with and without a preceding SetFocus() call — SetFocus()
  itself fails cross-process with access denied and isn't needed).
  SetForegroundWindow() first is what makes it take effect.
- UI Automation's TextPattern — the "proper", higher-level way to do this —
  is NOT implemented on this element (measured: GetCurrentPattern returns a
  null COM pointer), only ValuePattern is, so it cannot select a sub-range
  of the text either. That ruled out a comtypes/UIA-only implementation.

This is best-effort scaffolding on top of an undocumented, OS-owned window
hierarchy: every failure mode is swallowed and logged rather than raised,
because the file always saves with the correct extension regardless (via
SetDefaultExtension, confirmed working) — losing the cosmetic/selection fix
on some future Windows build must never take the actual save down with it.
"""

import logging
import os

import wx


def schedule_deselect_extension(base_name: str, delay_ms: int = 150) -> None:
    """Arrange for the Save dialog about to open to select only *base_name*
    once it appears, not the extension Windows auto-completes alongside it.

    Call this once, right before ``dlg.ShowModal()``, with the exact same
    string passed as that dialog's own ``defaultFile``. Does nothing outside
    a running wx.App or for an empty name.

    Also does nothing under pytest, checked explicitly (``PYTEST_CURRENT_TEST``,
    pytest's own marker for "a test is actually running") rather than left to
    the accident of "the test's mocked ShowModal() never pumps the event loop
    that would fire the CallLater anyway" — a suite-wide wx.App exists once
    any test requests the `wx_app` fixture (session-scoped), and this file's
    own tests import ui.conversations/status_panel/media_viewer functions
    directly, with no guarantee about what else that session's event loop
    processes before the process exits. The one thing this module must never
    do is touch a real window from inside a test run — the exact risk
    tests/conftest.py's hidden_frame()/wxgui-marker machinery exists to rule
    out everywhere else in this codebase.
    """
    if not base_name or wx.GetApp() is None:
        return
    if "PYTEST_CURRENT_TEST" in os.environ:
        return

    def _fix():
        try:
            _deselect_extension(base_name)
        except Exception:
            logging.exception("[SaveAs] could not adjust filename selection")

    wx.CallLater(delay_ms, _fix)


def _deselect_extension(base_name: str) -> None:
    import win32api
    import win32con
    import win32gui
    import win32process

    own_pid = win32api.GetCurrentProcessId()
    target = None

    def _enum_top(hwnd, _):
        nonlocal target
        if not win32gui.IsWindowVisible(hwnd):
            return True
        if win32gui.GetClassName(hwnd) != "#32770":
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid != own_pid:
            return True
        if _find_filename_edit(hwnd) is not None:
            target = hwnd
            return False  # stop enumerating, found our dialog
        return True

    win32gui.EnumWindows(_enum_top, None)
    if target is None:
        return

    edit_hwnd = _find_filename_edit(target)
    if edit_hwnd is None:
        return

    win32gui.SetForegroundWindow(target)
    win32gui.SendMessage(edit_hwnd, win32con.EM_SETSEL, 0, len(base_name))


def _find_filename_edit(dialog_hwnd):
    """The filename field, or None. See the module docstring: it's the
    bottommost "Edit"-class child of the dialog, identified by comparing
    child rects to each other rather than to a fixed pixel position."""
    import win32gui

    candidates = []

    def _cb(hwnd, _):
        if win32gui.GetClassName(hwnd) == "Edit":
            candidates.append((hwnd, win32gui.GetWindowRect(hwnd)))
        return True

    win32gui.EnumChildWindows(dialog_hwnd, _cb, None)
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[1][1])[0]
