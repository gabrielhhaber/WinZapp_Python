---
paths:
  - "client/api_patches/start.js"
---

# Whatsapp web version pin

**Read `docs/traps/whatsapp-web-version-pin.md` before changing these files.** Short form:

Interception and the pin are the same mechanism and must stay document-only. Never run unpinned, except that a pinned document which is not WhatsApp's full app (the login-only page) is never served at `/`: WhatsApp answers it and the session runs the live build (trap doc, "Never serve the login document at `/`"). The catalogue expires; a newest build is not necessarily one wa-js can drive — compare sessions within one log before blaming the pin.
