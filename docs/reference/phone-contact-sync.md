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
