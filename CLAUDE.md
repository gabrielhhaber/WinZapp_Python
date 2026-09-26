# CLAUDE.md

Guidance for Claude Code when working in this repository. This file holds only
what applies to *every* task; the reasoning behind each rule — measured
incidents, discarded hypotheses, log excerpts — lives under `docs/` and is
read when you touch that area (see the index at the end and `.claude/rules/`).

## Rules that protect users — read before running anything

1. **A plain `pytest` must never open a window.** WinZapp is maintained by
   blind developers; a test window taking focus steals whatever they had open
   and has crashed NVDA. Frames go through `tests/conftest.py`'s
   `hidden_frame()`; modules that build a real `Dialog` carry the `wxgui`
   marker and are skipped by default. **`--run-wx-gui` (or
   `WINZAPP_RUN_WX_GUI_TESTS=1`) is for CI only.** Never pass it on a
   developer machine and never tell an agent, script or helper to pass it —
   background agents run on the user's own desktop. The same goes for ad-hoc
   scripts that construct a wx window or hook WinEvents. Enforced by
   `tests/test_no_desktop_visible_windows.py`; history in
   `docs/traps/tests-never-open-windows.md`.
2. **Every user-facing string goes into every registered locale file**
   (`client/languages/{pt-BR,pt-PT,en-US,es-ES,pl,tr-TR}.json`; the set is
   data, driven by `language_map.json`, not a hardcoded count). `I18n.t()` has
   no per-key fallback: a missing key renders as the raw key name. Reuse the
   words that locale already uses for the concept (`docs/reference/i18n-terminology.md`).
   `tests/test_language_files_in_sync.py` enforces it.
3. **A new function or fix ships with a test in the same change**, in the
   repo's style: pure logic extracted, or the unbound method bound onto a
   plain stub (`MainWindow`/`ConversationsPanel` cannot be instantiated
   without a wx.App). Async tests are plain `async def` (`asyncio_mode=auto`).
4. **Only edit `client/api_patches/`, never `client/api/`** — the latter is
   a vendored checkout that `setup_api.py`/`build.py` overwrite from the
   patches. Third-party JS inside `node_modules` is patched by the
   `client/core/wppconnect_*_layer_patch.py` modules instead (see below).
5. **All speech goes through `MainWindow.speak_output`** (via
   `MainWindow.output()`); UI is plain wx controls only, list mutations
   inside `Freeze()`/`Thaw()`, titles and rows show names, never raw JIDs.

## What this is

WinZapp is a free, self-hosted Windows desktop WhatsApp client built for
**accessibility** (NVDA/JAWS/Narrator through `accessible_output2`). A Python
3.13 + wxPython GUI process drives a locally-run **WPPConnect Server**
(Node.js, cloned and built from `wppconnect-team/wppconnect-server`) that is
the actual WhatsApp Web gateway. They talk over local HTTP REST
(`http://127.0.0.1:6300/api/...`) and Socket.IO. One account per process;
several accounts run in parallel, each with its own Node, port and window.

## Commands

```powershell
uv sync && uv run setup-api            # fresh checkout (uv fetches Python 3.13); setup-api clones+builds client/api/
python -m venv venv; .\venv\Scripts\Activate.ps1; pip install -r requirements.txt -r requirements-dev.txt; python setup_api.py   # venv route — must keep working
uv run winzapp                         # or: cd client; python main.py   (ALWAYS from client/, or a second empty data/ install appears)
pytest                                 # from repo root; pytest.ini sets pythonpath=client, asyncio_mode=auto
pytest tests/test_database.py::TestChats::test_upsert_chat_creates_record
uv run build-installer                 # onedir: WinZappInstaller.exe + WinZapp.zip   (or: venv\Scripts\python.exe build.py)
uv run build-onefile                   # single-file WinZapp.exe                       (or: build.py --onefile)
```

`pyproject.toml`/`uv.lock` and `requirements.txt` pin the same versions; change
both and run `uv lock` (`tests/test_requirements_in_sync.py`). The bundled Node
version is named once, in `client/node_download_config.py`. `client/api/` and
`client/node/` are git-ignored and must exist before building. Details:
`docs/reference/build-and-setup.md`.

## Architecture

- **`client/` (Python/wxPython)** — all UI, logic, persistence, notifications,
  sounds. Almost every change lands here.
- **`client/api/` (Node/TypeScript, vendored, not committed)** — WPPConnect
  Server. WinZapp's patched files (`start.js`, `config.json`, `src/config.ts`,
  `src/index.ts`, several `src/{controller,middleware,util}/*.ts`,
  `src/routes/index.ts` — the list is `CUSTOM_ROOT_FILES + CUSTOM_SRC_FILES`
  in `setup_api.py`) are tracked in `client/api_patches/` and re-applied on
  every clone. `package.json` is merged, not restored
  (`_merge_package_json_dependencies()`).
