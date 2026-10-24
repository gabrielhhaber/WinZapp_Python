# Contact presence: preserve a missed read and an explicit visibility denial

The active-chat HTTP read is single-flight, with a ten-second interval per
canonical contact. An offline event can arrive while the previous read is
running or before the next attempt is allowed. Returning from that call without
remembering it left all last-seen views empty until the normal 30-second tick.
Discarding an older HTTP reply after a newer event is correct; removing that
revision guard is not a fix.

core/contact_presence_refresh.py owns one pending scope and at most one
single-shot timer. The scope includes panel identity, visit, connection epoch,
aliases and reciprocal query identities. Repeated requests coalesce without
moving the due time. The demand is consumed before a worker starts. Ordinary
HTTP results cannot renew it. A confirmed initial subscription has one explicit
pending-only readiness continuation, described below; null/error results do
not get that continuation. External events or ordinary ticks can request work.

Stop the pending timer on visit close/change and connection invalidation.
A timer already queued before Stop must still check its generation and scope.
At delivery recheck active/visible/connected/shutdown conditions. Never turn
this into continuous polling or make Alt+T wait for HTTP. A blocked or denied
contact is not a reason to search another identity.

The cache's _seen_version identifies the update that actually observed lastSeen.
Missing-field events retain that version instead of making an inherited
timestamp current. Comparing only receipt clock values loses this distinction
when separate updates share one clock tick. A later usable observation, online
state, activity or explicit withholding can satisfy/cancel pending work.

For the WPP onpresencechanged shape, WA-JS 4.7.1/4.7.2 defines isContact as
!chatstate.deny, not address-book membership. Preserve its boolean meaning as
restricted for private contacts. Any explicit denial wins conflicting fields.
Missing or malformed permission fields must not revoke an earlier denial.
Malformed modern ready or pending permission data is unknown/terminal, not
permission to query a paired alias. An authoritative positive legacy timestamp
can restore information; an empty legacy response cannot clear a known denial.
Activity remains independent of online visibility, and groups keep their own
(chat, participant) cache scope.

The measured source/wire paths, published package factories and offline
counterexamples are recorded in the 24 October last-seen investigation.
Tests exercise actual methods on plain stubs with fake clocks, timers, workers
and HTTP. No wx.App, native hooks, provider/account probes or background work.
Source/headless success does not establish real WhatsApp or screen-reader
acceptance.

## Persistent pending is separate from subscription success

The typed read in core/contact_presence_query.py distinguishes ready, withheld
(a valid ready/legacy response with no timestamp, not proof of its cause),
restricted, pending, error, malformed and unsupported. Activity is usable data.
The snapshot-only profile interface still returns a validated dict or None.
An explicit pending/restricted:true result carries a clear-only snapshot:
restricted:true and lastSeen:null, with no asserted online/state value. The
coordinator and profile must apply that denial through their existing acceptance
guards. Dropping its snapshot leaves earlier visible data in cache and permits
the profile's old fresh timestamp fallback. A denial is never alias/recovery
permission; ordinary unready/error/malformed outcomes still carry no snapshot.
Only explicit successful modern pending permits the reciprocal identity; the
second result replaces the first outcome. A terminal/error second result must
not leave the first pending behind as a recovery reason. Contradictory pending
fields and malformed permission/online/state fields are terminal.

core/contact_presence_recovery.py observes ordinary active-chat reads. Initial
policy bounds are 90 seconds of matching pending observations, no observation
gap over 45 seconds, at least 120 seconds between renewals, and two recovery
attempts per contact per connection epoch. These are conservative offline-tested
defaults, not measured provider latency. The observation window is independent
of the cache's coincidentally 90-second receipt freshness.

A new cache revision, any nonpending outcome, a long gap or a backwards clock
restarts the observation streak. A successful POST, a failed POST, worker-start
failure, closing/reopening a chat or a mapping change does not refund attempts.
Late reciprocal mapping combines used budgets; it cannot create new attempts.
Only connection invalidation replaces the budget. No extra retry timer or
POST-completion polling was added: the existing read cadence drives recovery.

