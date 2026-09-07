---
name: write-test
description: Write a test for WinZapp in the style the repository already uses. Use whenever a change adds or fixes a function, method or behaviour in client/ — CLAUDE.md requires the test to land in the same change — or when an existing test needs extending. Covers the two routes around wxPython (extract pure logic, or call the unbound method against a stub), the shared fixtures, and the async setup.
---

# Writing a test

## Why the tests look the way they do

`MainWindow` is a `wx.Frame` and `ConversationsPanel` is a `wx.Panel`. Neither
can be instantiated without a running `wx.App`, so the suite never constructs
them. Instead it reaches the logic by one of two routes — and picking the wrong
one is what stalls people who have only read a single test file.

`pytest.ini` sets `pythonpath = client`, so imports are written as if from
inside `client/`: `from main import MainWindow`, `from core.database import
_delivery_status`. It also sets `asyncio_mode = auto`.

## Route 1 — extract the pure logic (prefer this)

If the behaviour can live as a module-level function, move it there and test it
directly. `ack_to_status()` and `_delivery_status()` are module-level for
exactly this reason, and `tests/test_delivery_status.py` just imports and calls
them. No stub, no wx, nothing to keep in sync.

This is the better outcome even ignoring tests: it shrinks `main.py`, which is
~26,900 lines and where most logic ends up by default.

## Route 2 — unbound method against a stub

For logic that genuinely has to stay on the class (it reads a lot of instance
state, or calls siblings through `self`), bind the real method onto a plain
stub. About 100 of the 286 test files do this. The canonical example is
`tests/test_sender_names.py`:

```python
from main import MainWindow


class _Stub:
    """Minimal stand-in for MainWindow for name-resolution methods."""

    def __init__(self, **kwargs):
        self.contacts = {}
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        for key, value in kwargs.items():
            setattr(self, key, value)

    # Assigning the function as a CLASS attribute is what makes it a bound
    # method on instances — this is the whole trick.
    _learn_sender_name = MainWindow._learn_sender_name
    _learn_sender_names_bulk = MainWindow._learn_sender_names_bulk

    # A method that is genuinely a @staticmethod has to be re-wrapped, or it
    # would receive the stub as its first argument.
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
```

Rules that make this work and keep it honest:

- **The stub carries only the attributes the method under test actually
  touches.** That is not laziness — it documents exactly which state the method
  depends on, and a method that needs twenty attributes is telling you
  something about its design.
- **Bind siblings under their real names.** If `_learn_sender_names_bulk` calls
  `self._learn_sender_name`, that name must exist on the stub. Short aliases for
  the tests' own convenience come *in addition*, never instead.
- **Build message dicts through a small local factory** (`_group_msg(...)`)
  rather than repeating the canonical shape — `{"key": {"remoteJid", "fromMe",
  "id", "participant"}, "message", "messageType", "messageTimestamp",
  "pushName"}` — in every test.

## Shared fixtures (`tests/conftest.py`)

Check here before building your own:

| fixture | what it gives |
| --- | --- |
| `wx_app` | a single session-scoped `wx.App`, for the few tests that need real wx objects |
| `fernet_key`, `fernet` | encryption key / cipher matching the DB layer |
| `sample_chat`, `sample_contact`, `sample_message`, `sample_data` | canonical payload shapes |
| `tmp_dir` | temporary directory |
| `in_memory_db`, `db_with_data` | async `DatabaseManager` against an in-memory SQLite |

`wx_app` is session-scoped on purpose: wxWidgets supports exactly one `App` per
process, and two test files each building their own used to kill the whole
pytest process on the headless CI runner — no traceback, no output, just a
non-zero exit after the "N passed" line had already printed. Two release builds
died that way. Never construct a second `wx.App`.

## Async tests

`asyncio_mode = auto`, so an async test is just `async def test_...` — **no
`@pytest.mark.asyncio` decorator**, which is the reflex to unlearn. Used mainly
for `core/database.py` and `core/database_bridge.py`.

## The docstring earns its place

House style is a module docstring that names the *bug family* the file pins,
with the mechanism spelled out — see `test_delivery_status.py` ("WinZapp says
sent, the message never arrived", then the three specific defects). A test
whose docstring only says "tests for X" makes the suite a net, but not
documentation.

## Naming and verification

Files are `tests/test_<subject>.py`, grouped into `class Test<Behaviour>` when
a file covers more than one. Then:

```
pytest tests/test_<subject>.py
pytest
```

Run the whole suite before committing: `pythonpath = client` means a test can
import from anywhere in the client and break something unrelated.

Note that `pytest.ini` declares `slow`, `integration` and `load` markers that
nothing currently uses — tests needing real wx take the `wx_app` fixture
instead. Don't reach for the markers expecting them to select anything.
