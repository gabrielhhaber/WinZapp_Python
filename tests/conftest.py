"""Shared fixtures for all WinZapp tests — async edition."""

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

# ── Fixtures: wx ────────────────────────────────────────────────────────────



# ── wxgui: opt-in, because the default must be safe for a blind developer ────
#
# A plain `pytest` has to be safe to run on the machine somebody is using.
# Tests marked `wxgui` construct a real top-level wx dialog, which takes
# foreground focus away from whatever the developer is doing - and has crashed
# NVDA outright: NVDA takes focus on the throwaway window and is still
# enumerating its children over COM (event_gainFocus -> getDialogText ->
# IAccessible._get_children -> oleacc.AccessibleObjectFromEvent) when the test
# destroys it. Confirmed live from NVDA's own traceback.
#
# WinZapp exists for blind users and is maintained by blind developers, so
# "remember to pass -m 'not wxgui'" is the wrong default: the cost of
# forgetting falls on the person least able to absorb it, and it is not their
# test run that breaks - it is whatever they were doing in another window.
#
# So these are skipped unless explicitly asked for, by `--run-wx-gui` or
# WINZAPP_RUN_WX_GUI_TESTS=1. Every CI workflow passes the flag (enforced by
# tests/test_no_desktop_visible_windows.py), so the coverage is never actually
# lost - it just stops running on a human's desktop by accident.
_WX_GUI_OPT_IN_ENV = "WINZAPP_RUN_WX_GUI_TESTS"


def pytest_addoption(parser):
    parser.addoption(
        "--run-wx-gui",
        action="store_true",
        default=False,
        help="Run the tests marked `wxgui`, which open a real top-level wx "
             "dialog and steal foreground focus. Safe on CI and on a machine "
             "nobody is using; on a developer's own desktop it can crash a "
             "running screen reader. CI passes this.",
    )


def _wx_gui_requested(config) -> bool:
    return bool(
        config.getoption("--run-wx-gui")
        or os.environ.get(_WX_GUI_OPT_IN_ENV, "").strip() not in ("", "0", "false", "False")
    )


def pytest_collection_modifyitems(config, items):
    if _wx_gui_requested(config):
        return
    skip = pytest.mark.skip(
        reason="opens a real top-level wx dialog and steals focus - pass "
               "--run-wx-gui (or set WINZAPP_RUN_WX_GUI_TESTS=1) to run it"
    )
    for item in items:
        if "wxgui" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def wx_app():
    """A single wx.App shared by every test in the run that needs a real
    wx.Timer/wx.StaticBitmap/etc. (test_video_player.py, test_qrcode_render.py).

    wxWidgets only ever supports one App instance per process — each of
    those two files used to construct its own module-scoped wx.App()
    independently, which happened to run fine locally but crashed the
    whole test process (no traceback, no output at all — just the process
    dying with a non-zero exit code a couple seconds after pytest's own
    "N passed" summary line already printed) on GitHub Actions' headless
    Windows runner as soon as both test files were collected in the same
    session — reproduced live via two failed release builds in a row.
    session scope (not module) is what actually guarantees only one
    instance ever gets created, regardless of how many files ask for it.
    """
    import wx
    return wx.App()


# ── Fixtures: Windows notification state ─────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_do_not_disturb(request, monkeypatch):
    """Pin Windows' do-not-disturb state to OFF for the whole suite.

    core.quiet_hours.is_quiet_hours_active() reads the REAL machine: registry
    notification switches, WNF, WinRT and SHQueryUserNotificationState. That
    makes any test touching the notification path pass or fail according to
    whatever the machine running it happens to be doing — and a headless
    GitHub Actions runner is not doing the same thing as a developer desktop.

    It cost an alpha release to learn that twice. The gate started out
    covering only the background sound, so a handful of files monkeypatched it
    one by one and that was enough. Then it grew to suppress the entire
    notification — banner included — and every _dispatch() test in
    test_notifications.py started failing on CI only, with the local suite
    green: on the runner the real state reads as suppressed, so no toast was
    ever shown and the assertions had nothing to look at.

    Pinning it here rather than per file is the point: the next test to touch
    _dispatch() will not have to know this exists. A test that genuinely wants
    do-not-disturb ON monkeypatches it back, which still works because every
    caller imports the function inside the function body.

    test_quiet_hours.py is exempt — it is the module that tests this very
    function, and stubbing it there would test the stub.
    """
    import core.quiet_hours as quiet_hours
    exempt = request.node.fspath.purebasename == "test_quiet_hours"
    quiet_hours.invalidate_cache()
    if not exempt:
        monkeypatch.setattr(quiet_hours, "is_quiet_hours_active", lambda: False)
    # A generator fixture must yield on every path — returning early makes
    # pytest raise "did not yield a value" for the exempt module.
    yield
    quiet_hours.invalidate_cache()