The coordinator records outcomes only within the existing visit/epoch/identity/
cache revision guards. Recovery also rechecks visible, active, connected private
chat conditions and respects even an expired cached explicit denial. Alt+T
remains cache-only. Ready-null, explicit denial, unsupported, malformed and
errors never authorize recovery.

IdentityMixin.subscribe_presence owns the claim immediately before dispatch.
Keep the separate in-flight target set: a tentative marker is not a confirmed
subscription, and neither marker is proof of received data. Normal navigation
and failed-marker retry cannot bypass a used recovery budget. Repeated recovery
calls must acquire a fresh claim. A slow POST cannot duplicate its in-flight
target after the old ten-second throttle expires. Capture routing/auth, cache
object, in-flight registry and epoch; an old worker must never remove replacement
state or send its remaining alias to another account. Log outcome only, without
contact identity, response bodies or raw exception text.

At the earlier pending-recovery stage, the runtime classified model-read errors
as pending. The next section supersedes that behavior. These bounds still do
not establish a live cause or promise immediate last-seen updates or recovery
from a permanently stuck worker.

## Native field bridge and the initial subscription race

contactPresenceBridge.ts watches model addition, type, timestamp, deny, online, hasData and
chatstate-replacement events directly on PresenceStore. Nested events pass the
Chatstate child; find its current parent, then recheck current store/model
identity before reading. Only private numeric PN/LID identities are eligible.
No additional contact subscriptions, native polling or initial cache dump is
performed. A microtask coalesces a field burst; unchanged snapshots are deduped
with a bounded 512-entry fingerprint map. Remove/reset, generation replacement,
store replacement and disposal invalidate queued work. Partial attachment must
roll back, leaving legacy fallback eligible rather than claiming installation.

The Node side exposes one callback and load/close hook per page. Successor
clients and reloads get new generations. Reject old envelopes and old async
installation completions; an older in-page installer cannot replace a newer
one. Failed readiness/installation has at most 21 attempts spaced 500 ms apart;
close or replacement cancels retries. A hung legacy Promise.all or rejected
subscription registration cannot block the field bridge or later listeners.
The real registration block is tested with both faults.

Once installed, legacy private events do not also enter the local socket;
legacy group delivery stays separate. The old webhook path and payload remain
unchanged. Native snapshots go only to the existing local Socket.IO channel.
Adding their last-seen data to external webhooks was rejected by automatic
approval review and is not part of this implementation.

The shared snapshot reader requires ready, typed data; native read exceptions
are error, malformed ready models are malformed. An unready PresenceStore entry
can use ready ChatStore.presence for the same identity, but an explicit denial
in the primary prevents that fallback. No alternate identity is introduced.
Python keeps native error terminal. Ready native snapshots apply without a new
HTTP request. A hasData loss invalidates cached activity/online/last-seen as
unknown, never inventing offline; a known denial remains authoritative.
Presence logs contain shapes/counts, not identities, timestamps, names or spoken
text. Event emission t remains ordering time, never last-seen data.

If an initial subscription POST is in flight and data is needed, keep one
deferred demand and start GET only after successful completion. Capture epoch,
cache and routing for the completion callback. Current visit, identity mapping,
visibility, connection and revision guards still apply. Failed POST has no
completion loop; the ordinary later tick may retry it. Confirmed initial POST
permits one additional read only if its first result is explicit pending.
That read respects the existing ten-second GET interval and all cancellation
checks. Null/deny/error/malformed/unsupported never get it. Alias discovery,
reopening and recovery POSTs cannot mint another allowance. Only connection
invalidation replaces the allowance. A native ready event may satisfy/cancel
the waiting demand before POST completes.

Everything is exercised with real source and fake models, clocks, workers and
UI queues. No real browser, provider or account acceptance is implied. A stuck
POST/GET, a provider with missing event contracts, undisclosed privacy settings,
or WhatsApp's own delayed delivery can still prevent immediate information.

## Bounded active-chat workers

