# Remote keep-archived observations

The optional boolean `keepArchived` extends `privacy-settings-changed`. Its
absence never changes the checkbox. In an open WhatsApp Privacy panel, a remote
change advances the baseline while preserving an edited checkbox. Newer events
win over slow initial reads and apply read-backs. Old account contexts and
shutdown callbacks are ignored. Observation never sends a setting mutation.

WPP observes successful set/applySyncSet/bulkSetItemsToIndexedDB writes to the
archive-v2 and unarchive-chats preference keys (raw and native hashed forms),
then reads both native getters. Incoming WAWebArchiveSettingSync applies those
same preferences, as confirmed by its loaded source via CDP. The result is
archiveV2Enabled && !unarchiveChats. Related writes coalesce; revision checks
reject overlapping stale reads and disposal suppresses pending notifications.
Original method descriptors and sync/async results are preserved.

Neonize reuses the single AppStateMutationEv handler. The exact regular_low
index [setting_unarchiveChats] updates the mirror before emitting the inverse
boolean. An explicit removal uses the existing route's default, true. Missing
fields and full-sync replay produce no event. No chat or person IDs are needed.

Validation on 2026-11-15: the principal reviewed and adjusted both Luna changes.
Node tests exercise actual hooks over fake storage, unrelated writes, coalescing,
async ordering, disposal and return/error preservation. Client tests cover drafts,
revisions and account context. Bridge fixtures test serialized mutations without
starting Neonize. CDP installed the exact reviewed observer temporarily and
invoked its read notification twice over current native state: one notice had
a boolean keepArchived, six privacy fields and a recognized Status audience.
All original methods were restored. No account preference was changed. Actual
cross-device toggles and rebuilt delivery remain later live validations.
