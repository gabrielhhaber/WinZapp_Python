# CLAUDE.md

Only what applies to every task. Area traps load from `.claude/rules/` when
you open a matching file; the reasoning is in `docs/traps/` and
`docs/reference/`.

## Rules that protect users

1. **A plain `pytest` must never open a window** — the maintainers are blind
   and a test window stealing focus has crashed NVDA. Frames go through
   `hidden_frame()` (`tests/conftest.py`); a module building a real `Dialog`
   carries the `wxgui` marker. **`--run-wx-gui` and
   `WINZAPP_RUN_WX_GUI_TESTS=1` are CI-only**: never pass them, never hand
   them to an agent or script — background agents run on the user's own
   desktop — never run an ad-hoc script that creates a wx window or hooks
   WinEvents. Enforced by `tests/test_no_desktop_visible_windows.py`;
   history in `docs/traps/tests-never-open-windows.md`.
2. **Every user-facing string goes into every locale file**
   (`client/languages/<locale>.json`, `client/changelog_<locale>.txt`; the
   list is `client/languages/language_map.json`). `I18n.t()` has no fallback:
   a missing key is shown as the raw key. Reuse the words that locale already
   uses (`docs/reference/i18n-terminology.md`). Skill: `i18n-ui-string`.
3. **A function or fix ships with its test in the same change**: pure logic
   extracted, or the unbound method called on a plain stub. Skill:
   `write-test`.
4. **Edit `client/api_patches/`, never `client/api/`** (vendored, overwritten
   by `setup_api.py`/`build.py`). Skill: `wppconnect-patch`.
5. **All speech goes through `MainWindow.speak_output`**; plain wx controls
   only; list mutations inside `Freeze()`/`Thaw()`; titles and rows show
   names, never raw JIDs. Skill: `accessible-ui`.
6. **Never grow a file just because it is open.** Put code in the module that
   owns the responsibility (`client/main_window/`,
   `client/ui/conversation_panel/`, `client/core/`) or create one. Pure logic
   is a plain function with a direct test. A feature over ~150 lines is its
   own module. Delete dead helpers with the feature. Size budgets
   (`tests/test_god_file_split_structure.py`): split, don't raise.
7. **One agent at a time, one test run at a time.** Agents run on the
   maintainer's own machine next to a live WinZapp (Chrome + Node). Several
   agents, or any agent running `pytest -n auto`, froze it (68 orphan xdist
   workers, 2026-10-02). Start the next agent only when the previous one is
   done; agents use the repo's `.venv` (no `uv sync`/new `.venv` in a
   worktree), run only the touched test files serially plus at most one serial
   full run, leave no background process, and commit WIP early. After an
   interruption look for stray `python.exe` under `.claude\worktrees\`.

## What this is

A free Windows desktop WhatsApp client built for accessibility
(NVDA/JAWS/Narrator). A Python 3.13 + wxPython process drives a local
WPPConnect Server (Node.js) over HTTP (`http://127.0.0.1:6300/api/...`) and
Socket.IO. One account per process, each with its own Node, port and window.

## Commands

```powershell
uv sync; uv run setup-api              # fresh checkout; setup-api clones and builds client/api/
uv run winzapp                         # run the app
uv run pytest tests/test_database.py   # the test files for what you touched — the normal loop
uv run pytest -n auto                  # whole suite in parallel (~1 min); CI itself runs it serially
uv run build-installer                 # WinZappInstaller.exe + WinZapp.zip
uv run build-onefile                   # single-file WinZapp.exe
```

- Run only the test files for what you changed; run the whole suite locally
  only for a cross-cutting change. `load` tests need `--run-load` (CI runs
  them).
- A dependency change goes into `pyproject.toml` and `requirements.txt`, then
  `uv lock` (`tests/test_requirements_in_sync.py`).
- `client/api/` and `client/node/` are git-ignored and must exist before
  building. The Node version lives in `client/node_download_config.py`.
  Details: `docs/reference/build-and-setup.md`.
- `macos/` is a Mac-only layer the Windows build never runs; what it asks of
  a Windows change: `docs/reference/macos.md`.

