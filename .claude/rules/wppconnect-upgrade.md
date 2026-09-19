---
paths:
  - "wpp_minimum_version.txt"
  - "client/api_patches/package.json"
  - "setup_api.py"
---

# Wppconnect upgrade

**Read `docs/traps/wppconnect-upgrade.md` before changing these files.** Short form:

Bumping `wpp_minimum_version.txt` means diffing upstream against `CUSTOM_ROOT_FILES + CUSTOM_SRC_FILES`. `_PATCHED_DEPENDENCY_KEYS` is deliberately narrow; stale entries in `api_patches/package.json` outside it are inert and must not be 'fixed'.