core/contact_presence_work.py separates a logical UI wait from a worker that
is physically still alive. Initial policy: a 30-second monotonic deadline and
at most two jobs per kind (active-chat GET and private subscription POST),
counting both physically running workers and completed jobs awaiting UI delivery.
The deadline is an offline-tested policy, not measured provider latency or a
promise to interrupt Requests. No process/thread is killed.

Expired workers retain physical capacity until they actually finish. Connection
invalidation stops logical waits and timers but must retain those physical
slots. Reopening, changing contacts or reconnecting cannot create fresh pools.
Completed-but-undelivered jobs also stay bounded and are invalidated across
epochs. A healthy UI completion retires its own watchdog. Lost/late completion
is cleaned by the deadline. Scheduler/Thread construction/start failure releases
only the unstarted reservation. Timers and slot accounting are separate.
Stopped/queued watchdog callbacks check their generation: a healthy completed
ready-null read must not be renewed by a callback queued before Stop.

One stalled GET can be replaced by the second slot. An old result cannot clear
a newer single-flight flag or overwrite a successor snapshot, even for the same
contact/revision. After a context change, an old completion may wake a needed,
currently eligible read; it never applies the old result. The previous reconnect
refresh behavior is preserved. Deadline is an error outcome, never pending.

A stalled private POST loses only its logical marker. Do not overlap POST keys
while the old physical worker remains: the provider may already have received
it. GET is allowed to read available data instead of waiting forever for that
POST. Subsequent alias POSTs and success/failure cache commits check the lease
as well as captured routing/epoch/cache ownership. Expiry/late response cannot
confirm a subscription, refund recovery budget or create a readiness allowance.
Accepted earlier aliases remain confirmed if a later alias stalls. Cleanup
removes only markers owned by that job. Existing normal failure retry and the
separate 90/120-second/two-attempt pending recovery budget remain intact.

All UI retry paths retain visit/mapping/revision, visibility, connection,
shutdown and denial checks. Native push/rendering remain independent. At full
capacity, keep one demand without an extra polling timer; retry only when a
slot actually returns, an ordinary tick or another authorized event arrives.
If both slots never return, HTTP waits for capacity. This is an explicit resource
limit, not total recovery from arbitrary uninterruptible I/O. Group subscriptions,
profile-dialog workers, modal/focus/expiry policies and own online policy are
outside this active-chat worker change.

## Focus return, personal reader and native activity expiry

The subsequent foreground-recovery change addresses the preceding modal/focus/
expiry exclusion. The existing debounced main-window activation requests only
missing information. A personal dialog can keep the same visible private chat
eligible while the main frame is inactive: its weak reader must be shown, active,
open and bound to the same panel/visit. The visit is captured at dialog construction,
never acquired by late population or tab/profile rerendering. An older dialog
cannot adopt a reopened visit. Exact normalized identity or a reciprocal known
PN/LID pair is required; a one-way map cannot authorize this new reader.
Different contacts, group dialogs, stale visits and other applications do not
make a hidden/inactive main conversation readable. Existing 30-second ticks
continue; opening/reactivating the reader supplies one demand without a new timer.
Stop/repopulate unbinds the old handler; already queued callbacks recheck ownership.

Native composing/recording expiry may supply one missing-data demand for the
current private chat. Online, usable data and even an expired explicit denial
prevent the new demand; the coordinator also honors stale denial before dispatch.
Group expiry and other contacts cannot authorize it. Most critically, an HTTP
snapshot delivered with refresh_offline=False cannot authorize a further expiry
read. Otherwise an unchanged composing HTTP answer creates a 10-second polling
loop. Two deterministic before-fix cases establish that trap.

All triggers reuse ten-second coalescing, worker caps/deadlines, revision/epoch/
mapping/routing guards and existing pending/readiness budgets. They never invent
offline or last-seen values. Rendering stays ChangeValue-based with native caret/
selection preservation. Main activation still controls own online status and
typing speech; treating a dialog as a data reader must not change those policies.
These source/headless results do not establish live WhatsApp or NVDA acceptance.

## Independent profile presence is bounded separately

