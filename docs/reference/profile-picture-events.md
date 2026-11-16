# Profile picture invalidation

`profile-picture-changed`: `{session, chatId}` covers person and group pictures.
It invalidates cached fallback URLs, not the user's downloaded image files.
Neither a signed URL nor picture content travels in this event. Missing local
thumbnail data does not assert that another person removed their picture.

WPP observes explicit ProfilePicThumbStore tag/URL/direct-path changes,
coalescing a model batch. Initial hydration and collection removal emit nothing.
Neonize emits from its existing PictureEv handler, including Remove and group
notices. Its legacy fetched-person-photo update remains compatible.

The client normalizes known phone/LID/Brazilian aliases and clears only existing
contact/chat photo caches. It never creates a contact or downloads an image
because an event arrived. The next explicit view queries the current picture.
An API success with `response: null` no longer retries the old fallback URL;
network/server failures retain the established fallback behavior.

For the account's own JID, an already open WhatsApp Profile settings panel also
re-reads `my-profile` on a worker and refreshes only whether a photo is set.
Bursts coalesce to one in-flight read plus at most one follow-up read; an older
read is discarded if another photo notice arrived, account/sync changes discard
the result, and no request is made without an open panel. Name and About drafts,
and a locally chosen photo file, are left untouched.

CDP validation on 2026-11-14 invoked the exact observer over the authorized
development group's existing native thumbnail. Two same-batch observations
produced one identity-only notice; the model was unchanged. Temporary observers
and bindings were removed. Real remote picture replacement/removal and Neonize
delivery remain untested. Node/Python tests cover coalescing, cleanup, aliases,
shutdown/account guards, null-picture fallback and bridge picture events.
