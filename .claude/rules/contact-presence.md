---
paths:
  - "client/core/contact_presence*.py"
  - "client/core/websocket_client.py"
  - "client/main_window/contact_presence.py"
  - "client/main_window/presence_work.py"
  - "client/main_window/presence_visibility.py"
  - "client/main_window/profile_presence.py"
  - "client/ui/dialogs/personal_presence*.py"
  - "client/main_window/chat_events.py"
  - "client/main_window/identity.py"
  - "client/ui/conversation_panel/contact_presence.py"
  - "client/api_patches/src/util/contactPresenceRuntime.ts"
  - "client/api_patches/src/util/contactPresenceBridge.ts"
  - "client/api_patches/src/util/groupPresenceBridge.ts"
  - "client/api_patches/src/util/createSessionUtil.ts"
---

# Contact presence refresh and privacy

Before changing presence refresh, cancellation or permission propagation, read
[the measured refresh and visibility traps](../../docs/traps/contact-presence-refresh.md).
Preserve the scoped, bounded deferred request, cache revision checks and explicit
denial semantics. Alt+T stays cache-only; never probe a real account in a test.
HTTP pending with explicit denial must carry a clear-only snapshot to both
guarded cache paths; it asserts no offline state and authorizes no alias retry.
Persistent-pending recovery has an epoch/contact budget; ready-null, denial,
malformed/unsupported responses and errors cannot authorize it. Navigation or
subscription failure must not reset or bypass that budget.
The native field bridge is local Socket.IO only. Preserve legacy webhook
payloads; do not add native last-seen data to external webhook delivery.
Initial POST completion can authorize one pending-only short read per
contact/epoch, with all deferred scope and privacy guards still applied.
An expired UI wait does not kill a blocking thread. Preserve the per-window
physical/logical job cap across epochs; late workers cannot mutate successor
state or resume alias POSTs. Keep group/profile-worker policies separate.
Focus return, current private-dialog activation and native activity expiry may
request missing data through the same coordinator. Keep exact-visit/reciprocal
reader ownership and stale explicit denial checks. HTTP activity expiry cannot
renew itself. Own online writes and typing speech still require main activation.
Profile presence has a separate timer-free physical/pending cap. Epoch changes
cannot release its running slots. Capture routing and reject late returns/UI
delivery. Post profile fields before its presence continuation; current-chat
readers own their own request, while standalone dialogs retain one bounded read.
Group activity has an independent page-owned local binding. Keep its generation,
store/model checks and finite retry cancellation. Claim legacy suppression only
after successful local delivery; reset/remove/malformed state must release it.
Legacy callbacks capture page/client/request ownership and reject stale contexts.
The prior group webhook shape stays separate; private native snapshots never go there.
