# Remaining event integration audit

Audit date: 2026-11-15. Two Luna agents investigated separate areas serially;
the principal reviewed the changes and added account/race/failure guards.
This is an inventory of the inspected event surfaces, not a claim that every
future WhatsApp feature or native event has a corresponding WinZapp feature.

## Gaps closed in this batch

- Own profile photo: `profile-picture-changed` already had WPP/Neonize
  producers and cache invalidation. The open own-profile settings panel now
  refreshes only its photo baseline through the existing `my-profile` read.
  Name/About fields and a selected local photo remain drafts. Account context,
  shutdown, panel lifetime, overlapping reads and thread-start failures are
  guarded. See `profile-preference-event-audit.md` and `profile-picture-events.md`.
- Chat/list label associations: both producers already send `onupdatelabel`.
  `createSessionUtil.onLabelUpdated` includes the session, and config enables
  its subscription. Neonize's `label_events.association` emits after updating
  its mirror and skips message-label associations and full-sync replay. The
  client now subscribes, validates session/chat/add-or-remove/list shapes,
  and refreshes the existing custom-list snapshot. It never infers audience,
  phone/LID equivalence or membership from partial payloads. The existing
  readback resolves membership using the same account's canonical list rules.

The association refresh retains the selected filter, refreshes an open list
manager without changing focus, and blocks list writes until a fresh snapshot.
An event overtaking an active read discards that snapshot and schedules one
coalesced follow-up. Queued old-account callbacks cannot refresh a new account.
The handler lives in `core/remote_label_updates.py`; serialized read jobs remain
owned by `main_window/chat_lists.py`. No association is written by an observer.

## Existing coverage checked

Message reactions, edits, poll votes, stars, message pins, deletions for self,
chat clear/delete and read state already have producer/consumer paths. Chat
mute/archive/pin/lock and per-chat timers are integrated. Group metadata and
participant updates, contact names, picture invalidation, block state, the six
account privacy fields, keep-archived and Status audience are also integrated.
See their event contracts under `docs/reference/` and `neonize_bridge/server.py`.

Channels already have `NewsletterChangeEv` plus join/leave/mute observations.
`channel_events.WINDOW_OPS` defines the supported changes, and the existing
client refreshes the Channels and administrators/followers windows. Other
operation names are not assumed to describe a supported UI action.

## Remaining investigations and dependencies

| Area | Evidence and current limit |
| --- | --- |
| List/label rename, create and delete | `AppStateMutationEv` and the mirror retain `label_edit`; custom-list routes already read/write it. The inspected WPP `onUpdateLabel` binding originates in `chat.update_label`, an association event. A reliable remote metadata notification has not yet been verified. A future LabelStore observer needs a measured hydration/update contract; no guessed trigger was added. |
| Group membership requests | Listing and approve/reject routes exist, but the installed `GroupInfoEv` has no explicit membership-request field. `UnknownChanges` is not treated as a request without a verified decoder. The existing window still queries when opened. |
| Additional channel operations | Only the existing `WINDOW_OPS` have a mapped window effect. Other posts/milestones/partnership/reply operations need an existing UI consumer and a verified payload before integrating. |
| Muted Status contacts | A protobuf mute action exists, but the Status tab has no established mute state/control and the WPP route does not serialize that state. Requires feature/UI design as well as an event. |
| Global default message timer | The implemented timer is per chat. No matching global control and complete cross-engine event contract were identified in this audit. |
| Additional call privacy | Neonize exposes `CallAddChanged`; no matching control exists among the six account privacy fields. |
| Own name/About changes | Profile reads exist, but no complete cross-engine own-profile notification contract was verified. Contact nicknames and About visibility are not equivalent to these fields. |

These are unresolved integrations, not declarations that WhatsApp cannot support
them. Do not label them complete merely because a route or a protobuf exists.

## Validation and deferred live checks

All checks in this batch use plain stubs, fake workers/queued callbacks and
synthetic identifiers. No window was created, live account setting changed,
message sent or engine restarted. Both existing event producers are reused;
Neonize was not started. No CDP mutation or live cross-device test was performed.

After the updated WinZapp is running, test the already-integrated archive and
Status-audience settings from the phone. Then change the own profile photo and
check the open Profile panel's photo state while preserving unsaved fields.
Add/remove a development-group chat to/from a custom list on the phone and
check the selected filter and open list manager. Restore the original settings.
Repeat delivery on Neonize only when the user opens that engine later.
