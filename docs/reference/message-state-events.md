# Account message-state events

These are WinZapp events, shared by its WPPConnect patch and Neonize bridge.
They observe account state rather than introducing a second star/delete action.
Both use the Socket.IO root namespace with account/session filtering.

## Conversation settings

`chat-settings-changed`: `{session, chatId, setting, value}` is a partial
observation. `setting` is `mute`, `archive` or `pin`; a mute value is its Unix
expiry in seconds (`0` = unmuted, `-1` = indefinite), while archive/pin values
are strict booleans. No absent field is interpreted as a reset.

- WPPConnect observes `MuteStore.change:expiration`, `ChatStore.change:archive`
  and `ChatStore.change:pin`. The mute model's `id` and the chat model's `id`
  are the conversation JID, never a message or participant key. The chat's
  derived `muteExpiration` may lag behind MuteStore, so it is not observed.
  An undefined pin on an explicit pin-change event is the native unpinned state.
  Collection hydration, removal and initial installation emit no snapshots.
  The common page owner handles replacement, reloads and close cancellation.
- Neonize now emits mute changes after updating storage, excluding full-sync
  replay and actions without an explicit `muted` field. Its established
  `chats-update` archive/pin notifications remain compatible. For indefinite
  mute the bridge keeps its existing positive storage sentinel, and emits `-1`.
- The client validates account, conversation, setting and value before handing
  off to the main thread. Mute uses the existing local alias-aware persistence
  path; archive and pin use the existing chat-update paths. Expired mute values
  clear the local mute. No server action, focus change or speech is introduced.

On 2026-11-14 the exact source observer was installed temporarily over CDP.
Mute/unmute, archive/unarchive and pin/unpin were performed on the authorized
development group. All six expected notices arrived with matching conversation
identity and state. The group's original settings were verified restored, then
all temporary listeners and bindings were removed. Neonize and the rebuilt
Node-to-client integration were not started; remote-device receipt remains a
later live validation. Node and Python tests cover payload rejection, listener
ownership, client dispatch and bridge storage-before-notification behavior.

## Phone chat lock

`chat-settings-changed` also accepts `setting: lock` with a strict boolean.
WPPConnect observes only `ChatStore.change:isLocked`; Neonize updates storage
before notification and suppresses historical full-sync replay. The client uses
the existing keyed-fingerprint index for both phone/LID identities, closes an
affected open view and invalidates sensitive media/translation work. Unlocking
on the phone does not unlock WinZapp's own vault.

CDP validation on 2026-11-14 used `WAWebChatLockAction` to lock/unlock only the
authorized development group. Both notices matched and the original lock,
archive and pin states were restored. No native UI or Neonize session was opened.

## Disappearing-message timer

`chat-timer-changed`: `{session, chatId, duration, settingTimestamp}` uses seconds
for both the duration and Unix setting timestamp. Zero duration explicitly turns
the timer off; absent or invalid durations never do. WPPConnect coalesces changes
to ChatStore's `ephemeralDuration` and `ephemeralSettingTimestamp` within one model
batch, and also observes `GroupMetadataStore.change:ephemeralDuration`. In Meta's
split runtime the latter changes while the conversation field can remain stale;
group metadata carries no setting timestamp, represented as zero (unknown).
Collection hydration emits nothing. Neonize emits after storing an explicit
EPHEMERAL_SETTING notice or GroupInfo ephemeral change; inferred timers from
ordinary messages and history hydration do not emit. The client updates known
conversation aliases and schedules persistence, ignoring known older timestamps.

The initial CDP test on 2026-11-14 was refused in the development group with
`you_are_not_allowed_set_group_property`. The user then authorized another named
group where this account is an administrator. A real change from off to 86400
seconds exposed the metadata-only update. After adding its observer, the exact
source captured 86400 then 0, both with the matching group identity. The original
off setting was verified restored and temporary observers/bindings were removed.
Node tests reproduce the metadata-only change and verify disposal. Neonize and
rebuilt Node-to-client delivery were not started; cross-device receipt remains
a later live validation.

## Message pin notices

`message-pin-changed`: `{session, chatId, messageId, pinned, timestamp, duration}`.
The target is the raw ID of the pinned message; the separate pin-notification
MsgKey is never used as that target. `timestamp` is sender time in milliseconds;
duration is seconds. Unknown pin types are ignored, never treated as unpin.

The current Meta runtime does not hydrate PinInChatStore. The WPP observer wraps
`WAWebDBCRUDOperationsPinInChat.createOrUpdatePinInChat` and emits only the rows
the native function reports committed; rejected and stale writes emit nothing.
Its returned rows also include forced unpins when the maximum is exceeded.
Native return values, errors and property descriptors are preserved.
Neonize emits after its existing received-pin handler updates storage.
The client updates matching resident flags and the open overview, and atomically
changes only the existing disk row's pin metadata. Older observations cannot
overwrite newer ones. Unknown targets refresh the open pin overview from local
linked-device storage, without requesting phone history. Locked views stay hidden.

