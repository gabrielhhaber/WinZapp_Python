---
paths:
  - "client/main.py"
  - "client/core/incremental_sync.py"
  - "tests/test_*sync*.py"
  - "tests/test_*backfill*.py"
  - "client/main_window/sync.py"
  - "client/main_window/backfill.py"
  - "client/main_window/history.py"
  - "client/main_window/conversation_sync.py"
  - "client/main_window/chats_store.py"
  - "client/main_window/message_rules.py"
  - "client/main_window/contacts.py"
  - "client/ui/conversation_panel/history_loading.py"
  - "neonize_bridge/omitted_history.py"
  - "neonize_bridge/older_history.py"
  - "neonize_bridge/history_resume.py"
  - "neonize_bridge/recent_conversations.py"
  - "client/main_window/wpp_recent_conversations.py"
---

# Sync completion

**Read `docs/traps/sync-completion.md` before changing these files.** Short form:

Only a genuine I/O fault belongs in `message_failures`; one wrongly failed chat holds the whole account 'not synced' forever. On-demand history requests notify the user's phone and are strictly bounded. The incremental round only reads chat-list metadata (compare `t` forward-only, `>`). Older chats beyond WhatsApp's 399-chat `INITIAL_BOOTSTRAP` cap arrive via `OmittedConversationMetadata` (only if `supportOmittedConversationMetadata` field 28 was advertised at pairing time) with 0 local messages and fetch their first page on open/Home via `INITIAL_REVERSE_HISTORY` (`mode = 1`).
