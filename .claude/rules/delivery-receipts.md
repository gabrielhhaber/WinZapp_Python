---
paths:
  - "neonize_bridge/server.py"
  - "neonize_bridge/read_receipts.py"
  - "neonize_bridge/group_receipts.py"
  - "neonize_bridge/receipt_ids.py"
  - "client/core/websocket_client.py"
  - "client/main_window/chat_events.py"
  - "client/main_window/window_lifecycle.py"
  - "client/main_window/message_ack.py"
  - "client/core/message_ack.py"
  - "client/ui/conversation_panel/message_rendering.py"
  - "client/api_patches/src/util/createSessionUtil.ts"
---

# Delivery receipts

**Read `docs/traps/delivery-receipts.md` before changing these files.** Short form:

whatsmeow marks the delivery receipt `type="inactive"` while the client is not
online (`sendActiveReceipts == 0`); the official apps receive it but never
render it, so the sender is stuck on one tick forever. WinZapp binds presence
to the window, so every blur/minimize flips that state. Never revert the
`SetForceActiveDeliveryReceipts(true)` call in client creation — it stores 2, and
both `SendPresence` directions only swap 0↔1, so receipts stay active under
any window flap; presence stays bound to the window. A guard failure degrades
to the pre-fix behaviour with an ERROR, never a dead bridge thread. On the
WPPConnect engine the same symptom comes from WhatsApp Web's own foreground
rule and has no outsider hook — documented, not patched. An inactive is not
"read" — the read receipt is a separate, privacy-suppressed event.