# ── Fixtures: Keys / Encryption ───────────────────────────────────────────────


@pytest.fixture
def fernet_key() -> bytes:
    """Return a fresh Fernet key for one test."""
    return Fernet.generate_key()


@pytest.fixture
def fernet(fernet_key: bytes) -> Fernet:
    """Return a ready-to-use Fernet instance."""
    return Fernet(fernet_key)


# ── Fixtures: Sample Data ─────────────────────────────────────────────────────


@pytest.fixture
def sample_chat() -> dict:
    """A single chat dict matching the shape stored in messages.dat."""
    return {
        "remoteJid": "5511999999999@s.whatsapp.net",
        "unreadCount": 3,
        "pushName": "Alice",
        "name": "Alice Silva",
        "messages": {
            "messages": {
                "records": [],
                "total": 0,
                "pages": 1,
                "currentPage": 1,
            }
        },
        "lastMessage": None,
        "archive": False,
        "archived": False,
        "type": "chat",
    }


@pytest.fixture
def sample_contact() -> dict:
    """A single contact dict matching the shape stored in messages.dat."""
    return {
        "id": "5511999999999@s.whatsapp.net",
        "remoteJid": "5511999999999@s.whatsapp.net",
        "name": "Alice Silva",
        "pushName": "Alice",
        "profilePicUrl": "",
        "type": "contact",
        "isSaved": True,
    }


@pytest.fixture
def sample_message() -> dict:
    """A single normalized message dict (minimal)."""
    return {
        "key": {
            "remoteJid": "5511999999999@s.whatsapp.net",
            "fromMe": False,
            "id": "AB12345",
        },
        "pushName": "Alice",
        "message": {
            "conversation": "Hello, world!",
        },
        "messageTimestamp": 1700000000,
        "messageType": "conversation",
    }


@pytest.fixture
def sample_data(
    sample_chat: dict,
    sample_contact: dict,
    sample_message: dict,
) -> dict[str, Any]:
    """Full data dict matching the top-level shape of messages.dat."""
    chat_jid = "5511999999999@s.whatsapp.net"
    contact_jid = "5511999999999@s.whatsapp.net"

    # Add one message so the chat isn't empty
    chat = dict(sample_chat)
    chat["messages"]["messages"]["records"] = [sample_message]
    chat["messages"]["messages"]["total"] = 1

    return {
        "chats": {chat_jid: chat},
        "contacts": {contact_jid: sample_contact},
        "lid_to_phone": {"12345@lid": "5511999999999@s.whatsapp.net"},
        "unresolvable_lids": [],
        "unresolvable_names": [],
        "status_updates": {
            "5511888888888@s.whatsapp.net": [
                {
                    "key": {
                        "remoteJid": "status@broadcast",
                        "id": "status_1",
                        "participant": "5511888888888@s.whatsapp.net",
                    },
                    "message": {"conversation": "My status"},
                    "messageTimestamp": 1700000100,
                }
            ]
        },
    }


# ── Helpers: a chat as the warm cache holds it ───────────────────────────────


def warm_cached_chat(jid: str, t: int = 100, records: int = 1) -> dict:
    """One chat in the shape the incremental sync planner compares against.

    Shared rather than copied because the three modules that drive a sync
    round — tests/test_run_sync_warm_path.py, tests/test_periodic_poll_delta.py
    and tests/test_repair_state_durability.py — all need the same two fields
    and both are load-bearing in a way that is easy to get subtly wrong in a
    private copy: `t` is the activity marker the plan diffs, and the record
    count is what tells "unchanged" apart from "missing-local-history"
    (core/incremental_sync.classify_chat_sync). A chat built with no records
    is classified full on every path, which is exactly the shape that cannot
    tell a warm round from a cold one — so `records=0` is a deliberate choice
    a test makes, never an accident of which copy of the helper it used.

    Lives in conftest (imported as tests.conftest — tests/ is a package) for
    the same reason set_clipboard_data() below does.
    """
    return {
        "remoteJid": jid,
        "t": t,
        "messages": {"messages": {"records": [{
            "key": {"remoteJid": jid, "id": f"{jid}-m{n}", "fromMe": False},
            "message": {"conversation": "x"},
            "messageType": "conversation",
            "messageTimestamp": t - 10,
        } for n in range(records)]}},
    }


