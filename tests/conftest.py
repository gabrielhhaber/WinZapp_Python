"""Shared fixtures for all WinZapp tests — async edition."""

# Before anything imports win32com.client or comtypes.client: one COM
# wrapper cache per xdist worker, or two workers race on the same files
# (tests/win32com_cache.py).
from tests.win32com_cache import isolate_comtypes_gen, isolate_gen_py

isolate_gen_py()
isolate_comtypes_gen()

import importlib.util
import json
import os
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

from tests.ci_shards import WX_DESKTOP_GROUP, creates_real_windows, parse_shard, shard_of

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


#: Repeats behind fastest_of()/fastest_of_async(). Enough to find the floor
#: without making a load test noticeably slower.
TIMING_REPEATS = 7


def fastest_of(call, repeats: int = TIMING_REPEATS) -> float:
    """Seconds taken by the quickest of *repeats* runs of ``call``.

    The load tests assert that doubling the data does not more than double the
    work. Timing a single run makes that assertion unreliable in exactly one
    direction: noise is one-sided — a sample can only ever come out slower than
    the truth, never faster — so one unlucky garbage collection inside the
    larger measurement inflates the ratio without any code having changed.

    Measured in a full suite run on 2026-09-10: the 2,000-chat planning call
    was recorded at 69.9 ms against a median of 6.5 ms on the same machine,
    while the 500-chat call was recorded at its clean 1.6 ms — "44.1x for 4x
    the chats", failing the build over an implementation that is perfectly
    linear (3.2 microseconds per chat, flat from 250 to 4,000 chats).

    That is worth a helper rather than a local fix, because a red run is not
    the worst outcome: release.yml builds nothing when its test job fails, so a
    test that fails at random blocks a good stable release (and an alpha).

    The minimum is the right statistic precisely because of the one-sidedness:
    it converges on the cost of the work itself, and it still grows when the
    work genuinely grows, which is all these assertions read.
    """
    best = None
    for _ in range(repeats):
        started = time.perf_counter()
        call()
        elapsed = time.perf_counter() - started
        if best is None or elapsed < best:
            best = elapsed
    return best


async def fastest_of_async(make_awaitable, repeats: int = TIMING_REPEATS) -> float:
    """fastest_of() for async work. ``make_awaitable`` is called once per
    repeat and must return a FRESH awaitable — an already-created coroutine
    cannot be awaited twice, and a factory also lets a caller vary anything
    that must differ between runs (a fresh chat id per insert, say)."""
    best = None
    for index in range(repeats):
        started = time.perf_counter()
        await make_awaitable(index)
        elapsed = time.perf_counter() - started
        if best is None or elapsed < best:
            best = elapsed
    return best


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
    parser.addoption(
        "--run-load",
        action="store_true",
        default=False,
        help="Run the tests marked `load` (synthetic large-account scenarios, "
             "~45 s). Skipped by default on a developer machine; always run "
             "when the CI environment variable is set.",
    )
    parser.addoption(
        "--shard",
        default=None,
        metavar="K/N",
        help="Run only shard K of N: the test modules whose stable hash lands "
             "on K (tests/ci_shards.py). CI runs every shard in its own job; "
             "without this option every module runs, as always.",
    )
    # pytest.ini carries `--dist loadgroup` (only meaningful with -n). Without
    # pytest-xdist installed — a stale venv, a bare `pip install pytest` — that
    # option would not exist and even a plain `pytest` would refuse to start.
    if importlib.util.find_spec("xdist") is None:
        parser.addoption("--dist", default="no",
                         help="Accepted and ignored: pytest-xdist is not installed.")


def _wx_gui_requested(config) -> bool:
    return bool(
        config.getoption("--run-wx-gui")
        or os.environ.get(_WX_GUI_OPT_IN_ENV, "").strip() not in ("", "0", "false", "False")
    )


def _load_requested(config) -> bool:
    """The `load` tests cost ~45 s and guard scaling, not behaviour: CI runs
    them (GitHub Actions sets CI), a developer opts in with --run-load."""
    return bool(
        config.getoption("--run-load")
        or os.environ.get("CI", "").strip() not in ("", "0", "false", "False")
    )


def pytest_ignore_collect(collection_path, config):
    """--shard K/N: a test module of another shard is never collected, so it
    is not imported either — collection is split across the CI jobs, not only
    the run. Everything that is not a tests/test_*.py module (conftest,
    helpers, other folders) is left to pytest, as it always was."""
    shard = parse_shard(config.getoption("--shard"))
    if shard is None or collection_path.suffix != ".py" or not collection_path.name.startswith("test_"):
        return None
    index, count = shard
    return True if shard_of(collection_path.name, count) != index else None


_REAL_WINDOW_MODULES = {}


