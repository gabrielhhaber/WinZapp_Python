---
paths:
  - "client/core/profile_recovery.py"
  - "client/api_patches/src/util/createSessionUtil.ts"
  - "client/api_patches/src/controller/sessionController.ts"
---

# Profile recovery

**Read `docs/traps/profile-recovery.md` before changing these files.** Short form:

The Chrome profile is the only copy of the WhatsApp login. Ask the browser to close before killing it; `restore_snapshot()` only with the session closed; never offer a profile state that was already refused; the teardown runs at `WM_QUERYENDSESSION`, not `WM_ENDSESSION`.
