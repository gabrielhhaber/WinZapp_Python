"""WinZapp must not answer "no" when Windows asks to shut down.

Diagnosed on 2026-09-09, after an overnight PC shutdown came back with a
profile WhatsApp Web refused. `shutdown_audit.log` covering 159 launches
carried seventeen runs that ended with no teardown line at all -- eleven of
them overnight gaps of 7-12 h, exactly the shape of Windows ending the session
with WinZapp open -- and **not one line from either shutdown handler**, though
both audit as their first statement.

Two faults, and the second is the damaging one:

* wxMSW routes WM_QUERYENDSESSION and WM_ENDSESSION to `wxTheApp` and to
  nothing else (`wxWindowMSW::HandleQueryEndSession` /`HandleEndSession` both
  end in `wxTheApp->SafelyProcessEvent(event)`), and a wxCloseEvent is not a
  command event, so it never propagates to a frame. Bound on the MainWindow,
  as they were, the handlers could not run. Confirmed live by sending
  WM_QUERYENDSESSION to the running app's own window: delivered, no audit line.
* So wxApp's own static table entry ran instead, and it is this:

      void wxApp::OnQueryEndSession(wxCloseEvent& event)
      {
          if (GetTopWindow())
              if (!GetTopWindow()->Close(!event.CanVeto()))
                  event.Veto(true);
      }

  `Close()` fires EVT_CLOSE on the main window, which is `_on_close()` -- and
  `_on_close()` hides to the tray and **vetoes**, correctly, because that is
  what the X button should do. The veto reached Windows: WinZapp refused every
  shutdown. Measured against the live app, which answered 0 to
  WM_QUERYENDSESSION. Windows blocks on that, then terminates the process on
  the way past its own timeout -- no WM_ENDSESSION, no teardown, node.exe and
  Chrome killed mid-write, and the profile comes back rejected.

These tests drive the real wx dispatch rather than restating it, because every
step above is wx behaviour rather than WinZapp behaviour, and the whole bug was
a wrong belief about where wx sends an event.
"""

import ast
import inspect
import re
import textwrap
from pathlib import Path

import pytest
import wx

from main import MainWindow
from tests.conftest import hidden_frame


MAIN_PY = Path(__file__).resolve().parents[1] / "client" / "main.py"


def _query_event():
    event = wx.CloseEvent(wx.wxEVT_QUERY_END_SESSION, wx.ID_ANY)
    # Exactly what HandleQueryEndSession() sets before dispatching.
    event.SetCanVeto(True)
    return event


@pytest.fixture
def tray_style_app(wx_app):
    """A top window that vetoes its close event, like MainWindow does while the
    tray icon is up -- and a clean app afterwards, since wx_app is shared by the
    whole run."""
    frame = hidden_frame()
    frame.Bind(wx.EVT_CLOSE, lambda e: e.Veto())
    previous = wx_app.GetTopWindow()
    wx_app.SetTopWindow(frame)
    try:
        yield wx_app, frame
    finally:
        wx_app.SetTopWindow(previous)
        frame.Destroy()


class TestTheBugThisReplaces:
    def test_with_nothing_bound_on_the_app_winzapp_vetoes_the_shutdown(
            self, tray_style_app):
        """The shipped behaviour until 2026-09-09, reproduced end to end: the
        frame-bound handlers never see the event, wxApp's default closes the top
        window, the tray close handler vetoes, and the veto is what Windows is
        told. A veto here is a process Windows kills instead of one it lets
        finish."""
        app, _frame = tray_style_app
        event = _query_event()
        app.SafelyProcessEvent(event)
        assert event.GetVeto() is True

    def test_binding_on_the_frame_does_not_help(self, tray_style_app):
        """Why the fix is a different Bind target and not a different handler
        body: the event is dispatched to the app, and a wxCloseEvent does not
        propagate to windows."""
        app, frame = tray_style_app
        ran = []
        frame.Bind(wx.EVT_QUERY_END_SESSION, lambda e: ran.append(1))
        event = _query_event()
        app.SafelyProcessEvent(event)
        assert ran == [], "a frame-bound EVT_QUERY_END_SESSION handler ran"
        assert event.GetVeto() is True


class TestTheFix:
    def test_a_handler_bound_on_the_app_runs_and_allows_the_shutdown(
            self, tray_style_app):
        app, _frame = tray_style_app
        ran = []
        handler = lambda e: ran.append(1)          # noqa: E731 - mirrors the real shape
        app.Bind(wx.EVT_QUERY_END_SESSION, handler)
        try:
            event = _query_event()
            app.SafelyProcessEvent(event)
        finally:
            app.Unbind(wx.EVT_QUERY_END_SESSION, handler=handler)
        assert ran == [1]
        assert event.GetVeto() is False, (
            "the shutdown is still vetoed, so Windows will kill the process "
            "instead of letting the teardown flush the profile"
        )

    def test_skipping_puts_the_veto_straight_back(self, tray_style_app):
        """The one detail that makes the fix fragile, pinned so it cannot be
        'tidied' back in: a dynamic Bind is searched before the class's static
        event table, so binding on the app replaces wxApp::OnQueryEndSession --
        but event.Skip() resumes the search and reaches it anyway."""
        app, _frame = tray_style_app
        handler = lambda e: e.Skip()               # noqa: E731
        app.Bind(wx.EVT_QUERY_END_SESSION, handler)
        try:
            event = _query_event()
            app.SafelyProcessEvent(event)
        finally:
            app.Unbind(wx.EVT_QUERY_END_SESSION, handler=handler)
        assert event.GetVeto() is True


class TestWinZappBindsAndAnswersTheRightWay:
    """Structural, because the real binding happens inside MainWindow's UI
    construction, which needs the whole app to stand up."""

    def test_the_handlers_are_bound_on_the_app(self):
        source = MAIN_PY.read_text(encoding="utf-8")
        for evt in ("EVT_QUERY_END_SESSION", "EVT_END_SESSION"):
            assert re.search(
                r"_app\.Bind\(wx\.%s," % evt, source), (
                f"{evt} is not bound on the wx.App; wxMSW dispatches it to "
                f"wxTheApp and nowhere else, so a frame binding never runs"
            )
            assert not re.search(r"self\.Bind\(wx\.%s," % evt, source), (
                f"{evt} is bound on the frame again — that binding is dead, "
                f"and wxApp's default vetoes the shutdown in its place"
            )

    @pytest.mark.parametrize(
        "method", [MainWindow._on_query_end_session, MainWindow._on_end_session],
        ids=["_on_query_end_session", "_on_end_session"])
    def test_neither_handler_skips(self, method):
        """Skipping reaches wxApp's defaults: the veto for the query, and
        DeleteAllTLWs()/OnExit()/exit() for the end — the second one spending a
        budget the teardown has already used, in a process Windows terminates
        the moment the handler returns."""
        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
        skips = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Skip"
        ]
        assert skips == [], (
            f"{method.__name__} calls Skip(), which resumes the handler search "
            f"and reaches wxApp's own default"
        )
