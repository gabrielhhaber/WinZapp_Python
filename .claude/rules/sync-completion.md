---
paths:
  - "client/main.py"
  - "client/core/incremental_sync.py"
  - "tests/test_*sync*.py"
  - "tests/test_*backfill*.py"
---

# Sync completion

**Read `docs/traps/sync-completion.md` before changing these files.** Short form:

Only a genuine I/O fault belongs in `message_failures`; one wrongly failed chat holds the whole account 'not synced' forever. On-demand history requests notify the user's phone and are strictly bounded. The incremental round only reads chat-list metadata (compare `t` forward-only, `>`).
