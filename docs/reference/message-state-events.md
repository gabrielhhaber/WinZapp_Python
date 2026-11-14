# Account message-state events

These are WinZapp events, shared by its WPPConnect patch and Neonize bridge.
They observe account state rather than introducing a second star/delete action.
Both use the Socket.IO root namespace with account/session filtering.

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
