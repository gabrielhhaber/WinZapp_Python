# Remote Status audience observations

The optional `status: {setting, list}` extends `privacy-settings-changed` with
contact, allow-list or deny-list and a complete audience. The event validator
rejects malformed/non-person IDs, missing lists and groups. Phone forms normalize
to the existing API's @c.us contract; LIDs remain LIDs, without guessed phone
numbers. No audience is logged. Unknown or incomplete values never reset it.

WPP observes writes to STATUS_PRIVACY_SETTING, STATUS_ALLOW_LIST and
STATUS_DENY_LIST in userPrefsIdb, including raw and native hashed keys. The
loaded WAWebStatusPrivacySettingSync source confirms remote mutations call
bulkSetItemsToIndexedDB directly, bypassing setStatusPrivacyConfig. The final
observer therefore watches storage, not just the local action. It reads mode
and the appropriate list through WAWebUserPrefsStatus. Malformed list members
invalidate the whole audience rather than silently narrowing it. Related writes
coalesce, newer reads supersede old asynchronous reads, and disposal restores
the original methods and suppresses pending emissions.

Neonize reuses the mirror's unique AppStateMutationEv handler for the exact
regular_high index [status_privacy]. Explicit mode/list fields translate through
the existing status_privacy contract. Removing the index reports contacts and
an empty list, matching the read route. Hydration replay and malformed partial
actions are silent. No second handler or running Neonize session is created.

The open panel advances its remote baseline but preserves a pending choice or
picked audience. Newer observations supersede slow reads; old account contexts
are ignored. No event sends a privacy change or opens any window.

Validation on 2026-11-15: the exact reviewed observer ran temporarily via CDP
and read a recognized mode and complete list from native state, emitting one
coalesced notice and restoring all original methods. No privacy option changed.
Node, Python client and bridge fixture tests also pass, including malformed
lists, async ordering and disposal. Actual cross-device changes and rebuilt
client delivery remain future live validations; the bridge was not started.
