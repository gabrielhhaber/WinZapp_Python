---
name: write-test
description: Write a test for WinZapp in the style the repository already uses. Use whenever a change adds or fixes a function, method or behaviour in client/ — CLAUDE.md requires the test to land in the same change — or when an existing test needs extending. Covers the two routes around wxPython (extract pure logic, or call the unbound method against a stub), the shared fixtures, and the async setup.
---

# Writing a test

`MainWindow` (`wx.Frame`) and `ConversationsPanel` (`wx.Panel`) cannot be
instantiated without a `wx.App`, so tests never construct them. `pytest.ini`
sets `pythonpath = client` (write `from main import MainWindow`,
`from core.database import ...`) and `asyncio_mode = auto`.

## Test behaviour, not source text

Call the code and assert on what it returns or does. Do **not** add tests that
read a source file and assert a string is in it (`assert "x" in source`): they
break on every refactor and prove nothing about behaviour. A source-text
assertion is acceptable only as a structural guard with no behaviour to call
(a workflow file, a patch constant, "no module imports `main`").

## Route 1 — extract the pure logic (prefer this)

Move the logic to a module-level function and test it directly, as
`tests/test_delivery_status.py` does. It goes in a plain-function module next
to its responsibility (`client/main_window/message_rules.py`,
`client/ui/conversation_panel/media_paths.py`, `client/core/`).

## Route 2 — unbound method against a stub

For logic that must stay on the class. Canonical example:
`tests/test_sender_names.py`.

```python
from main import MainWindow


class _Stub:
    def __init__(self, **kwargs):
        self.contacts = {}
        self._lid_to_phone = {}
        for key, value in kwargs.items():
            setattr(self, key, value)

    # A function assigned as a CLASS attribute becomes a bound method.
    _learn_sender_name = MainWindow._learn_sender_name
    # A real @staticmethod must be re-wrapped.
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
```

- The stub carries only the attributes the method touches.
- Bind sibling methods under their real names.
- Patch a module global with `tests/god_modules.py`
  (`patch_main_global(monkeypatch, "api_post", fake)`,
  `patch_conversations_global(...)`), never `monkeypatch.setattr(main, ...)`:
  a method looks a global up in the mixin module it is defined in.
- For a raw member use `inspect.getattr_static(MainWindow, name)`, not
  `MainWindow.__dict__[name]`.
- Build message dicts through a small local factory (`_group_msg(...)`).

## Never wait for real time

A test that sleeps through a timeout, or joins every live thread, costs the
whole suite seconds. Shrink the timeout on the stub, replace the module's
`time` with a fake clock, and join only the threads the call started.

## Shared fixtures (`tests/conftest.py`)

| fixture | what it gives |
| --- | --- |
| `wx_app` | the single session-scoped `wx.App` — never construct a second one |
| `fernet_key`, `fernet` | key / cipher matching the DB layer |
| `sample_chat`, `sample_contact`, `sample_message`, `sample_data` | canonical payloads |
| `tmp_dir` | temporary directory |
| `in_memory_db`, `db_with_data` | async `DatabaseManager` on in-memory SQLite |

A frame goes through `hidden_frame()`; a module that builds a real `Dialog`
carries the `wxgui` marker (see `docs/traps/tests-never-open-windows.md`).
An async test is a plain `async def test_...`, no `@pytest.mark.asyncio`.

## Naming and running

Files are `tests/test_<subject>.py`, grouped into `class Test<Behaviour>`.
The module docstring names the bug the file pins and its mechanism.

```
uv run pytest tests/test_<subject>.py
```

Run the files for what you touched. CI runs the whole suite on every PR; run
`uv run pytest` serially, at most once, locally only for a cross-cutting change (`tests/conftest.py`,
a shared helper, a module split). Never pass `--run-wx-gui`.