- **node_modules patches**: four files patch `@wppconnect-team/wppconnect`'s
  own *compiled* `node_modules` output (not WPPConnect Server's own source, so
  they can't go through the `client/api_patches/` mechanism above) —
  `client/core/wppconnect_host_layer_patch.py` (pairing-code lifecycle,
  `host.layer.js`), `client/core/wppconnect_status_layer_patch.py`
  (status-post reporting, `status.layer.js`),
  `client/core/wppconnect_sender_layer_patch.py` (attachment sending, chunked
  transfer, `sender.layer.js`) and `client/core/wppconnect_welcome_layer_patch.py`
  (`welcome.js`). Each is idempotent search-and-replace applied from two call
  sites that must stay in sync: `setup_api.py` and
  `ApiSetupDialog._apply_node_modules_patches()` (`client/ui/dialogs/api_setup.py`).
  Never edit a shipped `_V<n>` constant in place — add a version and a
  migration (`docs/traps/large-media.md`).
- **Send contract**: wppconnect/wa-js are exact pins in
  `api_patches/package.json`; every send endpoint passes through
  `auditSendResult()` and Python through `core/send_contract.py` — a 201
  without a real message id is a failure. `docs/traps/send-contract.md`.
- **MainWindow is split into mixins**: `client/main.py` (~1,900 lines) keeps
  only `__init__`, `init_UI` and startup; every other method lives in one
  module per responsibility under `client/main_window/` (sync, connection,
  sending, calls, identity, chat list, …; the map is that package's
  `__init__.py`). `client/ui/conversations.py` (`ConversationsPanel`, ~17,500
  lines) holds the message list and composer. **grep `client/main_window/` and
  them first** — the method you need very likely exists. New code goes into
  the module that owns the responsibility, or a new module — never back into
  `main.py` (see "Keep files small" below).

### Message pipeline (short form — `docs/reference/message-pipeline.md`)

1. `client/core/websocket_client.py` normalizes WPPConnect events into the
   canonical dict `{"key": {"remoteJid","fromMe","id","participant"?},
   "message", "messageType", "messageTimestamp", "pushName"}`.
2. `MainWindow.on_new_message()` (live) and `on_historical_message()`
   (history) are the two funnels; both gated by `_live_events_ready()`.
   `is_countable_message()` keeps system events out of badges/sort/notify;
   `_is_undecrypted_placeholder()` drops a live `ciphertext`; one a sync stored is
   shown and later replaced by its decrypted copy (`message-pipeline.md`). An edit
   arrives under the *original* `key.id` and goes to `_apply_possible_edit()`.
3. Sends: `client/ui/conversations.py` shows a virtual pending message
   (`_local_pending`, `_local_id`), `client/core/message_queue.py` calls
   `MainWindow.send_*`; the echo comes back through `on_new_message` and is
   matched to the pending message **by type**. Ambiguous failures (timeout,
   5xx) are never resent.
4. `client/core/database.py` is async aiosqlite (payloads Fernet-encrypted
   with `data/secret.key`); `client/core/database_bridge.py` is the sync
   façade with a timeout so a stuck coroutine cannot freeze the app.
5. `client/core/video_player.py` plays video (ffmpeg frames) and audio (BASS).

### JID handling — the recurring source of bugs

- `@s.whatsapp.net` — canonical phone JID; everything is normalized to it.
- `@c.us` — legacy phone form still in some WPPConnect responses; normalized
  on load (`MainWindow.deduplicate_chats`, `_normalize_jid`).
- `@lid` — linked-device id, **not a phone number**. Bridge through
  `_lid_to_phone`/`_phone_to_lid` (`main.py`) before display, send or contact
  lookup. Brazilian numbers also need 8/9-digit interchangeability.
- `@g.us` — group. Not trustworthy alone: a self-chat echo can carry a
  participant's `@lid` digits suffixed `@g.us`. Invariant: a group JID's
  digits are never a participant's digits (`deduplicate_chats()` guard,
  `on_new_message()` redirect).
- `@broadcast` — statuses, `_store_status_update`, never a conversation.
  `@newsletter` — channels, ignored.
- Sends go to `@lid` when known (`_resolve_jid_for_send`), falling back to
  `@c.us` only on a definite refusal. Message ids for WPPConnect lookups are
  built by `_serialize_msg_id` and keep whatever form the store used.

### Sync — the one rule that keeps being rediscovered

`message_sync_ok = not message_failures` gates persisting the sync state.
**Only a genuine I/O fault belongs in `message_failures`**; a definite server
answer ("no messages", `chat not found`) is not a failure, or the account stays
"not synced" forever and resyncs out loud. Two bounded exceptions already exist
(`_delta_unsatisfied_chats`, `_absent_chats`) — follow their shape, never
invent a third mechanism. Every on-demand history request notifies the user's
phone, so they are strictly bounded. Full reasoning: `docs/traps/sync-completion.md`.

### Paths, config, data

