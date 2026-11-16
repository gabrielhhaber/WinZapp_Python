# Profile and account preference event audit

This audit records what the current client can observe and where live effects
are complete or partial. Protobuf names below were read from the prepared
Neonize Python package's descriptors, without reading session data.

## Covered

- Contact address-book and profile nickname changes already use
  `contact-state-changed`. WPP observes `ContactStore` name, saved/contact,
  sync-to-address-book and pushname fields; Neonize handles `ContactUpdateEv`
  and `PushNameUpdateEv`. The client applies them to contact records and
  existing contact views. See `contact-state-events.md`,
  `client/api_patches/src/util/contactStateBridge.ts:3-46`,
  `neonize_bridge/contact_events.py:202-255`, and
  `client/core/remote_contact_state.py:27-85`.
- Profile photo changes already use `profile-picture-changed`. WPP watches
  `ProfilePicThumbStore` metadata and Neonize's existing `PictureEv` handler
  emits identity-only invalidation. The contact/chat cache path is complete.
  The open own-profile panel had no effect; this change now re-reads its
  existing `my-profile` route and updates only the photo baseline. See
  `profile-picture-events.md`,
  `client/api_patches/src/util/profilePictureBridge.ts:3-34`,
  `neonize_bridge/profile_picture_events.py:5-9`, and
  `client/core/remote_profile_picture.py:35-47`.
- The six account privacy choices, Status audience, and Keep chats archived
  already have cross-engine observations and panel behavior. The
  `privacy-settings-changed` handler intentionally recognizes the six choices;
  Keep chats archived is an optional boolean. See
  `privacy-setting-events.md`, `keep-archived-events.md`, and
  `neonize_bridge/privacy_events.py:8-26`.
- Per-chat mute, archive, pin, disappearing timer and message-state events
  already have individual client effects. They are chat-scoped and do not
  represent the user's global defaults.

## Present in Neonize, no safe client feature to extend

- `SyncActionValue.userStatusMuteAction.muted` exists. There is no
  `MuteStatusEv` in the installed `neonize.events` surface, and the
  `AppStateMutationEv` mirror does not currently retain or decode this action.
  WPP's status route deliberately includes muted `StatusV3Store` models but
  serializes no mute state (`statusController.ts:378-410`). The status tab has
  no mute control or muted section (`client/status_tab/`), so there is no
  established display behavior to keep synchronized. This is not integrated.
- `PrivacySettingsEv.CallAddChanged` exists, but the account privacy panel has
  no call-privacy choice and `privacy_events.CHANGED` excludes that flag.
  `CallAddChanged` is therefore not currently a user-visible setting that an
  event can update.
- `SyncActionValue.settingsSyncAction` covers general app settings and has no
  default disappearing-message duration field. The UI has no global default
  message-timer control; the existing timer setting is per chat. No new
  control or guessed default was added.

## Own name and About text

The Profile settings panel reads the account's own name, About and photo via
`my-profile` (`profileRuntime.ts:13-42`) but does not subscribe to remote
changes (`whatsapp_profile_panel.py:118-139`). `SyncActionValue.pushNameSetting`
contains a name, and the existing `PushNameUpdateEv` carries a JID and new
pushname. However, the current WPP contact observer watches `ContactStore`
contact `pushname`, not an explicitly identified own-profile model; Neonize's
current handler records a contact nickname and does not establish that the
event is the account's own profile update. Neither is a verified cross-engine
contract for refreshing the Profile panel's name. No update was inferred from
contact identity or added.

About text has no field in the inspected `SyncActionValue` profile settings;
`PrivacySettingsEv.StatusChanged` describes who may see the About value, not a
change to the About text itself. The current handler only emits the six
supported privacy values. A remote About-text panel refresh therefore remains
unavailable from a verified event contract.

## Evidence limits

The CDP `/json/version` endpoint timed out during this audit, so no target list
or page state was read. No live account changes were made and no WhatsApp or
Neonize process was started. The own-photo panel refresh is grounded in the
existing identity-only picture event and existing `my-profile` read route;
actual cross-device delivery still needs later live validation.
