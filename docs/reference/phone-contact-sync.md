# Saving a contact and requesting phone synchronization

`save-contact` saves a WhatsApp contact with `syncAddressBook: true`.
The resulting `syncToAddressbook` flag is a synchronization preference, not
a receipt proving that the primary phone wrote its OS address book. Do not
announce that the phone completed synchronization based on that flag.

`phone_contacts.save_contact()` requires an explicit boolean `isMyContact:
true` before reporting a saved contact. Empty/malformed HTTP success bodies,
error envelopes and a synchronization flag without a saved contact are not
successful saves. `SaveResult.synced` retains its existing field name for
compatibility, but means that phone synchronization was requested/enabled.
The success announcement and tab explanation describe that limitation.

The existing local sync marker also controls remote editing/removal: it keeps
a contact accepted by WhatsApp on the WhatsApp action path, even while phone
synchronization is unconfirmed. It must not be interpreted as a physical
phone completion receipt. Removing that marker without separating ownership
would allow deletion to remove only the WinZapp record and leave WhatsApp's
contact behind.

## Read-only CDP inspection, 2026-11-15

An anonymously selected contact had a saved name, `isAddressBookContact: 1`
and `syncToAddressbook: true` in WhatsApp's internal contact database. The
frontend ContactStore initially showed no saved name and false contact/sync
flags for its known phone and LID aliases. Reading the contact through
`WPP.contact.get()` subsequently returned the saved contact and sync flag.
Reading only ContactStore would therefore have produced the wrong diagnosis.

The loaded `WAWebSaveContactAction` saves through
`WAWebContactEditSync.sendContactUpdate`, which writes the contact and queues
the app-state mutation. This path sets `syncToAddressbook` from the requested
option. It does not return a phone address-book completion receipt.
`isContactSyncCompleted` is also unsuitable: the same action marks records
complete during its number/contact query and then writes the saved record
with that field reset. It cannot prove an OS address-book write.

No contact was saved, renamed or removed during this inspection. No session
was restarted and Neonize was not started. The user's reported absence on
the phone remains a cross-device issue; this fix makes the announcement
accurate and rejects unproven save responses. It does not establish why the
phone has not written or displayed the contact.

## Full LID fix and iPhone confirmation, 2026-11-15

The user clarified that the contact was absent from WhatsApp's own contacts on
the iPhone linked to this account. Repeating `WPP.contact.save` with the same
saved name and phone-sync option did not fix that absence. The contact app-state
collection was UpToDate, with no pending mutations. Primary features reported
contact backup, address-book permission and native-contact setup enabled.

Comparing the loaded native drawer and WA-JS 4.7.2 revealed an argument mismatch:
the drawer passes a complete LID Wid to `WAWebSaveContactAction.saveContactAction`;
WA-JS passes only `wid.user`. The native contact-sync serializer calls
`lid.toString()` to populate `contactAction.lidJid`. A read-only construction
and protobuf decode confirmed the library path omitted the `@lid` domain,
while the native Wid retained it. Both otherwise requested phone synchronization.

One authorized save through the native action, preserving the existing name and
using the full LID Wid, completed. The collection advanced and had no pending
contact writes. The user then confirmed that the contact appeared correctly on
the iPhone. This supports the LID mismatch as the cause of this report; it does
not turn a future save response into a general phone-completion receipt.

`contactSaveController.ts` now calls the object-form native action with full
mapped Wids for WhatsApp versions at or above the same boundary WA-JS uses,
2.3000.1030209354. Older versions retain the positional/legacy library path.
Unknown versions or unavailable native dependencies fail explicitly. An error
after the native write never retries through the malformed library path.
Unmapped LIDs cannot be treated as phone numbers. Neonize already writes the
full `lidJID` string in `address_book_fields`; it was not started or live-tested.

The regression test executes the real controller and its serialized Puppeteer
callback in separate Node VM contexts. It checks full Wid serialization, both
identity forms, sync on/off, the version boundary, missing mappings, safe errors
and absence of duplicate writes after write/read failures. No personal contact
details are stored in the test, this report or the commit.

The compiled patched callback was also serialized and evaluated over CDP with
its write intercepted, without modifying global WPP bindings. Its arguments
were passed to the loaded native mutation builder and decoded with the loaded
protobuf implementation: the LID was a native Wid, the wire `lidJid` exactly
matched the full mapped LID and `saveOnPrimaryAddressbook` remained true. No
additional contact write was sent by this check.
