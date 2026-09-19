---
paths:
  - "client/ui/dialogs/connect.py"
  - "client/core/websocket_client.py"
  - "client/core/wppconnect_host_layer_patch.py"
  - "client/api_patches/src/config.ts"
---

# Pairing flow

**Read `docs/traps/pairing-flow.md` before changing these files.** Short form:

`connection_dial` can only be closed from `show_pairing_dial()` after its own `ShowModal()` returns. Pairing codes are rate-limited per phone number and the quota outlives the process; an unattended code stream got an account banned — every branch that reacts to a code must go through `_pairing_attended()`.
