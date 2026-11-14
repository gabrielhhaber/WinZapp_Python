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
