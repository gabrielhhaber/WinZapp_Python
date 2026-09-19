# Tests must never open a window on a developer's desktop

> Why a plain `pytest` never shows anything, what `--run-wx-gui` is for and why no agent may pass it.
>
> Moved verbatim out of `CLAUDE.md` so it is read when the area is touched, not on every session. Keep it here: this is measured history, not a summary.

Under uv, prefix any of these with `uv run` (`uv run pytest ...`, or the `uv run test ...` shortcut).
**A plain `pytest` never opens anything in the foreground, and that is a
deliberate default, not a convenience.** WinZapp is maintained by blind
developers: a test window taking focus does not break the test run, it breaks
whatever they had open in another window — and it has crashed NVDA outright.
NVDA takes focus on the throwaway window and is still enumerating its children
over COM (`event_gainFocus` → `getDialogText` → `IAccessible._get_children` →
`oleacc.AccessibleObjectFromEvent`) when the test destroys it; confirmed live
from NVDA's own traceback. "Remember to pass a flag" was the wrong shape for
that risk — forgetting costs the person least able to absorb it.

So the suite stays off the desktop two ways. Frames go through
`tests/conftest.py`'s `hidden_frame()`: real, fully functional parents, but
off-screen tool windows with no taskbar button, so they never become
foreground. The handful of modules that construct a real `Dialog` subclass —
which owns its own construction and cannot be positioned from outside — carry
the `wxgui` marker and are **skipped unless explicitly asked for**, via
`--run-wx-gui` or `WINZAPP_RUN_WX_GUI_TESTS=1`.

Every CI workflow passes `--run-wx-gui`, so the coverage is never actually
lost — it just stops running on a human's desktop by accident.

**`--run-wx-gui` is for CI, and for nothing else.** Do not pass it on a
developer machine, and do not instruct an agent, script or helper to pass it:
background agents run on the *user's own desktop*, not somewhere else, so a
subagent told to "just run the full suite" opens those dialogs in that user's
face. It has already happened once — a review agent was told to use the flag on
the reasoning that no human was at that desktop, and the pairing dialog's
country list was read aloud by the user's screen reader mid-session. The same
applies to ad-hoc probe scripts: anything that constructs a real wx window, or
hooks WinEvents to watch one, belongs in CI or nowhere.
`tests/test_no_desktop_visible_windows.py` enforces all of it: no module may
reintroduce a bare `wx.Frame(None)`, a new module building a real dialog must
carry the marker, and a CI step running a bare `pytest` fails the suite rather
than silently dropping the dialog tests.
Tests cover `client/core/database.py` and `client/core/database_bridge.py` (async SQLite layer + its sync façade) and small islands of pure logic pulled out of `main.py`/`core/notification_manager.py`/`ui/conversations.py` (e.g. `tests/test_sender_names.py`, `tests/test_notifications.py`, `tests/test_delivery_status.py`, `tests/test_send_jid_resolution.py`, `tests/test_message_bookmarks.py`) by binding their unbound methods onto a plain stub object — `MainWindow`/`ConversationsPanel` are wx.Frame/wx.Panel and can't be instantiated without a running wx.App, so the stub carries only the attributes the method under test actually touches. There are no tests for the wx UI in bulk. Async tests use `pytest-asyncio` in `auto` mode — no `@pytest.mark.asyncio` decorator needed, just declare test functions `async def`. **New functions/features should come with a test in this style as part of the same change.**
