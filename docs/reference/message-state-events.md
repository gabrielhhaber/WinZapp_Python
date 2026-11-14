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

## Validation

On 2026-11-14 a marked test message in the user-authorized development group
was starred and unstarred over CDP. The exact in-page observer produced the two
expected identifier/boolean payloads; the live store read back true then false.
Native star setters return the old flag, so verification used the model's
current state. Node tests cover serialization, reinstallation, replacement
ownership and close/retry guards; Python tests cover both engine payloads and
the client's memory/database behavior. The TypeScript observer was type-checked.

Neonize was not started. Cross-device receipt and the rebuilt Node binding's
delivery to an updated running client remain later live validations. No
existing messages or other conversations are used for destructive tests.