main_window/profile_presence.py now wraps the synchronous get_last_seen path.
It creates no thread/timer and admits at most two profile presence GET jobs,
counting both running I/O and finished jobs awaiting UI delivery. Identity keys
are exclusive, so a concurrent same-contact/reciprocal-alias read cannot overlap.
Its lazily created per-window pool is initialized under a lock; reconnects never
replace it or release running slots. Expired finished waits can be reaped by a
later caller; expired physical I/O still occupies capacity until it returns.

The same initial 30-second monotonic acceptance window applies to return values
and UI delivery. Routing (server, port, token), epoch, open visit, mapping/query
identity, revision, shutdown and connected state are rechecked. Stale explicit
denial blocks dispatch. Error is unknown, not offline; an empty verified snapshot
can still clear an older timestamp. Dispatch/fetch failure retires reservations.
There is no new retry/polling timer or pending-recovery permission here.

get_contact_profile must not wait for presence I/O before returning its profile
fields. Its lastSeenTs field reads current cache only. The personal dialog posts
Overview before invoking the background presence continuation. A matching visible
current chat uses its existing coordinator/modal reader; a standalone contact
dialog retains a bounded profile read after posting its fields. Constructor-time
context and closed/visit guards prevent a late dialog continuation from starting
against a replacement account/visit. Touched profile logs report status/outcome
only, without raw profile payloads, identities or exception content.

This is a separate pool: at most two active-chat plus two profile presence GETs
may physically coexist. It does not cap all contact/About/common-group API calls
or all threads of the profile feature. Blocking I/O cannot be killed, and two
permanently stuck profile GETs exhaust its capacity. Other profile HTTP delays,
group/legacy webhook ownership and real WhatsApp/NVDA acceptance remain separate.

## Group activity no longer uses the provider's first-client binding locally

The subsequent group-ownership phase supersedes that local group exclusion.
Published WPPConnect 2.3.5 and 2.3.6 have byte-identical listener.layer.js files:
afterPageScriptInjected only exposes a name when it is missing, and the exposed
closure emits on its first client's listenerEmitter. Reusing a page for another
client does not replace that closure. Actual unmodified exposure/registration
methods reproduced group events going to the old client in a fake page.

groupPresenceBridge.ts supplies one separate local binding and load/close hook
per page, with a current client/request owner and increasing generation. The
page bridge watches PresenceStore add/typing/recording fields, checks the current
store and exact model at flush, coalesces a burst and dedupes unchanged activity.
Remove/reset/malformed native data clears pending/last state and local legacy
suppression without inventing an activity/last-seen event. Reinstall disposes
the old store's listeners. Pending/last/native-owned group maps are bounded to
512; envelopes accept at most 4096 participants and copy only the known shape.
No contact subscriptions, polling, initial activity dump or private snapshot.

The previous group readiness allowance is retained: initial try plus at most
120 retries, spaced 500 ms, one owned chain. Close/replacement/load invalidates
old attempts; a late in-page installer cannot replace a newer generation. A
provider's missing/hung exposed callback does not block local group delivery.
Partial attachment rolls back, leaving legacy fallback available.

Only successful native delivery claims suppression for that group. A thrown
socket emit releases it. An already queued legacy event may still arrive at
handover; the real Python pipeline was tested to keep one spoken transition
and (chat, participant) scope, never a private last-seen observation.

Legacy callback dispatch also captures the page and checks current client AND
request ownership. Checking only client.page at delivery is insufficient: that
property can itself move to another page. Both private/group native callbacks
and pending attempts reject that case; an old page cannot use the moved client
to deliver a private snapshot, group event or legacy webhook event.

The group page source preserves the old group activity legacy shape/source;
no private lastSeen/restricted snapshot is added to webhooks. Stale callbacks
fail closed. If the provider binding remains attached to an old client, external
webhook delivery can remain unavailable while the new local group route works.
Do not redirect private native data or claim external recipient acceptance.
Untagged legacy fallback still depends on the provider's event contract; native
generation checks do not prove the provenance of arbitrary untagged old events.

Portable test fixtures retain the original LGPL header, exact method fragments
and SHA256 provenance for both published providers. The local run first used
the verified unpacked packages; portable fixtures match those exact fragments.
No provider/browser/account process or network is started by these VM tests.