# ── Fixtures: Temporary files / directories ───────────────────────────────────


@pytest.fixture
def tmp_dir() -> Path:
    """Return a temporary directory that lives for the duration of a test."""
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


# ── Fixtures: Async DatabaseManager ───────────────────────────────────────────


@pytest_asyncio.fixture
async def in_memory_db(fernet_key: bytes):
    """Yield an async DatabaseManager pointed at an in-memory SQLite database."""
    from core.database import DatabaseManager

    async with DatabaseManager(":memory:", fernet_key) as db:
        yield db


@pytest_asyncio.fixture
async def db_with_data(in_memory_db, sample_data: dict):
    """Yield a DatabaseManager pre-populated with sample_data."""
    await in_memory_db.import_from_dict(sample_data)
    return in_memory_db


# ── Helpers: clipboard writes that actually land ─────────────────────────────


def set_clipboard_data(make_data_object, attempts=10, delay=0.05):
    """Open the clipboard and write ``make_data_object()`` to it, retrying
    until the write is confirmed. Returns True on success.

    wx.TheClipboard.SetData() wraps Windows' OLE clipboard, which can
    transiently refuse a write right after another Open/Close cycle elsewhere
    in the same process — reproduced by running the clipboard tests after
    enough other wx-using tests earlier in a full suite run.

    Two things make that refusal invisible if you do not handle it here:

    * A failed SetData() leaves the PREVIOUS content in place, and the
      clipboard still opens fine — so a caller that only checks Open() reads
      some earlier test's data and asserts happily against it. Hence checking
      SetData()'s return value, and retrying.
    * Clear() first, so that if every attempt somehow fails the clipboard is
      empty rather than stale: the test then fails honestly instead of
      passing on someone else's data.

    Flush() is deliberately NOT called. It asks the OS to render every format
    so the data outlives the owning process; these tests read the clipboard
    back in the same process with the wx.App still alive, so it buys nothing
    and is one more thing that can block or be refused.

    Lives in conftest (imported as tests.conftest — tests/ is a package)
    because both clipboard-using test modules need it, and they were drifting
    apart: one retried, the other only called Flush().
    """
    import wx

    for _ in range(attempts):
        if not wx.TheClipboard.Open():
            time.sleep(delay)
            continue
        try:
            wx.TheClipboard.Clear()
            if wx.TheClipboard.SetData(make_data_object()):
                return True
        finally:
            wx.TheClipboard.Close()
        time.sleep(delay)
    return False



def hidden_frame(**kwargs):
    """A real wx.Frame for tests that need a live parent window, created so
    the DESKTOP never sees it.

    A plain `wx.Frame(None)` is a normal top-level window: Windows gives it a
    taskbar button and, as it is created and destroyed, moves the foreground
    around. A screen reader reacts to that. Running the suite on a machine
    with NVDA active crashed NVDA repeatedly, and its traceback named the
    mechanism exactly - event_gainFocus -> reportFocus -> getDialogText ->
    IAccessible _get_children -> oleacc.AccessibleObjectFromEvent: NVDA got
    focus on one of these windows and was still enumerating its children over
    COM when the test destroyed it, so the object it was reading vanished
    mid-call. Dozens of these windows appear and disappear within a single
    pytest run, which makes that race very easy to lose.

    Off-screen (well outside any real monitor), WS_EX_TOOLWINDOW and no
    taskbar button: the window is fully real - it has an HWND, children lay
    out and size normally, and every test that needs a parent still works -
    but it never becomes the foreground window, so no focus event is ever
    raised for a screen reader to chase.

    Tests must still Destroy() what they create; this only stops the window
    being visible to the desktop while it lives.
    """
    import wx

    kwargs.setdefault("pos", (-32000, -32000))
    kwargs.setdefault(
        "style", wx.FRAME_TOOL_WINDOW | wx.FRAME_NO_TASKBAR | wx.DEFAULT_FRAME_STYLE
    )
    return wx.Frame(None, **kwargs)

def set_clipboard_text(text):
    """Write plain text to the clipboard, with the same retry guarantee."""
    import wx

    return set_clipboard_data(lambda: wx.TextDataObject(text))
