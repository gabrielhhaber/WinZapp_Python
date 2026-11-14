# Live contact block state

`block-state-changed` is account scoped and has two shapes:

- `{session, kind: contact, contactId, blocked}`: an explicit person-JID delta
  with a strict boolean. Groups, broadcasts and channels are never contacts.
- `{session, kind: refresh}`: no individual delta is known, so request the
  existing read-only block-list route. It never means to clear the list.

WPP observes ContactStore.change:isContactBlocked and BlocklistStore.reset.
Collection add/remove is not used as evidence of blocking or unblocking.
Neonize's BlocklistEv follows the installed proto and whatsmeow events.go:
Action DEFAULT (1) supplies Changes, each with BlockAction BLOCK (1) or UNBLOCK
(2); Action MODIFY (2) supplies no changes and requires re-querying. Unknown
actions are ignored. The existing single-handler registration guard covers it.

The client applies known-phone deltas using its existing local block/persistence
path, without sending a block action. Known LIDs resolve through the mapping;
unresolved LID digits are never treated as phone numbers, and remain for the
normal block-list/identity synchronization to resolve. Unblocking removes both
Brazilian digit forms. Lookup by known LID also resolves the phone now.

Opaque refreshes coalesce on the background executor, capture the account/sync
context before fetching, and repeat once if another notice arrives during the
fetch. Existing context guards refuse stale-account results. Local block changes
increment a revision so an older in-flight poll cannot undo the newer state.
Shutdown notices do nothing; this introduces no new connection or sync-failure
mechanism and does not create contacts.

## Validation

CDP on 2026-11-14 invoked the exact observer with an existing participant model
of the authorized development group, then a detached object with its opposite
flag, and the opaque refresh callback. It emitted false, true and refresh with
the correct person identity. The real native blocked flag remained unchanged;
no participant was blocked or unblocked. Temporary observers were removed.

Node tests cover explicit deltas, reset, payload rejection, disposal and no
eviction-derived unblocks. Python tests cover aliases, Brazilian forms, unknown
LIDs, account/shutdown/context ownership, refresh coalescing and stale polls.
The actual registered Neonize handler is driven with fixtures, without starting
a bridge. Real cross-device block operations and rebuilt client delivery remain
later live validations.
