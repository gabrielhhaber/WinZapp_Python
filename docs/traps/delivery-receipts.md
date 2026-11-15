# Delivery receipts: "sent" that never becomes "delivered"

The sender of a message sees it sit at one tick forever, and no amount of
waiting or opening the conversation changes it. Reported against the Neonize
engine on 2026-11-15 with the WinZapp window unfocused.

The mechanism, from `whatsmeow/receipt.go` (the checkout `neonize_patches/`
builds on): whatsmeow answers every message it handles with a delivery
receipt, but only sends it typeless — which WhatsApp renders as the two gray
ticks — once the client is considered online; otherwise it sends
`type="inactive"`, which the official apps receive but **do not render**.
`cli.SendPresence(types.PresenceAvailable)` is what flips the internal
`sendActiveReceipts` 0→1, and `SendPresence(PresenceUnavailable)` flips it
back 1→0. WinZapp binds presence to the *window*: every blur, minimize, tray
or hidden state, and again every 20 s while none of that resolves, writes
`set-online-presence(false)`. Every message arriving in one of those windows
got an inactive receipt, so the sender never saw delivered — and with read
receipts disabled there is no later event that would fix the rendering.

The fix (2026-11-15, `neonize_bridge/server.py`): client creation now calls
`SetForceActiveDeliveryReceipts(true)`, which stores 2 in the same atomic;
both `SendPresence` directions only `CompareAndSwap(0,1)` and `CompareAndSwap(1,0)`,
so the forced value survives every window flap — receipts are active whatever
the window does, while presence stays bound to it. Both approved neonize
builds (0.6.0 and 0.10.0) export `SetForceActiveDeliveryReceipts`; the call is
guarded anyway so an install whose build lacks the export degrades to the
pre-fix behaviour (with an ERROR in `bridge.log`) instead of the bridge thread
dying and the app hanging on "connecting" forever.

Diagnosing a report of this symptom, by engine:

- **Neonize**: an ack reaches the UI as `onack` → `on_wpp_ack` →
  `on_message_status_update`. The bridge logs one line per received receipt
  (`receipt DELIVERED[ mine] (N message(s)) in <chat> -> ack 2 ids=...`, in
  `neonize_bridge/run/bridge.log`); a receipt is not logged at all if the
  message could not be decrypted (this is by design — no receipt, ever, and
  the sender stays on one tick until the message is readable or is revoked).
  Ask for `bridge.log` and `log.log` and compare: if the DELIVERED line is
  there but the sender still shows one tick, the sender's app is refusing to
  render it — the window/presence binding is not the cause. If the DELIVERED
  line is missing, the message never got handled; look for decryption
  failures or for `delivery receipts could not be forced active` in the same
  log.
- **WPPConnect (WhatsApp Web)**: the page decides receipts by its own
  foreground rule — the same behaviour as when a browser tab is in the
  background — and there is no outsider hook to override it. WinZapp tells the
  page "unavailable" whenever the window loses focus, so every message
  arriving then gets an inactive receipt from *WhatsApp Web* itself and the
  sender stays on one tick. This is the same symptom through a different
  engine and it is not fixable from outside WhatsApp Web; the lever is
  WinZapp's own presence binding, which is a deliberate privacy decision and
  was left unchanged. Users affected by this on WPPConnect should use the
  Neonize engine (Settings) — the Neonize path decouples the delivered tick
  from the window as described above.

What this note deliberately does not say: that anything about *reading* a
message fixes the tick. Read receipts are a separate receipt (`read`/`played`)
and are independently suppressible by the account's privacy setting
(`read_receipts.py`); the inactive/active rule above is about delivery only.