CDP on 2026-11-14 pinned a new marked message in the development group for one
day and then unpinned it. The public WA-JS wrapper is incompatible with the
current native object's argument shape, so the test used the inspected native
`WAWebSendPinMessageAction.sendPinInChatMsg({msg, pinType, pinExpiryDurationOption})`.
The exact observer captured unpin, pin (86400 seconds), unpin, each with the
correct target. Storage confirmed unpinned afterward; the temporary wrapper and
bindings were removed. Neonize live receipt remains untested.

## Conversation deletion

`chat-deleted`: `{session, chatId, until}` names the latest covered message's
Unix second. WPPConnect wraps `WAWebDeleteChatSync.applyMutations`, accepts only
explicit SET deleteChat indexes with an available message range and successful
native results. A generic ChatStore removal/eviction never deletes client history.
Neonize emits after its existing storage cleanup, suppressing full-sync replay;
it prefers the explicit message range over the mutation's operation timestamp.
Newer bridge messages prevent cleanup.

The client normalizes account/chat identity, checks both stored history and
resident aliases for newer messages, and closes only the affected open view.
It reuses local deletion, durable deleted-chat markers and lock cleanup, without
sending any delete or leave-group action. Failed disk checks suppress cleanup.
This is a deletion notice, not evidence inferred from an absent chat in a poll.

CDP on 2026-11-14 inspected the actual sync index, result and range contract.
The exact observer ran in an isolated fake-native context using the authorized
group identity: one success emitted one notice, failed/orphan/remove cases did
not. The real group's history was verified unchanged. A real destructive
cross-device deletion and Neonize live delivery remain untested.

## Favorites

`message-star-changed`: `{session, chatId, messageId, starred}`. `chatId` is
the conversation JID; `messageId` is the opaque raw message ID, never a
serialized MsgKey or a participant JID. `starred` must be an explicit boolean.

- WPPConnect's `messageStarBridge.ts` observes `MsgStore.change:star`. Its page
  binding sends only identifiers and the flag, with no message content. The
  installer owns one listener per store and updates the owner on replacement;
  reloads retry initialization, closing the page cancels retries.
- Neonize's existing `MessageStarEv` handler updates its stored flag before
  emitting. Full sync hydrates the bridge without replaying historical notices;
  an absent protobuf star field is unknown, never an unstar instruction.
- The client changes resident flags and paints only the matching open chat.
  The encrypted database uses its existing atomic star merge, including for
  unloaded rows; a star event cannot create a message or replace its content.
  Legacy local stars and newer observations keep their established precedence.
  Locked chats are not repainted. No action is sent back to WhatsApp.

## Delete for me

`message-deleted-for-me`: `{session, chatId, messageId}` uses the same
conversation/raw-message identity as favorites. A group participant is never
used as the target chat. This is an explicit deletion notice, not a message
missing from a fetched page or evicted from an in-memory collection.

- WPPConnect observes successful `deleteMessageForMe` sync mutations and
  accepted local `WPP.chat.deleteMessage` results. Failed/orphan mutations,
  delete-for-everyone results and generic collection removals emit nothing.
  Native return values and errors are preserved. An accepted local deletion
  can return `isDeleted: false` before its model disappears; in that case the
  observer checks both message collections for at most ten seconds without
  repeating the action. Disposal cancels checks and restores property descriptors.
- Neonize's existing `MessageDeleteForMeEv` handler deletes the bridge row
  before emitting; full-sync notices are not replayed.
- The client filters by account, normalizes the conversation JID and resolves
  known phone/LID aliases. It removes the exact message from resident history
  and disk, updates the preview and uses the existing panel-removal path for
  playback, selection and focus. It never sends a second delete action.
  Whole-chat deletion and clearing are separate actions; this handler never
  infers either from a missing message.

## Validation

On 2026-11-14 a marked test message in the user-authorized development group
was starred and unstarred over CDP. The exact in-page observer produced the two
expected identifier/boolean payloads; the live store read back true then false.
Native star setters return the old flag, so verification used the model's
current state. Node tests cover serialization, reinstallation, replacement
ownership and close/retry guards; Python tests cover both engine payloads and
the client's memory/database behavior. The TypeScript observer was type-checked.

Two additional marked messages were deleted for me in that same development
group. Both native calls returned `OK` with `isDeleted: false` before removal.
With the final observer, the stores subsequently lost the message and exactly
one event matched its raw ID and conversation; no pending checks remained.
Temporary CDP observers and native wrappers were removed afterward. Other
participants retain these test messages, as expected for delete-for-me.
The remote-device sync branch was tested with native-shaped fixtures, not a
second physical device.

Neonize was not started. Cross-device receipt and the rebuilt Node binding's
delivery to an updated running client remain later live validations. No
existing messages or other conversations are used for destructive tests.
