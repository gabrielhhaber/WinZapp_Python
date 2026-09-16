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

Two more things were measured after the above shipped and a real user hit
them on a freshly-launched install:

- ``ShowModal()``'s own call into ``IFileDialog::Show()`` does not return
  control to wx's event loop — so no scheduled ``wx.CallLater`` can fire at
  all — until the dialog has finished populating itself: measured at
  500-900ms, cold or warm, both with an invisible test frame and with a
  real, already-visible one. A single fixed-delay attempt shorter than that
  (the first shipped version used 150ms) never gets a real chance to run;
  it was pure luck that it ever appeared to work.
- Plain ``SetForegroundWindow()`` on the dialog is not reliable the first
  time a freshly-started process calls it: Windows' foreground-lock rules
  can silently refuse it (the call "succeeds" but the window never actually
  becomes foreground), which is enough on its own for the following
  EM_SETSEL to be ignored per the point above — and it explains the exact
  pattern reported live: the very first Save As after launching WinZapp
  showed everything selected, extension included, and every one after that
  (any file type, any message, any conversation) was correct. Not
  timing-in-general, specifically this process's first attempt to steal
  the foreground. The fix is the standard ``AttachThreadInput`` dance (see
  ``_force_foreground``), which lets our thread borrow the currently
  active thread's input-processing rights before asking for the
  foreground, instead of asking cold.

Both are handled the same way: don't assume either one worked, check, and
retry on a short interval — proven empirically (3 separate fresh processes,
each simulating "first Save As since launch", screenshots and logged
attempt counts) to succeed within 1-2 retries (well under 100ms of actual
polling) every single time, cold or warm.

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

_RETRY_INTERVAL_MS = 20
_MAX_ATTEMPTS = 150  # ~3s ceiling at the interval above — generous; measured
                     # success is within 1-2 attempts, cold or warm.


def schedule_deselect_extension(base_name: str) -> None:
    """Arrange for the Save dialog about to open to select only *base_name*
    once it appears, not the extension Windows auto-completes alongside it.

    Call this once, right before ``dlg.ShowModal()``, with the exact same
    string passed as that dialog's own ``defaultFile``. Does nothing outside
    a running wx.App or for an empty name.

    Retries on a short interval rather than trying once: see the module
    docstring for why a single attempt — at any delay — is not reliable,
    measured live on both counts (the dialog not being ready yet, and the
    first SetForegroundWindow of a freshly-launched process being silently
    refused).

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

    attempts_left = [_MAX_ATTEMPTS]

    def _attempt():
        try:
            done = _deselect_extension(base_name)
        except Exception:
            logging.exception("[SaveAs] could not adjust filename selection")
            return
        if done:
            return
        attempts_left[0] -= 1
        if attempts_left[0] > 0:
            wx.CallLater(_RETRY_INTERVAL_MS, _attempt)

    wx.CallLater(_RETRY_INTERVAL_MS, _attempt)


def _deselect_extension(base_name: str) -> bool:
    """One attempt. Returns True once the selection was actually applied to
    a confirmed-foreground dialog, False when it's worth retrying (dialog or
    its filename field not there yet, or the foreground switch wasn't
    confirmed) — never raises for either of those, only for something truly
    unexpected, which the caller logs and gives up on."""
    import win32con
    import win32gui

    target = _find_our_dialog()
    if target is None:
        return False

    edit_hwnd = _find_filename_edit(target)
    if edit_hwnd is None:
        return False

    if not _force_foreground(target):
        return False

    win32gui.SendMessage(edit_hwnd, win32con.EM_SETSEL, 0, len(base_name))
    return True


def _find_our_dialog():
    """The Save dialog's own HWND, or None. Matched by window class
    ("#32770" — the classic, locale-independent "Dialog Box" class, so this
    never depends on the dialog's translated title), owned by this same
    process (each account runs its own process), and actually containing a
    filename Edit control — never just the first "#32770" window found,
    since a wx.MessageBox uses the identical class."""
    import win32api
    import win32gui
    import win32process

    own_pid = win32api.GetCurrentProcessId()
    target = None

    def _cb(hwnd, _):
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
            return False  # stop enumerating, found it
        return True

    win32gui.EnumWindows(_cb, None)
    return target


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


def _force_foreground(hwnd) -> bool:
    """SetForegroundWindow(), confirmed — not just called.

    Windows refuses a bare SetForegroundWindow() from a process that hasn't
    itself just received input, and a freshly-launched process's first-ever
    call is exactly that case (measured: this is what made the very first
    Save As after launching WinZapp behave differently from every one
    after). Temporarily attaching this thread's input state to whichever
    thread currently owns the foreground is the standard, documented way
    around that restriction — attach, ask, detach, and then check the
    foreground window is genuinely ours before reporting success, since a
    refused call does not raise anything on its own.
    """
    import win32api
    import win32gui
    import win32process

    current_fg = win32gui.GetForegroundWindow()
    if current_fg == hwnd:
        return True

    my_thread_id = win32api.GetCurrentThreadId()
    attached = False
    fg_thread_id = 0
    if current_fg:
        fg_thread_id, _ = win32process.GetWindowThreadProcessId(current_fg)
        if fg_thread_id and fg_thread_id != my_thread_id:
            attached = win32process.AttachThreadInput(fg_thread_id, my_thread_id, True)

    try:
        win32gui.SetForegroundWindow(hwnd)
    finally:
        if attached:
            win32process.AttachThreadInput(fg_thread_id, my_thread_id, False)

    return win32gui.GetForegroundWindow() == hwnd
