---
paths:
  - "client/api_patches/src/util/createSessionUtil.ts"
  - "client/main.py"
---

# Session startup

**Read `docs/traps/session-startup.md` before changing these files.** Short form:

Background launch skips the dialogs, never the wait for Node. A dead Chrome holding `userDataDir` loops CLOSED forever: the stale-lock recovery kills by that directory, once, and only when no live client owns the session slot. Two `create()`s can be in flight for one session.
