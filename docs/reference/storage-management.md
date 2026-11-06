# User-controlled storage management

Settings > Storage > Storage manager accounts for known WinZapp locations and
offers explicit local-copy operations. It never calls a message deletion API,
logs out an account, removes a database/key or searches a user's whole disk.
Old or large is a filter, not evidence that a file is unnecessary.

## Ownership and measurement

`core/storage_manager/roots.py` obtains paths from `app_paths`, the Neonize
bridge owner and the transcription model catalogs. More-specific roots own
their subtree; `(device, inode)` deduplication counts a hard link once.
Counts are **logical lengths**, not allocated space or freed disk bytes.
Program, shared, current-account, external-model and other-account scopes
remain distinguishable. Other accounts are informational and opt-in; saving
that option captures the current folder set. New folders require re-enabling
the option. External models contribute catalog/registered files, never an
arbitrary directory sweep, and are not removed by this manager.

The default scan reads metadata, without opening file contents. Reparse,
offline/recall and unsupported entries are not followed. Required missing
roots, denied access, changing entries, unavailable owners, cancellation and
limits make coverage partial. An unavailable optional model owner does not
discard measurements of the remaining roots. Default bounds are 100,000 files
and 300,000 visited/queued nodes. Results are timestamped observations, not a
filesystem snapshot or a promise of future availability. OS metadata calls
can delay cooperative cancellation; no network timeout is promised here.

Optional media matching reads selected **local** message histories in pages
of 256, up to 100,000 records per evaluation. Counts must agree before and
after; missing/decryption-filtered rows or backend errors invalidate matching.
Select fewer visible unlocked chats when that bound is reached. Only compact
reference metadata and allowed display names remain in the scan result.
Sent, pending, failed, starred, locked/hidden and unmatched media stay
protected. Unknown/colliding references never become orphan-cleanup consent.

The older media-cleanup dialog uses the same shared/account lock order and
rereads file/chat/category protections. Its download suppression is persisted
before deletion too, so it cannot race the new manager's index rollback.

## Permissions, plans and recovery

Priority is explicit protection, current use, ownership, then category
eligibility. Keep-lists support files, hashed chat identifiers and categories;
shared protection applies across this installation's accounts. Pending sends,
recording, playback and transcription/AI job owners are checked separately
from native file sharing. Removing protection never selects a file.

A preview contains at most 500 eligible fixed entries, a snapshot identity,
method and one-use token. Larger selections are refused, not truncated.
Previews expire after one day. Apply rereads preferences, roots and references
under shared-then-account locks; it never rescans to add candidates. Native
Windows handles pin ancestry, final path, attributes and file identity.
Changes, active writers, reparse points or unsupported providers fail closed.
There is no path-delete, cross-volume copy or permanent-delete fallback.

The two explicit methods are same-volume recoverable holding and permanent
local removal. Holding still occupies disk space. A private atomic journal
and durable cleaned-media block precede mutation. Pending identity-matching
holdings can be reviewed after a crash. Restore never overwrites a current
file and reverses only the block originally introduced by that holding.
Purge keeps automatic downloading blocked. Broken/incompatible records are
retained. Holding pages expose continuation and partial coverage. Completed
journals shed private item paths; explicit maintenance can retire empty tokens
older than two days, after every associated preview has already expired.

`media_cleaned.json` retains its existing `{ids: [...]}` format and can read
the legacy list. Durable writes reject malformed or unknown formats. A
message's explicit Download action can confirm one reacquisition request,
without removing any automatic block. Ordinary Open/Play/Save behavior and
F5 do not bypass the index. WhatsApp may no longer have the media.

## Preferences and background work

Scheduling, media matching, broader scope, history and rules default off.
Manual scans never invoke automatic removal. Rules support type, age, size,
all/any conditions, suggestion, notification and a disabled draft state.
Non-destructive matches win over automatic matches. Only verified Node
compile-cache layout under the owned default cache can be automatic.
Scheduled evaluations require a newly completed, uncancelled scan; batches
are bounded, and permissions/rules are checked again before each operation.
Startup/interval triggers share one job slot; sleep produces no catch-up queue.
Notification repetition and one-hour/day scheduling pauses are user choices.

