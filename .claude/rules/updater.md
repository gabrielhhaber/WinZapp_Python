---
paths:
  - "client/updater.py"
  - "client/update_coord.py"
  - "client/version.py"
  - ".github/workflows/*.yml"
  - ".github/scripts/*.py"
  - "client/core/release_signature.py"
  - "client/core/release_keys.py"
---

# Updater

**Read `docs/traps/updater-channels.md` and `docs/traps/release-integrity.md` before changing these files.** Short form:

Alpha = the literal word 'alpha' in the tag; version = stable base + commit count, and a date-shaped version would strand every stable user forever. The next stable must bump patch or higher. Keep `alpha-release.yml` and `release.yml` in sync. Releases are immutable and signed; stable keys live offline.
