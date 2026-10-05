# Message stars: WhatsApp state and legacy local stars

Previously `starred` was only a WinZapp flag. Single and bulk star actions now
use the existing WPPConnect `/star-message` endpoint. A star is private to the
account; this does not change the separate, participant-visible message-pin
feature.

## Write and verification contract

`main_window/message_stars.py` sends one POST with the full serialized message
key from `_serialize_msg_id`, including the cached LID and group participant.
It then makes one bounded GET to `/message-by-id/<serialized-id>`. Each request
has a 15-second timeout. No star POST is automatically retried, including after
a reset, read timeout or server error.

The setter's HTTP success is insufficient. In the pinned
[WA-JS 4.6.1 implementation](https://github.com/wppconnect-team/wa-js/blob/v4.6.1/src/chat/functions/starMessage.ts),
`StarMessageReturn` is built **before** `sendStarMsgs`/`sendUnstarMsgs`; its
`star` can describe the previous state. Verification instead reads the exact
message key and a strict boolean `star` matching the requested state. The
server's `deviceController.getMessageById` returns HTTP 201 with
`Success.response.data`; the direct `success.response` envelope is also
accepted. Error envelopes, another chat/fromMe/participant key, missing `star`
and the opposite state cannot confirm success.

Results are `confirmed` (observed on the linked device), `refused` (a definite
pre-controller/connection refusal) or `unknown`. Ambiguous writes are checked
by reading, not replayed. An unverified result may already have changed
WhatsApp; the UI says to refresh the **open chat with Shift+F5** before a manual
retry. This read is not a phone-history request or an AI-provider call. It is
not proof of an immediate update on the phone while that phone is offline.

## Local state, persistence and incoming changes

`core/message_stars.py` owns these JSON fields:

| Field | Meaning |
| --- | --- |
| `starred` | Displayed star: local legacy star or observed WhatsApp star |
| `_star_remote` | Last strict boolean read from WhatsApp |
| `_star_local` | An old local star still awaiting explicit migration |
| `_star_observed_at` | Observation ordering used to retain a newer action |

An absent/non-boolean remote field is unknown, never an instruction to unstar.
An old `starred=True` without remote metadata is treated as a local legacy
star. Remote False does not remove it. Explicitly confirmed star/unstar consumes
the local marker; later remote False can remove a server-managed star.

The normalizer retains `star`. Same-id redeliveries merge the flag independently
of edits. Remote star-only changes use an atomic flag update and repaint only
the corresponding open chat row. An unchanged observation advances ordering
without an extra repaint. There is **no dedicated immediate phone-star push
subscription** in this change: phone changes appear when that message is
fetched or redelivered. Shift+F5 fetches the open chat; an incremental sync can
skip a quiet chat and must not be advertised as an immediate star refresh.

Normal sync and older-history fetches merge stored flags before replacing UI
records, including older rows outside the resident page. Their observations
are stamped with the read's start time so a read already in flight cannot undo
a later confirmed action. Incoming live/history messages restore disk-only
legacy stars on a wx callback, without changing their text.

`core/star_storage.py` preserves flags under the database write lock, with a
bounded query per 400 IDs and phone-JID variants. Duplicate IDs inside one
batch retain the newer star observation too. Atomic star writes update only
an existing row's encrypted JSON: they keep an edited body and do not insert a
deleted message. No schema migration is needed.

## Old-star migration and batches

The conversation context menu offers **Sync old local stars with WhatsApp**.
It scans that chat's local database in pages of 500, including history outside
the visible page. System events and duplicate IDs are excluded. A standard
Yes/No dialog names the chat and the count; **No is the default**. There is no
automatic migration, account-wide sweep or automatic history request.

Only one star job runs per window, across its conversation panels. Operations
are sequential, off the UI thread. Only verified changes update the UI and
database; the final speech reports confirmed/refused/unverified/unprocessed
counts and the changed rows are repainted together. Normal bulk starring
skips already-starred messages; legacy migration is its separate explicit
operation.

While a job is active, the conversation menu offers **Stop star sync**. It
stops before the next message; an in-flight request may still finish and its
confirmed result is retained. Cancellation of a scan prevents its confirmation
dialog. Shutdown or locking the target chat aborts the job; closing the vault
invalidates the job permanently, even if unlocked again before a request ends.
An already-sent request cannot be recalled. Opening a different conversation
does not repaint the new conversation with the old one's result.

Legacy preservation applies to normal loads, refreshes and database rewrites.
The existing **F5 full resync deliberately wipes local chat/message data**;
unmigrated local-only stars have no remote copy to restore after that wipe.
This feature does not alter that explicit cache-reset contract. Prefer the
per-chat Shift+F5 refresh when checking an uncertain result.

## Validation boundary

Tests use pure functions, plain stubs, queued fake worker/wx callbacks, in-memory
or temporary encrypted SQLite, and mocked HTTP. No wx.App or real dialog is
created, and no installed profile, WhatsApp session, API key or AI provider is
used. Focused checks cover strict metadata, legacy migration consent/cancel,
partial outcomes, busy jobs, chat switches, locks, stale reads, encrypted
persistence, edited bodies and message identity. All seven registered locales
contain the new strings with matching placeholders and changelog entries.

Full-suite/native wx checks belong in GitHub CI after publication approval.
Live cross-device propagation and NVDA interaction still require an explicitly
authorized manual test; headless tests do not establish those outcomes.
