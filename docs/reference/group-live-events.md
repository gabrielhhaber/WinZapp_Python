# Live group metadata and membership

Both engines use `groups.update`: `{session, data: [updates]}`. Each update
names a group conversation in `id` (serialized JID, or a legacy Wid object).
Description/settings/membership changes are accepted without a group subject.
Known fields are patched independently; absence never resets another field.

- `subject` is a nonempty name; `desc` is text, including empty text for removal.
- `announce` and `restrict` are explicit booleans. They describe admin-only
  sending and admin-only editing, respectively; neither is WhatsApp Chat Lock.
- Neonize already emits participant deltas: `participants: [person JIDs]` plus
  `action: add|remove|promote|demote`. Phone and LID aliases are reconciled only
  through known mappings; group IDs are never participant IDs.
- The WPP observer listens to GroupMetadataStore's changed subject, description
  and settings. A null native description on an explicit change is removal.
  For membership, each model owns a listener: native
  `triggerParticipantsChange()` emits no model argument. Its serialized snapshot
  carries `participantsComplete: true` and `{id,isAdmin,isSuperAdmin}` entries.
  Initial attachment and collection hydration do not emit snapshots; incomplete
  or empty hydration does not erase the roster. Removing a model detaches it.

The client preserves the existing subject-update path and live-sync gate. It
updates known group metadata and admin flags, invalidates the short group-info
cache, recomputes/persists sending permissions and refreshes the matching open
composer. Membership notices invalidate stale mention suggestions and update
the participant count/call limit only when the roster is known. An unknown
roster remains unknown until the existing group-info lookup supplies it.
Locked views are not repainted, other chats are untouched, and no group action
or focus change is sent in response to an observation.

## Validation

On 2026-11-14 the exact observer source was installed temporarily over CDP in
the authorized development group. Field handlers were invoked with its existing
native metadata model, and native `triggerParticipantsChange()` produced the
complete 11-person snapshot. The four payloads contained the matching group ID,
and description/settings/membership were verified unchanged. Listeners/bindings
were restored afterward. This is observation/simulation of existing state, not
a real remote rename, permission change, promotion or membership change.

Node tests execute the actual source and cover hydration, removal, empty
description, serialization and listener cleanup. Python tests cover partial
updates, phone/LID deltas, admin permissions, cache invalidation, account/sync
guards, unknown rosters and locked views. Neonize was not started; its existing
group event contract is exercised by its fixture-based suite. Rebuilt delivery
to the user's running client and real cross-device group changes remain later
live validations.