Targets are advisory logical-size thresholds for all/current/shared scope
and a category. They can notify or pause **new automatic** media downloads;
manual requests, sending and session operation remain available. The gate
uses cached aggregates, never a file-list traversal per message. It keeps
the last non-cancelled measurement until a new measurement or scope change.
No allocated-space display, free-disk threshold or hard quota is implemented.

Preferences are account-local; strict unreadable protection/schema checks
block file actions. Reset is explicit and does not remove held files or
media blocks. Templates omit paths/chat identifiers and disable imported
rules, scope expansion and scheduling. Optional history retains at most 100
count/size/time summaries for the chosen period; it contains no message
content, contact names or file paths. Required recovery journals are separate.

## Native interface and evidence

The dialog uses standard wx controls, checkable ListCtrl rows, six tabs,
200-row file pages, wrapping explanations and no focus grabs on updates.
Keyboard cursor and check state are distinct. Speech uses `speak_output`;
progress is opt-in and throttled. Lists use Freeze/Thaw. All eight catalogs,
unreleased changelogs and generated user guides describe the workflow.

Pure tests cover scanning, roots, policy, forms, references, jobs, services,
scheduling, transactions, recovery, aggregates, adapters and one-copy retries.
Synthetic transactions mutate only test fixtures through injected providers.
`test_storage_fileops_windows.py` exercises real pinned-handle operations only
in Windows CI. `test_storage_manager_gui.py` is behind the CI-only `wxgui`
gate. Local headless results are not live NVDA/JAWS, account/device, storage
hardware, large-DPI or native-speaker acceptance. Real disk unplugging, network
latency, power loss and older-binary downgrade acceptance remain unverified.

The design scenario inventory covers S01–S144. Its review groups are:

| Scenarios | Decision / automated evidence family |
| --- | --- |
| S01–S08, S97–S104 | Known roots, logical size, deduplication, partial coverage; scan/roots/evaluation tests. Allocated/free space is explicitly unavailable. |
| S09–S16, S65–S72 | Conservative references and keep-lists; policy/references/forms/adapter tests. Model, backup and log content stays owner-managed. |
| S17–S24, S105–S112 | Fixed plans, identity validation, single ownership and cancellation; transactions/jobs/service tests plus CI-native provider tests. |
| S25–S32 | Busy owners, shared/account locks, durable blocks and crash holdings; adapter/transactions/recovery tests. Real crash/power-loss acceptance is separate. |
| S33–S40, S89–S96 | No ownership expansion; roots/native path checks. Media relocation S92–S95 is a separate feature, not simulated by changing a folder preference. |
| S41–S48, S129–S136 | Native controls, page/selection semantics, locale validation and CI-only construction. Live assistive-technology/DPI acceptance is separate. |
| S49–S64, S73–S80 | Explicit defaults, frozen other-account scope, scoped targets, rule precedence and opt-in scheduling; forms/service/runtime/evaluation tests. |
| S81–S88, S113–S120 | One-copy reacquisition, no overwrite, malformed/future-schema refusal, restore/purge; reacquire/recovery/store/transactions tests. No new data migration is required. |
| S121–S128 | Safe labels, protections, path-free optional history and portable templates; adapter/forms/store tests. A shareable storage report is not provided by template export. |
| S137–S144 | Bounded queues/pages/plans and honest evidence limits; scan/jobs/evaluation tests. No claim exhausts every filesystem or event interleaving. |

Scenario counts describe design coverage; they are not a test count or a
claim that every scenario has been executed. Relocation, cross-installation
administration, Recycle Bin integration, free-space policies and shareable
storage reports require separate contracts. No option pretends they exist.
