# Account privacy observations

`privacy-settings-changed`: `{session, settings}` contains recognized string
values for lastSeen, online, about, profilePicture, readReceipts and groupAdd.
Missing, unknown and invalid values never reset another setting. Optional
keepArchived and status audience members are described in
[keep-archived-events.md](keep-archived-events.md) and
[status-privacy-events.md](status-privacy-events.md). Preference-storage keys
are never exposed in the event.

The current Meta runtime stores USER_PRIVACY_SETTINGS in userPrefsIdb. The WPP
observer wraps only set/applySyncSet/bulkSetItemsToIndexedDB, matching that raw
or natively hashed preference key, plus the archive and Status audience keys
described in the linked contracts. Other preference writes pass through untouched.
After native completion it coalesces a batch and reads the public normalized
getUserPrivacySettings() values. Synchronous and asynchronous return types,
values, errors and original method descriptors survive. Initialization emits
nothing, removal/unknown state does not invent defaults, and disposal cancels
pending notices. The observer never calls a privacy setter itself.

Neonize forwards only fields named by PrivacySettingsEv's explicit Changed
flags, translating its existing enum contract. Unknown enum values are omitted.
A known read-receipts notification updates the bridge's last-known receipt mode.
There is still only one registered handler for each event type.

The client records the latest six values and optional archive/Status observations,
with revisions and the current
account/sync context. Queued old-account observations are dropped. Open privacy
panels are weakly registered and refresh only choices still matching their loaded
baseline; user edits remain selected while the remote baseline advances. Late
initial reads and apply read-backs retain newer observations. Existing wx choices
are reused, with no new controls, focus changes, windows, or outbound settings
requests caused by an observation. State is not persisted as account preferences.

## Validation

CDP on 2026-11-14 installed the exact observer temporarily and invoked its read
notification twice in one batch. One notice contained the six expected field
names. No settings were changed; before/after native values matched. Temporary
hooks/bindings were restored. This validates normalized observation over native
data, not an actual cross-device privacy change or rebuilt client delivery.

Node tests execute the actual source for native write hooks, watched/unrelated
keys, sync/async behavior, coalescing, rejection and disposal. Python tests use
plain panel/choice stubs for user edits, revisions and account ownership; no wx
window is constructed. Existing privacy/accessibility tests and bridge fixture
tests also pass. Neonize was not started.
