---
paths:
  - "client/core/utils.py"
  - "client/data/settings_default.json"
  - "client/app_settings.py"
---

# Settings migrations

**Read `docs/traps/settings-migrations.md` before changing these files.** Short form:

A changed default reaches new installs only; existing ones need a one-shot migration with its own flag in `settings['general']`, run before `backfill_missing_defaults()`. Voice notes are their own media category (`is_voice_message()`, checked only for audio-capable types).