## Architecture

- **`client/`** — Python/wxPython: all UI, logic, persistence, sounds.
- **`client/api/`** — WPPConnect Server, vendored and not committed; WinZapp's
  changes live in `client/api_patches/`.
- **node_modules patches**: four files patch the compiled
  `@wppconnect-team/wppconnect` — `client/core/wppconnect_host_layer_patch.py`,
  `client/core/wppconnect_status_layer_patch.py`,
  `client/core/wppconnect_sender_layer_patch.py`,
  `client/core/wppconnect_welcome_layer_patch.py`. A fifth,
  `client/core/wppconnect_wa_js_patch.py`, patches `@wppconnect/wa-js`'s
  bundle, the script injected into the WhatsApp Web page.
- **The big classes are split into mixins.**
  `client/main.py` (~1,900 lines) keeps `MainWindow.__init__`, `init_UI` and
  startup; its methods live under `client/main_window/`.
  `client/ui/conversations.py` (~1,200 lines, `ConversationsPanel`) is split
  into `client/ui/conversation_panel/`, and `client/status_panel.py` into
  `client/status_tab/`. Each package's `__init__.py` is the map — **grep
  those packages first**; the method you need very likely exists. A mixin
  never imports `main`.

### JID handling — the recurring source of bugs

- `@s.whatsapp.net` — canonical phone JID; normalize everything to it.
- `@c.us` — legacy phone form; normalized on load (`_normalize_jid`).
- `@lid` — linked-device id, **not a phone number**. Bridge through
  `_lid_to_phone`/`_phone_to_lid` (`client/main_window/identity.py`) before
  display, send or lookup. Brazilian numbers are 8/9-digit interchangeable.
- `@g.us` — group. A group JID's digits are never a participant's digits; a
  self-chat echo can carry a participant's `@lid` digits suffixed `@g.us`.
- `@broadcast` — statuses, never a conversation. `@newsletter` — ignored.
- Sends go to `@lid` when known (`_resolve_jid_for_send`), falling back to
  `@c.us` only on a definite refusal.

### Sync

**Only a genuine I/O fault belongs in `message_failures`.** A definite server
answer ("no messages", `chat not found`) is not a failure, or the account
stays "not synced" forever. Follow the two existing exceptions
(`_delta_unsatisfied_chats`, `_absent_chats`); never add a third mechanism.
`docs/traps/sync-completion.md`.

### Paths, data, logs

- Paths come from `client/app_paths.py` (`resource_path()`, `data_path()`,
  `log_path()`) — never hardcoded.
- WA_token only through `MainWindow._get_wa_token()`/`_set_wa_token()`
  (`client/core/token_vault.py`).
- Never log a JID, phone number or contact name unredacted
  (`docs/traps/log-pii.md`).
- For a user report ask for both `log.log` (truncated every launch) and
  `shutdown_audit.log` (the previous run's ending) —
  `docs/reference/diagnosing-from-logs.md`.

## Agent skills

The `mattpocock-skills` plugin provides `/grill-with-docs` → `/to-spec` →
`/to-tickets` → `/implement` → `/code-review`, plus `/triage` and
`/diagnosing-bugs`. WinZapp's bounds on it:

- Tests: the files for what changed, never `--run-wx-gui`.
- Commit only when the user asks, whatever `/implement` says.
- **Open every PR as a draft** and keep it a draft until the reviewer
  (the `winzapp-reviewer` agent) has finished and approved. Only
  then mark it ready for review or merge it. This applies to every developer
  and every Claude session, so nobody merges a PR while its review is still
  running.
- The "coding standards" `/code-review` looks for are this file,
  `docs/traps/` and `.claude/skills/`.

Issues: GitHub Issues on `gabrielhhaber/WinZapp_Python` via `gh`
(`docs/agents/issue-tracker.md`). Triage labels: the five defaults
(`docs/agents/triage-labels.md`). Domain docs: `CONTEXT.md` + `docs/adr/`,
created by `/domain-modeling` (`docs/agents/domain.md`).
