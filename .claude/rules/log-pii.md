---
paths:
  - "client/core/pii_redaction.py"
  - "client/core/api_client.py"
  - "client/api_patches/src/util/logger.ts"
---

# Logging phone numbers, LIDs and contact names

**Read `docs/traps/log-pii.md` before changing these files.** Short form:

A JID's digits are the contact's phone number. `redact_phone()`/`redactPhone()` mask any digit run (8-20, so group JIDs count too) plus its JID suffix, wired into the ONE formatter both sides funnel through — not into individual call sites, which cannot be kept complete. It does not catch a bare contact name with no JID next to it; that needs its own "shape, not content" fix at its own call site.