def _module_creates_real_windows(path) -> bool:
    key = str(path)
    if key not in _REAL_WINDOW_MODULES:
        try:
            _REAL_WINDOW_MODULES[key] = creates_real_windows(Path(path).read_text(encoding="utf-8"))
        except OSError:
            _REAL_WINDOW_MODULES[key] = False
    return _REAL_WINDOW_MODULES[key]


# tryfirst: pytest-xdist reads the xdist_group marker in its own
# collection_modifyitems, so the group has to be on the item before that runs.
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    # With -n (CI only): every module that puts real wx windows up runs on one
    # worker, one test after another (tests/ci_shards.py). Harmless without -n.
    # It overrides a module's own group: both "clipboard" modules put windows
    # up too, and they stay together, one after the other, in this one.
    for item in items:
        if _module_creates_real_windows(item.path):
            item.add_marker(pytest.mark.xdist_group(WX_DESKTOP_GROUP))
    if not _load_requested(config):
        skip_load = pytest.mark.skip(
            reason="synthetic load test - pass --run-load to run it (CI does)"
        )
        for item in items:
            if "load" in item.keywords:
                item.add_marker(skip_load)
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


@pytest.fixture(autouse=True)
def _transcription_slot_of_this_process(monkeypatch):
    """The machine-wide transcription slot is a named mutex shared with the
    WinZapp the developer has running: a test transcription must never wait
    for theirs, nor make theirs let its model go. Tests get a slot of their
    own process only (test_transcription_machine_slot.py builds its own)."""
    from core.transcription import machine_slot
    monkeypatch.setattr(machine_slot, "slot",
                        machine_slot.MachineSlot(native_factory=machine_slot._LocalNative))
    # and no gap a connection test recorded reaches the age of another test's message
    from core.transcription import auto_queue
    monkeypatch.setattr(auto_queue, "listening_gaps", auto_queue.ListeningGaps())


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

    The records carry `t` as their own timestamp, because that is what a chat
    that is actually up to date looks like: the activity the chat list claims
    is the timestamp of the newest message we stored. A record older than `t`
    means the server has something we never stored, and
    incremental_sync.local_history_behind_server() reads it as exactly that —
    a chat built that way is not "unchanged", it is one of the broken ones.

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
            "messageTimestamp": t,
        } for n in range(records)]}},
    }


# ── Helpers: a forbidden word in a translated sentence ───────────────────────


def _fold_for_word_search(text: str) -> str:
    # casefold() alone is not enough: "İndirildi".casefold() is "i̇ndirildi",
    # with a combining dot above that no stem written "indir" contains. Dropping
    # every combining mark after NFKD removes it — and every other accent with
    # it, so a stem is written the same way with or without its diacritics.
    # Dotless ı is folded into i for the same consistency: casefold() already
    # turns the Turkish capital I, whose lowercase is ı, into a plain i.
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.replace("ı", "i")


def words_found_in(text: str, words) -> list:
    """The entries of `words` that occur in `text`, compared case- and
    accent-insensitively.

    Shared by the transcription tests that assert a sentence does *not* use a
    word in any of the app's languages (tests/test_transcription_management.py,
    tests/test_transcription_narration.py). A plain `.lower()` let a Turkish
    sentence opening with "İndir…" slip past the stem "indir", and a check that
    can only fail in the languages whose capitals behave is a check that the
    locales added last never really had.
    """
    folded = _fold_for_word_search(text)
    return [word for word in words if _fold_for_word_search(word) in folded]


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

def destroy_now(*windows):
    """Destroy wx windows AND their parents for real, now.

    Destroy() on a top-level window (a Dialog, a Frame) only queues it: wx
    deletes it on the next idle cycle, and the test suite never runs an event
    loop, so without this nothing a test creates is ever freed. A Settings
    dialog is hundreds of native controls, and the roundtrip tests build one
    per case: the process ran out of window handles part-way through
    test_settings_checkboxes_roundtrip.py on CI, and every dialog test after
    that point failed at creation ("Failed to create dialog. Incorrect
    DLGTEMPLATE?") — 103 failures that had nothing to do with what they test.

    wx.Yield() is what runs the idle cycle here (wxPython exposes no
    ProcessIdle). Only ever reached from `wxgui` tests, which run on CI.
    """
    import wx

    seen = []
    for window in windows:
        while window is not None and window not in seen:
            seen.append(window)
            try:
                window = window.GetParent()
            except RuntimeError:
                break
    for window in seen:
        try:
            if window:
                window.Destroy()
        except RuntimeError:
            pass
    wx.Yield()


def set_clipboard_text(text):
    """Write plain text to the clipboard, with the same retry guarantee."""
    import wx

    return set_clipboard_data(lambda: wx.TextDataObject(text))
