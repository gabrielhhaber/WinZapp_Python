# Live address-book and profile names

`contact-state-changed` is an account-scoped Socket.IO event with a serialized
person JID in `contactId`, never a group ID or a message key.

These are observations, not phone-write acknowledgements. See
[phone-contact-sync.md](phone-contact-sync.md) for the save action's limits
and the distinction between frontend contact models and persisted state.

Two kinds separate saved names from public nicknames:

- `kind: address-book`, `name`, `saved: boolean`, optional
  `syncedToPhone: boolean`. An explicit removal has `saved: false` and clears
  the remote book name and phone-sync flag. An absent phone-sync flag preserves
  existing knowledge; it does not invent successful phone synchronization.
- `kind: profile`, `name` updates only `pushName`. It never marks the person
  as saved and never overwrites their address-book name.

WPPConnect observes ContactStore changes to name, isMyContact,
syncToAddressbook and pushname. Related book changes in one model batch coalesce
before reading the final flags. Installation and collection hydration do not
emit. Unknown fields, empty saved names and non-person identities are rejected.

Neonize's existing ContactUpdateEv handler stores the book name before emitting
the observation; full-sync replay remains silent. An action without either name
field is not removal. saveOnPrimaryAddressbook is forwarded only if present.
PushNameUpdateEv now stores and announces the primary identity's profile name;
the existing alternate phone/LID update remains in place.

The client dispatches to the main thread, resolves known phone/LID aliases and
Brazilian number variants, updates contact records and uses the existing batch
contact persistence and view refresh paths. User-saved WinZapp names keep their
precedence across aliases. Removal from the remote book retains the contact
record, public nickname, conversation and message history; it never sends an
address-book action back to WhatsApp. Shutdown and other-account notices are
ignored. No names or raw identifiers are logged by these observers.

## Validation

On 2026-11-14 the exact observer source ran temporarily over CDP with a native
contact model belonging to the authorized development group's participant list.
Its current book state and profile nickname produced the two expected kinds
with the correct contact identity. This invoked handlers over existing native
state; no real contact was saved, renamed or removed. The model was verified
unchanged and temporary listeners/bindings were restored.

Node tests cover coalescing, saved/removal/profile payloads, rejection and
disposal. Python tests cover alias/variant behavior, local-name protection,
nickname preservation, unknown removals, account/shutdown guards and bridge
storage-before-notification/full-sync rules. Neonize was not started. Real
cross-device address-book changes and rebuilt delivery to the running client
remain later live validations.