- `client/app_paths.py`: `resource_path()` for bundled assets, `data_path()` /
  `log_path()` for runtime data — never hardcode. `client/config.py` loads an
  optional `.env` (`WINZAPP_GITHUB_REPO`, …).
- Runtime data: `messages.db`, `secret.key`, `settings.json` (seeded from
  `client/data/settings_default.json`), `voice_messages/`, `media/`. Changing
  a default needs a one-shot migration with its own flag
  (`docs/traps/settings-migrations.md`).
- `languages/language_map.json` is the source of truth for locales.
  `core/locale_format.py` reads Windows regional format;
  `core/utils.get_downloads_folder()` resolves the real Downloads folder.
- WA_token: only through `MainWindow._get_wa_token()`/`_set_wa_token()`
  (`client/core/token_vault.py`, Fernet, portable by design — not DPAPI).
- Logs: `log.log` is truncated every launch; `shutdown_audit.log` is
  append-only and is where the *previous* run's ending is. Ask for both
  (`docs/reference/diagnosing-from-logs.md`).

### Multi-account modules

`client/accounts.py`, `client/account_ui.py`, `client/account_launcher.py`,
`client/account_bootstrap.py`, `client/account_migration.py` (account manager);
`client/ipc.py` (foreground/quit between processes); `client/node_coord.py`,
`client/node_ports.py` (one Node port per account); `client/session_store.py`;
`client/connection_state.py` (resume/suspend/single-flight);
`client/coord_locks.py`; `client/app_settings.py`, `client/window_title.py`,
`client/update_coord.py`; `client/api_patches/src/middleware/auth.ts`.

### Other places

`client/status_panel.py` (Alt+5, `docs/reference/status-tab.md`);
`client/calls_panel.py` (Alt+6, every call record of every chat; logic in
`client/core/call_log.py`, `docs/reference/message-pipeline.md` 3a);
`client/updater.py` (`docs/traps/updater-channels.md`);
`client/core/release_signature.py` (`docs/traps/release-integrity.md`);
`build.py` + `installer/` (PyInstaller via CLI args, no spec file; onedir
also compiles the C installer stubs); `client/changelog_*.txt`
(`docs/reference/writing-changelogs.md`).

## Before touching an area, read its trap file

| Area / files | Read first |
|---|---|
| Sync, backfill, `incremental_sync.py`, `message_failures` | `docs/traps/sync-completion.md` |
| Pairing, QR, `connect.py`, `host_layer_patch` | `docs/traps/pairing-flow.md` |
| `profile_recovery.py`, session teardown, `WM_QUERYENDSESSION` | `docs/traps/profile-recovery.md` |
| `createSessionUtil.ts`, `ensure_wpp_running()`, background launch | `docs/traps/session-startup.md` |
| `start.js`, WhatsApp Web version, `wa-version` | `docs/traps/whatsapp-web-version-pin.md` |
| `wpp_minimum_version.txt`, `api_patches/package.json` | `docs/traps/wppconnect-upgrade.md` |
| Media download/upload, size limits, `sender_layer_patch` | `docs/traps/large-media.md` |
| Send endpoints, `send_contract.py`, `messageController.ts` | `docs/traps/send-contract.md` |
| Voice calls (`call_audio.py`, `callMediaBridge.ts`, `callController.ts`) | `docs/traps/voice-calls.md` |
| `speak_output`, `focus_cloak.py`, list-row repaints | `docs/traps/screen-reader-speech.md` |
| `sound_system.py`, BASS devices | `docs/traps/audio-devices.md` |
| `updater.py`, `update_coord.py`, release workflows | `docs/traps/updater-channels.md`, `docs/traps/release-integrity.md` |
| Settings defaults, `_migrate_settings()` | `docs/traps/settings-migrations.md` |
| `tests/conftest.py`, anything creating a wx window in a test | `docs/traps/tests-never-open-windows.md` |
| Logging a JID, phone number or contact/pushname anywhere | `docs/traps/log-pii.md` |

## Agent skills

The `mattpocock-skills` plugin (enabled in `.claude/settings.json`) provides
the idea → ship flow: `/grill-with-docs` → `/to-spec` → `/to-tickets` →
`/implement` → `/code-review`, plus `/triage` and `/diagnosing-bugs`. Three
WinZapp-specific bounds on it:

- "Run the full test suite" means a plain `pytest` — **never** `--run-wx-gui`
  (rule 1 at the top of this file).
- `/implement` says "commit your work"; here a commit happens only when the
  user asks for one.
- The "documented coding standards" `/code-review` looks for are this file,
  `docs/traps/` and the project skills in `.claude/skills/`; there is no
  `CONTRIBUTING.md`.

### Issue tracker

GitHub Issues on `gabrielhhaber/WinZapp_Python`, via `gh`. See `docs/agents/issue-tracker.md`.

### Triage labels

The five default roles as-is (`needs-triage`, `needs-info`, `ready-for-agent`,
`ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root, created lazily by
`/domain-modeling`. See `docs/agents/domain.md`.
