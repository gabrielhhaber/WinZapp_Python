# Joining an ongoing group call

The conversation's Join button replaces the voice/video start buttons when WhatsApp's
`WAWebVoipOngoingCallCollection.getByChat` reports a call for that group.
The visible group is checked every ten seconds, with at most one probe in
flight. Hidden chats make no requests. Closing or changing chats invalidates
late replies and stops the old timer. Discovery never initializes VoIP or
acquires audio devices; it waits for the runtime already warmed at startup.
Failed checks hide the button without speaking. Repeated results do not
rewrite an unchanged label, and hiding a focused button moves focus to the
message field, never away from another control.

The authenticated `call/group/ongoing`, `call/ongoing`, `call/participants`
and `call/group/join` endpoints live in `callController.ts`; the serialized
native adapter is `groupCallRuntime.ts`. A join without `callId` or a chat
(`chatJid`/`groupJid`) is refused with 400 before anything touches the page.
The adapter calls `joinOngoingWAWebVoipGroupCallPN` with the same options as
WhatsApp's Calls-tab Join button (`joinAndAccept: true`, `CALL_LOG`), after
checking the call id, chat, media type and absence of another active/pending
call; that check runs before the media bridge is enabled. It requires the
selected call to reach the live call store and never falls back to an offer
or retries the join. It waits 5 s for the call to connect, extended to 15 s
while that call is still dialling (`CALLING`, `LINK`, `PRE_CALLING`, `CALL_B_STARTING`); an
ended or failed call stops the wait at once. Failed cleanup cannot disable
the bridge of a different call: it stops the bridge only when the page holds
no call or this call, and no outgoing call is pending. Python reuses the
existing cancellable call worker and audio lifecycle. When a join fails,
Python also asks the page to end that exact call id (`call/ensure-ended`), so
a join that connects after "join failed" was spoken never leaves the user in
a call with no window; that request is skipped when WinZapp holds that call
id again by then (the late join was adopted, or the user joined again). Video is joined with the camera off, using the same
camera probe as Answer without video.

This does not add a participant picker to start
calls in groups exceeding the existing 32-member starting-call limit.
The visibility checks use the existing free function
`core.conversation_view.conversation_in_view(panel)`, not a method on
ConversationsPanel. Tests expose wx's actual `IsShown()` interface on both the
panel and its detail pane; a stub `conversation_in_view` method would hide a
crash in the real opening path. Poll, completion and click all use
the shared helper, including when only the detail pane is hidden.

## Ongoing filter in Calls

Calls (Alt+6) adds Ongoing after the existing All/Missed/Declined/Answered
filters. Native ongoing calls also appear first in All, with duplicate
history records suppressed. Stored call-log outcomes never make a call
joinable. `ui/ongoing_calls.py` polls the authenticated list endpoint only
while Calls is visible, at most once per ten seconds with one request in
flight; hide/show invalidates old replies. Unchanged rows are not rewritten,
row identity preserves selection, and periodic discovery does not take focus.
Failure clears joinable state and shows an unavailable placeholder on Ongoing.
Locked chats are filtered again on render and activation.

The native collection also holds multiparty calls filed under a person's
chat. Listing uses `getModelsArray()` after `checkOngoingCalls()`, and verifies
`WAWebMsgGetters.getIsGroupCall()` for a non-group chat. The same native
`joinOngoingWAWebVoipGroupCallPN` accepts that chat: its native implementation
explicitly branches on `chat.id.isGroup()`. Phone `@c.us`/`@s.whatsapp.net`
forms address the same native chat; LIDs remain distinct. Python stores a
listed `@c.us` chat as `@s.whatsapp.net` (`core/ongoing_calls.py`), so lock
checks and names match the stored chat, and preserves the chat peer even
when the multiparty call has no group JID.

A call filed under a person's chat also carries `people`: who is connected to
it, this account left out (`connectedPeople` in `groupCallRuntime.ts`,
`connected_people` in the bridge). It matters when this account is the
creator: a call it started on its phone, with no group, is filed under its own
chat. `_start_call` refused that chat as "a call to yourself"
(`voice_call_individual_only`), so the creator who left on the phone could not
join again from WinZapp (reported 2026-11-22). Now such a call
(`core/ongoing_calls.is_own_chat_call`) is offered in the chat of each person
in `people`. Its Calls row is named after them, like a one-to-one call that
became a group call (`MainWindow.ongoing_call_people_text`), and its "Open
conversation" goes to the first of them. A join (`join_call_id` with
`is_group`) is the only thing allowed to the account's own chat; placing a call
there is still refused.

Enter, the Join button and the context menu join the selected live call by
id. A call already active on this PC instead offers Return to call, which
focuses the existing call window without joining again. Other active calls
retain the existing busy protection. Video joins still start with the camera off.
An established local one-to-one call is included from the active call record,
even though WhatsApp's joinable collection contains multiparty calls. It offers
Return to call only; a pending outgoing attempt is never presented as connected.

## Connected participants

The active call window and a selected ongoing Calls row expose Call participants.
The authenticated `call/participants` endpoint is read-only and shares the
non-initializing discovery path. For multiparty calls it uses the native
ongoing call's `callParticipants` and the exact
`WAWebVoipWaCallEnums.CallParticipantState.Connected` value, matching the filter
in `WAWebCallLogJoinOngoingCell.react`. Invited, ringing and departed people
are excluded; group membership is never used as a fallback. An established
local one-to-one call can name its peer and the local user from the active
call model. Unknown group roster shapes fail instead of claiming completeness.
The call window's button is enabled only for a connected call (`ACTIVE` or
`REJOINING`) with WhatsApp's own call id whose chat is not locked. Attaching
Python's audio to a call answered or adopted on the page keeps that
connected state.

The native LID-to-phone bridge deduplicates addresses when available. Saved
contact names take precedence over native profile names; raw JIDs never reach
the list. The standard wx dialog refreshes every ten seconds only while open,
also offers Refresh, keeps selection by participant identity, and does not
rewrite unchanged rows or announce periodic updates. Closing invalidates
pending results. Failure clears stale names; an ended call clears the list
and stops polling. Locked-call permission is checked before opening and again
before presenting an asynchronous response. No audio device or join is invoked.

## Neonize engine

The bridge answers the same four routes (`neonize_bridge/ongoing_calls.py`).
It has no native call store: it remembers the group calls it saw (an offer that
rang, a call placed from WinZapp) and asks the server about each with the
lobby query WhatsApp Web sends for a call the account is not in
(`<lobby query="1"/>` to `<id>@call`, captured over CDP); the answer's roster
gives who is `connected`. Joining sends WhatsApp Web's `<accept>` (meowcaller
`api/ongoing-group-join`). A call started while the bridge was off, that never
rang here, is not listed. A one-to-one call becomes listed once someone is added
to it: `report_rosters` sees its roster turn into a group's and files it
(`CallMedia._remember_promoted`) as WhatsApp does. For a call that rang here,
that is the caller's chat, and the lobby query names the creator kept from the
offer. For a call placed here, it is the chat of the person called. Whoever
leaves such a call finds it again to join.
