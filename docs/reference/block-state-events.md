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

The user subsequently authorized a real named contact for toggling. Its phone
and LID records were confirmed aliases before acting. Contrary to the expected
initial state, both frontend and stored block state were unblocked; a successful
native server refresh returned `match` and confirmed that state. The explicit
live test therefore ran block, unblock, block via WPP.blocklist, using the LID
identity required by the migrated native blocklist. Each accepted call changed
the native model and produced exactly one matching contact notice: true, false,
true. Storage confirmed the final blocked LID, and a final successful server
refresh again returned `match`. Temporary bindings/listeners were restored.
No contact identifier or name is included in this record. This validates real
same-session actions and observer receipt, not a second device's delivery.

Node tests cover explicit deltas, reset, payload rejection, disposal and no
eviction-derived unblocks. Python tests cover aliases, Brazilian forms, unknown
LIDs, account/shutdown/context ownership, refresh coalescing and stale polls.
The actual registered Neonize handler is driven with fixtures, without starting
a bridge. Real cross-device block operations and rebuilt client delivery remain
later live validations.
