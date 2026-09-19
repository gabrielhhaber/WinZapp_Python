---
paths:
  - "client/api_patches/src/controller/messageController.ts"
  - "client/core/send_contract.py"
---

# Send contract

**Read `docs/traps/send-contract.md` before changing these files.** Short form:

`auditSendResult()` never throws (the message is already on the network; a 500 would be retried and duplicate it). The verdict rides in the 201 body; read the ACK before the error string.
