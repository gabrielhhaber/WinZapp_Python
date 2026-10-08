# Architecture and test ownership

The authoritative contribution rules are in [CLAUDE.md](../../CLAUDE.md).
Each subsystem's measured failure history is in `docs/traps/`. Read the
relevant trap before changing its implementation or regression tests.

## Runtime boundaries

| Responsibility | Source | Regression coverage |
| --- | --- | --- |
| Startup and main window composition | `client/main.py`, `client/main_window/__init__.py` | `test_god_file_split_structure.py`, startup and connection tests |
| Conversation composition and native controls | `client/ui/conversations.py`, `client/ui/conversation_panel/__init__.py` | panel switching, list rows, composer and message tests |
| Status composition, remote read and cache merge | `client/status_panel.py`, `client/status_tab/` | `test_status_panel_api.py`, `test_reconcile_my_status_cache.py` |
| Async encrypted SQLite and synchronous calls | `client/core/database.py`, `database_bridge.py` | database, bridge, timeout and encrypted-storage tests |
| Socket.IO normalization and live/history funnels | `client/core/websocket_client.py`, `client/main_window/message_events.py` | websocket, message identity, echo and placeholder tests |
| Initial/incremental synchronization and older history | `client/main_window/sync.py`, `backfill.py`, `history.py`, `conversation_sync.py` | sync, absent-chat, retry, backfill and history tests |
| Stable command IDs, overrides and conflicts | `client/core/keyboard_shortcuts.py`, `shortcut_catalog.py` | `test_keyboard_shortcuts.py`, `test_shortcut_table_catalog_coverage.py` |
| Native accelerator and direct key routing | `client/ui/shortcut_bindings.py`, `shortcut_names.py` | `test_keyboard_shortcut_scenarios.py`, per-control shortcut tests |
| Staged shortcut preferences | `client/ui/dialogs/shortcuts_tab.py`, `shortcut_capture.py` | shortcut scenarios and settings-dialog tests |
| Local transcription and explicit remote AI | `client/core/transcription/`, `client/core/ai_media/` | transcription and AI-media tests; these are separate consent and storage paths |
| Account and install coordination | `client/accounts.py`, `app_settings.py`, `coord_locks.py`, `node_coord.py`, `update_coord.py` | account, shared-settings, leases and updater tests |
| Node server customizations | `client/api_patches/`, `client/core/wppconnect_*_patch.py`, `setup_api.py` | patch, Node VM, restore-list and drift tests |
| Translation sources and packaged resources | `translations/`, `winzapp_tools/translations.py`, `client/core/translation_catalog.py` | gettext, language synchronization and key-reference tests |
| Packaging and release integrity | `build.py`, `winzapp_tools/build_env.py`, `.github/workflows/`, `installer/` | build, dependency pin, signing, workflow and installer tests |
| Mac-only adaptations | `macos/winzapp_mac/` | Windows-side Mac contract/provenance tests; native Mac checks run on macOS |

The Python client owns UI, identity rules and persistence; it uses HTTP and
Socket.IO to communicate with a separate WPPConnect Node process. One account
has its own client, Node lease, port and window. `client/api/` is generated
and ignored by Git: maintain server changes in `client/api_patches/`.

## Composition and invariants

The three large UI classes combine responsibility-specific mixins. Look in
their package indexes before adding methods to the entry files. Mixins must
not import `main`; a module global belongs to the module defining the method.
Tests patch those globals through `tests/god_modules.py`, and invoke unbound
methods on minimal stubs when native construction is unnecessary. Pure logic
belongs in plain modules with direct tests. Structure tests enforce size
budgets and duplicate-method/import constraints.

Phone JIDs normalize to `@s.whatsapp.net`; `@lid` identities require learned
phone mappings, and group digits must never be treated as participant digits.
Only genuine I/O faults enter synchronization failures. A timeout during a
send may already have delivered the message and must not trigger a blind
retry. Speech passes through `MainWindow.speak_output`; message rows are
updated by identity instead of clearing the list and re-announcing focus.

## Settings and translation ownership

`SettingsDialog` keeps fixed early page indices for existing callers. After
Reactions it conditionally adds Locked chats, then AI, Transcription and
Shortcuts. Shortcuts is constructed by its mixin. With Locked chats visible
there are 18 pages; hiding it leaves 17. AI, Transcription and Shortcuts use
`FindPage()` for translation; adding a page must update both real-notebook
tests and any structural tests that inspect only inline `AddPage()` calls.
Test parents for incoming-call dialogs must provide `settings` and `i18n`,
since button accelerators now read the account's shortcut preferences.

The locale registry is `client/languages/language_map.json`, not a hardcoded
count. PO files are authoritative; MO files and `winzapp.keys` are generated.
Follow [the gettext workflow](gettext-migration.md). A changed default needs
the appropriate existing-account migration before defaults are backfilled;
an idempotent corruption repair has a different contract.

## Verification boundaries

Run touched test modules serially in the existing environment. At most one
completed serial full run is needed for a cross-cutting change. Do not use
`-n auto`, create another environment or enable GUI tests on the maintainer's
desktop. `hidden_frame()` is the native off-screen frame factory; real dialog
tests retain the `wxgui` marker and run in CI.

Default local pytest skips real dialogs, synthetic load scenarios and opted-out
remote network checks. Tests also skip unavailable platform tools and absent
generated API trees. Record the actual pass/fail/skip counts and environment
limitations. A local run cannot establish live WhatsApp delivery, NVDA speech,
cross-device propagation or native macOS behavior.
