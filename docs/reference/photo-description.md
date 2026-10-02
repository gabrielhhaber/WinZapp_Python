# On-demand photo description

Implementation on `feature/photo-description`, not released. Automated tests
use synthetic data and no live provider keys/network. Separate, explicitly
authorized manual Gemini checks used only the generated demo photo and the
user's saved demo key through the application. The first timed out at about
15 seconds; one newly authorized request after the budget correction succeeded
in about 23.7 seconds. This is one synthetic-photo result, not broad quality or
performance validation. The key was never inspected by the agent.

## Using the feature

1. In Settings > Photo description, enable the feature, choose OpenAI or Google
   Gemini and enter **your own API key**. This choice is shared across accounts;
   the WhatsApp connection key is unrelated. Leave the field blank to retain a
   saved key; deletion is explicit and takes effect on Apply/OK. Cancel discards
   staged key changes. Show/hide exposes a separate, Tab-reachable read-only
   field. Get an API key opens the provider's official website.
   Get models explicitly retrieves metadata with the entered draft key or the
   saved key, without saving a draft key, sending a photo or generating an answer.
   Choose a model in the native Photo description model dropdown. The existing
   model remains unchanged until an explicit selection; Apply/OK saves it.
   Advanced manual model entry remains available, including on discovery failure.
2. Test connection checks authentication/model access without uploading a photo
   or generating an answer. It does **not** verify vision quality, available
   quota, billing or that a paid Gemini project is configured.
3. In a chat's message list, focus an ordinary photo and choose Describe photo
   or press Ctrl+Shift+Y. No automatic requests happen for incoming messages.
   View-once media, videos, stickers, documents and statuses are not supported.
4. Read/select the description with Tab. Enter inserts a newline in the question
   field; Ctrl+Enter asks about the same photo. Answers never enter WhatsApp's
   message composer. Copy copies only the latest answer. Describe again is an
   explicit new, potentially billed request.

Each request reads the current application language and includes it in the
provider's system instruction; the initial description prompt is localized too.
Romanian requests use `ro`, not a hardcoded Turkish demo locale. Questions in
another language do not remove the reply-language instruction. This is a request
contract, not a guarantee that a provider will always follow it perfectly.

### Model catalogue boundary

Discovery uses the official [OpenAI models endpoint](https://developers.openai.com/api/reference/resources/models)
or [Gemini models endpoint](https://ai.google.dev/api/models). It is opt-in,
uses the existing two-slot worker pool, has a 20-second operation deadline and
GUI watchdog, and never follows redirects or retries. Gemini pagination is
bounded to five pages, 256 KiB per page and 1 MiB total. Credentials are sent in
headers, never URLs. No key, response body or model catalogue is persisted/logged.
Key/provider changes, credential removal/reset/apply and page destruction cancel
discovery; generations reject stale completions. Model discovery and connection
checks invalidate each other's status callbacks. Completion announces a localized
status through the existing speech gate without moving keyboard focus.

The endpoints do not provide a complete image-input/endpoint compatibility flag.
`model_catalog.COMPATIBLE_MODELS` therefore intersects returned exact IDs with a
small reviewed catalogue. Gemini must also advertise `generateContent`; OpenAI
entries announcing shutdown are omitted. Names shown in the dropdown come from
the reviewed catalogue, not arbitrary provider descriptions. Empty results and
malformed/partial/pagination-limit results are distinguished; the latter are
errors, not a misleading partial success. The list does not establish billing,
quota, image quality or successful generation with every listed model.

Initial catalogue review (2026-10-03):

- [GPT-4.1 Mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini),
  [GPT-4.1](https://developers.openai.com/api/docs/models/gpt-4.1) and
  [GPT-4o Mini](https://developers.openai.com/api/docs/models/gpt-4o-mini),
  including only the snapshots explicitly listed on those official pages.
- [Gemini 3.8 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash)
  and [Gemini 3.5 Flash-Lite](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite).

This is intentionally not the entire provider catalogue. Adding a model requires
official image-input/text-output and adapter-compatibility review plus tests,
not a name-prefix guess. Unknown IDs can still be entered in the advanced field;
they are not presented as compatibility-reviewed choices. No defaults or saved
models were migrated for this addition.

Verification: the initial 36 cases reported setup errors because the new
catalogue/UI modules did not yet exist. After implementation the first expanded
selection run passed 1,421 cases, with two older connection-test stubs missing
the new cancellation method; those stubs were updated. The final **25 explicitly
selected files passed 1,839 tests / seven native GUI cases deselected** in 22.13
seconds. This includes bounded metadata/error/pagination contracts, actual settings
action cancellation on plain stubs, stale/new request isolation, draft-key
non-persistence, manual/explicit selection, timeout/disposal and 28 combinations
of locale/provider/description-or-question payloads. The Romanian cases verify
localized initial prompts and `ro` instructions in both adapters, without a live
language-quality claim. Locale/changelog, sound/speech, build/dependency and
no-desktop-window checks also passed. No sockets, real credentials, audio devices
or windows were used by these tests. At that point live discovery and the new
dropdown's NVDA behavior still needed a demo restart. The subsequent manual
result is recorded below. Full CI and a packaged-app pass remain unverified.
No commit, push, PR or workflow dispatch occurred.

On 2026-10-03 the separate demo was normally closed without saving the old
settings dialog, then restarted with the current code. The installed WinZapp
was left untouched. The new settings text, Get models button and labelled native
model control were observed; the initial saved model was still gemini-3.8-flash.
During the user-operated check the status became Model list ready, the dropdown
was enabled, and Gemini 3.5 Flash-Lite was selected with gemini-3.5-flash-lite
also shown in the advanced field. The agent did not press Get models, choose a
model or save settings. Asked whether they had fetched the list and whether
NVDA announced the result and Tab/Shift+Tab/arrows worked, the user confirmed
"yes, without problems". This is user-reported listening/navigation evidence
for that Gemini demo check, not audio heard by the agent. No inference is made
about how many requests the user made or whether they saved the selection.
Technical information, live OpenAI discovery, full application settings and
the packaged executable were not verified by this check.

The result starts with the description, not the application's internal prompt.
Follow-up questions and answers have localized headings. A successfully answered
question is cleared only if its editor is still unchanged; a new draft typed
while waiting, failed/cancelled questions and focus are preserved. Regenerating
a description does not clear the question draft. Status also has a visible label.

### Waiting sound and automatic reading

During a consented photo operation a request-owned stream loops the active
soundpack's Photo description in progress event. The default uses the bundled
five-second dijital-imza.wav cue; it is a separate stream, not the live
synchronization or call sound. The synchronization event retains synchronizing.ogg.
Settings > Sound Events can disable this event or choose a custom
file. Existing pack overrides, effect output routing and background-mode silence
apply. The manual demo initializes the same sound backend, separately from the
installed WinZapp process; no WhatsApp/account runtime is involved.

The selected cue is a locally synthesized original motif (mono, 44.1 kHz,
16-bit PCM), copied unchanged from the manual listening samples. No external
recording or sample was used. Its SHA-256 is
`0b4541ef1d2a17a526d21f1d658aa19c53f6761cd334d74776909149230f2691`.

Successful completion synchronously stops and frees the waiting stream before
updating the result or sending any speech through MainWindow.output. Errors,
timeouts, cancellation and disposal also release it. No delayed stop, new speech
engine or request retry is added. Private waiting streams decline the generic
Sound device-recovery path, which otherwise creates an unowned fallback stream;
audio failure leaves the readable result usable. Other Sound callers retain
their existing recovery behavior.

Read new answers automatically now defaults on when no preference is saved.
An explicit saved false remains false: choose this option in Settings > Photo
description to enable it for an existing demo/configuration. Enabled reads only
the latest answer once, not the internal prompt or whole transcript; disabled
announces Ready. Only the automatically spoken copy joins line breaks and other
layout whitespace into single spaces. Visible paragraphs, the copied answer and
provider history stay unchanged. Punctuation and the user's speech-engine settings
still determine natural pauses; this does not force uninterrupted speech.
The normal screen-reader/SAPI/master speech gate is unchanged.
A later authorized manual demo returned a description in about 19.36 seconds
and the follow-up “What color is the circle?” in about 3.13 seconds. The user
confirmed the first waiting cue stopped and the description was automatically
read without overlap. Enter/newline, Ctrl+Enter/send, cleared input and backward
Tab access to history were observed separately. The single-line speech refinement
was added afterwards: it still needs a new manual listening check. Fake audio
ordering tests are not a claim that the agent heard the output.

On 2026-10-03 the user-requested demo was restarted with dijital-imza.wav and
the single-line speech change. One generated-photo request to Gemini returned
HTTP 200 in about 5.54 seconds; the visible result matched the text and shapes,
the status became Ready, and actions were enabled again. Asked about the new
cue, stop-before-reading behavior, continuous speech, level and overlap, the
user confirmed "yes, without problems". This is the user's listening evidence
for this one demo run, not audio heard by the agent. No follow-up request was sent.

The Turkish photo dialog and consent screen were also inspected live; settings
wording was reviewed in source, not in the complete application's SettingsDialog.
Four explicitly selected window-free files passed 122 tests in 11.30 seconds
(locale/key consistency, processing feedback and UI logic). This proves no
missing translations/placeholders were found, not that every translation is
natural or every native control works with every screen reader. Wording review
identified dense settings guidance, the ambiguous "ordinary photo" consent
label, "captions" translated as "descriptions", and generic Processing status
as possible refinements. No UI text was changed during this review.

### Copy and readiness refinement (2026-10-03)

The subsequent user-authorized refinement updates all seven locales. Settings
guidance now has four paragraphs: API use/cost, submitted content, local key/cache
handling, and provider data terms. The Turkish text explicitly says photo
captions are not submitted. Remembering consent is labelled as applying to photos
outside locked chats; the existing provider-specific and locked-session consent
rules are unchanged.

Description generation, answering a question and checking a connection have
distinct waiting labels. This does not add progress estimates, extra speech,
retries or network operations. Settings guidance has its own visible Usage and
privacy information label rather than sharing the provider privacy-link name;
the status field also has a visible label. Both remain native, read-only controls.

Nineteen focused regressions failed before these changes. After implementation,
the 23-file readiness run passed **1,738 tests / five native GUI cases deselected**
in 21.41 seconds. It includes photo preparation/provider contracts, bounded
networking, credential/settings isolation, cancellation/deadlines/vault close,
speech/audio ordering, locale/changelog consistency, dependency/build/macOS
contracts and the no-desktop-window guard. It was not the full suite. The GUI
cases now also check the settings help/status labels, but were not run locally.

The separate demo was restarted without new API requests. Its live settings
screen shows the new help/status labels and paragraph breaks; the API key remains
hidden and settings were not saved. The user confirmed Tab/Shift+Tab and NVDA
access to both fields, but found the guidance unnecessarily alarming and key
removal wording confusing. The complete production SettingsDialog and packaged
executable are not yet verified by this run.

In response, setup/use, transmitted content, cost and essential provider terms
remain in the main four-paragraph guidance. Encryption/cache/retention details
are retained in an optional native Technical information dialog. The shorter
Remove saved key action now reports removal as pending until Apply/OK, explains
Cancel and explicitly distinguishes local removal from revoking a provider key.
The status field is multiline for readable feedback. Twenty-three failing
window-free cases preceded this refinement; the selected 23-file run then passed
**1,756 tests / six native GUI cases deselected** in 21.17 seconds. An attempted
manual-demo restart was stopped by the user's physical Escape key; no more
computer-use inputs followed, and the new wording/help dialog was not manually
verified. No provider request was sent.

The repository's existing Branch test build workflow can run the full suite and
produce Windows ZIP/installer artifacts without creating a release or PR. It
requires these local changes to be committed and pushed first; that approval has
not been given. Full CI, the native GUI cases, and a packaged-app manual pass
remain acceptance gates. No claim of release readiness is made yet.

Verification for the transcript/audio addition: six transcript regressions and
seven waiting-audio/default-reading regressions failed before implementation.
The final explicitly selected 21-file window-free run passed **1,615 tests /
five GUI cases deselected**, in 21.88 seconds. It includes fake stream cleanup,
stop/free-before-speech ordering, event/pack overrides, unchanged general audio
recovery, draft preservation, locale/changelog consistency, dependency/build
and macOS contracts. No live request, audio playback or desktop window ran in
these checks. Packaging contracts passed; a new executable was not built.

The default profile is Balanced. Quick reduces image resolution/detail and
asks for a shorter description; Detailed asks for more detail. These are not
claims of measured latency or quality. Default models were checked against
official documentation on 2026-10-02: `gpt-4.1-mini`, `gemini-3.8-flash`.
Advanced model identifiers are configurable; arbitrary endpoints are not.

## Privacy and lifecycle

- Consent names the selected provider and the potential charges. Ordinary-photo
  consent can be remembered per provider. Locked-chat photos require fresh
  consent every dialog session; general consent never bypasses this.
- Only the normalized image and this dialog's questions/answers go to the
  provider. No chat name/JID, caption, WhatsApp media URL or API token is added
  to an AI request. Each follow-up includes the image again. Model outputs are
  fallible; visible text is evidence, not executable instructions. No tools.
- Re-encoding applies EXIF orientation and drops metadata, including GPS. The
  original is unchanged. The usual encrypted WhatsApp media cache may acquire
  the original photo when it is downloaded; no additional AI image files,
  descriptions or question histories are persisted.
- API keys are in install-global `ai_credentials.enc` with a separate
  `ai_credentials.key`. Atomic writes use the existing cross-process settings
  lock. This is portable Fernet encryption, **not** protection against someone
  who obtains the entire data directory. Credentials are not in app.json,
  account settings, settings exports or logs. Reset all keys is separately
  confirmed and staged; it can recover an unreadable store.
- OpenAI requests use `store=false`; that is not a zero-retention promise.
  Gemini unpaid/paid services have different terms. Users must check billing
  and terms themselves; WinZapp cannot infer them from a key. Sensitive/private
  WhatsApp photos must not be submitted under unpaid-service data terms that
  prohibit them. ChatGPT subscription billing is separate from API billing.
- Results/history are RAM-only and account/session-specific. Close, chat
  change, photo deletion, account hiding/exiting or locking the corresponding
  vault cancels the session. Generation and account guards discard late
  callbacks without restoring text or speaking. Python memory is released,
  not cryptographically wiped. Cancellation does not reverse provider charges.
  Explicitly copied answers remain subject to Windows clipboard/history rules;
  locking the vault does not erase someone else's clipboard contents.

## Bounds and networking

Two jobs per process, no unbounded executor queue. One active request per photo
session, at most 12 attempts and eight successful question/answer turns; each
question is limited to 2,000 characters, each answer to 12,000. No streaming
speech, automatic retries, provider fallback or remote Files/Conversations API.

Source cap: 20 MiB and 24 million pixels, still JPEG/PNG/WebP only. Normalized
JPEG: at most 8 MiB, 2,048 pixels per edge in Quick, 3,072 otherwise. Declared
size is checked before downloading, and actual bytes during streaming through
the existing WPPConnect route (binary and older base64-JSON responses supported).
Oversized, malformed and animated payloads are refused. Successful downloads
remain encrypted in the regular cache.

AI response bodies are bounded to 256 KiB. Direct official HTTPS only,
certificate verification on, redirects off. Generation requests have an
eight-second connection cap (or the smaller remaining budget). The header wait
uses the remaining operation budget minus connection time, not a separate
15-second cutoff; image preparation also consumes this same budget. The
60-second operation deadline and GUI watchdog are unchanged. Metadata-only
connection probes still use 8/15-second connect/read limits. Available-byte
reads avoid waiting for full chunks from a trickling peer. Cancellation tries
the public urllib3 socket shutdown API on a separate thread; resource cleanup
stays with the reader. DNS, OS and provider processing cannot be forcibly
rolled back: late results are always dropped, and a received request may still
cost money. Error text uses safe categories, never provider response bodies,
raw requests or user content.

## Verification boundary

### Account-free manual demo

`client/photo_demo.py` is an explicit, user-operated development application,
not a pytest/GUI probe. Launch it with the repo's `.venv/Scripts/pythonw.exe`.
It never imports `MainWindow`, boots WPPConnect, pairs an account or downloads
WhatsApp media. Its fictional message list reuses `ImageDescriptionMixin`, the
real encrypted-cache loader, `ImageDescriptionSettingsPage` and
`ImageDescriptionDialog`. The small settings host is not the full SettingsDialog.
It generates an 800x500 PNG with WINZAPP DEMO 123, a blue rectangle, a red circle
and a yellow triangle. The image is placed only in the demo's encrypted cache.

Data and provider keys live separately, outside the checkout, in
`../_MANUEL_TEST/fotograf-betimleme/data/`; never copy these into Git. No installed
account data is copied. There are no canned AI answers or implicit requests:
enable/configure the feature and explicitly consent before a real provider call.
The optional locked-chat simulation closes the real photo dialog after 15
seconds, but does not test actual PIN authentication or the real vault scheduler.
The demo does not validate WhatsApp networking or the entire application layout.

Manual-demo diagnostics are opt-in through that entry point and go to
`../_MANUEL_TEST/fotograf-betimleme/photo-diagnostics.log`. Only allowlisted
operational stages, HTTP status codes, request generations and safe error
categories/exception classes are accepted. Timeout diagnostics distinguish
ConnectTimeout and ReadTimeout without exposing exception messages.
No API keys, headers, URLs, response bodies, image
bytes, questions, answers or chat identifiers are logged. No automatic request
or retry is used for diagnosis. Error/timeout messages now go through the same
screen-reader speech gate as ready notifications, without moving focus; the
GUI watchdog is armed before worker dispatch. The demo's context menu uses
explicit `Destroy()` rather than unsupported wx.Menu context-manager syntax.

Regression verification for these corrections: three failures reproduced
before fixes; five explicitly selected, window-free files then passed **1,321
tests**. A user-reported Gemini request remaining at Processing prompted
a separate, user-authorized manual run with diagnostics; mocked tests alone
were not treated as proof of success. That one-request check ended at about 15.37
seconds before any response headers arrived. The updated UI showed timeout and
re-enabled actions, rather than remaining at Processing. A keyless/photo-free
public-endpoint check returned HTTP 404 in 0.41 seconds: generic HTTPS access
worked then, but this does not prove why the generation request was delayed.

### Nested consent close and operation-budget correction

The manual locked-chat simulation exposed another failure: locking while the
consent dialog was open hid both dialogs but left the main demo disabled.
MSW modal event loops must be unwound inside-out. Private session state is now
cleared immediately, only the child is ended initially, and the parent is
ended after the child's ShowModal returns and its consent cleanup completes.
Duplicate close callbacks are harmless; a queued consent OK cannot dispatch a
request after closing. The same manual scenario now closes both dialogs,
reports the simulated lock and allows Settings to open again, without any new
provider request. This does not validate the actual vault scheduler/PIN.

Generation timing now uses urllib3's total/connect timeout, accepted by the
actual installed Requests adapter, instead of the early 15-second read cutoff.
Virtual-clock responses at 20/50 seconds succeed; results after the existing
60-second deadline and results returned after cancellation are discarded. No
automatic retry, model change or increased total operation deadline was added.
The real adapter is tested against a fake connection pool; no sockets or live
keys are used in automated tests.

After new explicit user authorization, a single manual Gemini request through
the demo's own consent/send button succeeded. Safe stage timestamps were
request_started at 22:33:51.097 and HTTP 200/response_parsed at 22:34:14.773
on 2026-10-02 (about 23.68 seconds); ui_completed followed. The window showed
Ready and a Turkish description matching the demo text and three colored
shapes. No retry or follow-up question was sent, and remembering consent stayed
unchecked. The answer dialog was left open for the user. This confirms the
corrected budget on one real request; it does not establish reliability for all
providers/images, actual billing, or complete NVDA speech behavior.

Verification for these two corrections: failing window-free regressions were
reproduced before the fixes. The final explicitly selected nine-file run passed
**1,376 tests / five GUI cases deselected**, in 17.73 seconds. It covered the
dialog logic, request lifecycle, core, diagnostics, demo, no-window guard and
language/key consistency. This was not the full suite; no wxgui/load tests or
live provider requests ran. The manual lock/consent check above was separate.

Additional demo verification: six explicitly selected, window-free files,
**1,353 passed**. The user-requested manual application was launched separately;
no wxgui tests were run. NVDA behavior and live provider quality still require
the user's observation.

Initial local verification on 2026-10-02: 27 explicitly selected test files, **1,802
passed / five GUI cases deselected**, in 17.86 seconds. `git diff --check` and
an offline `uv lock --check` passed. This was not the full suite. That initial
automated verification opened no application windows and made no provider
calls. Later manual application checks are described separately above.
No commits, pushes or pull requests had been made at that initial stage.

### Upstream integration (2026-10-03)

After explicit user approval for commit, fork feature-branch push and Branch
test build, the feature was preserved in local commit `9d06cf53`. The official
main tip `858f891a` (the source of `v2.0.0.3924alpha`) was then integrated without
text conflicts. The overlapping navigation, accelerator, vault, settings,
build and locale changes were inspected relative to that new upstream tree:
its panel/audio/forwarding changes remain intact alongside the photo feature.
No version bump or alpha release was requested for this feature.

The subsequent serial, explicitly selected **32-file** check passed **2,133
tests / seven native GUI cases deselected** in 26.22 seconds. It extended the
previous selection with window-free panel-switch/visibility/mnemonic, vault
settings and settings wiring/buildability checks. This was not the full suite;
no GUI opt-in, xdist workers, real provider requests or audio devices were used.
Full GitHub CI and test artifact generation are still pending at this point.
Only the fork's feature/photo-description branch is authorized for push; its
main branch is not being updated, avoiding an unintended alpha publication.

The first authorized full CI run, `37073339227` at merge commit `69dd9074`,
passed 11,621 tests and skipped 87, with two failures in the older native
settings-tab tests (386.17 seconds). Their total-page assertions still expected
15/14 pages without the new photo page; the locked-tab index assertions passed
and all seven photo native tests passed. The tests now explicitly check the
photo page's appended index and translated title alongside totals of 16/15.
No production behavior or window safety gate was changed. These native cases
are verified only by the subsequent CI run, never by a local GUI opt-in.

Run `37074231066` at `c6a8f0cf` then completed successfully: **11,623 passed,
87 skipped** in 440.79 seconds, including the seven photo native cases and
all settings-file tab cases. Windows installer/portable ZIP build, ZIP content
verification, checksums and artifact upload also succeeded. Its artifact is
tied to that exact commit, not to later branch changes.

During that run official main advanced to `e46e8347` with only the uninstaller
language correction, its shared installer language helper, tests and changelogs.
Because this affects the installer artifact, it was also integrated without
text conflicts. Five explicitly selected, window-free checks passed **1,310
tests / two compiler cases skipped** in 5.42 seconds; no local compiler was
available for those two syntax-only cases. This last integration requires its
own fresh full CI/build result before its artifact is treated as verified.

Window-free tests use generated photos, temporary encrypted credentials, fake
HTTP responses and unbound GUI methods against plain stubs. Native control
tests are marked `wxgui`, for CI only. **Never** run the full suite or opt into
GUI tests on this user's machine. Real NVDA/Tab navigation, paid provider
quality/cost checks and packaged application validation still need a controlled
manual/CI pass; mocked tests cannot establish those outcomes.

Official contracts:

- [OpenAI vision](https://developers.openai.com/api/docs/guides/images-vision)
- [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data)
- [GPT-4.1 mini modalities](https://developers.openai.com/api/docs/models/gpt-4.1-mini)
- [Gemini image input](https://ai.google.dev/gemini-api/docs/image-understanding)
- [Gemini model catalogue](https://ai.google.dev/gemini-api/docs/models)
- [Gemini terms](https://ai.google.dev/gemini-api/terms)
- [Gemini generation latency and thinking](https://ai.google.dev/gemini-api/docs/troubleshooting)
- [urllib3 timeout budgeting](https://urllib3.readthedocs.io/en/stable/reference/urllib3.util.html#urllib3.util.Timeout)
- [wxWidgets MSW modal dialog implementation](https://github.com/wxWidgets/wxWidgets/blob/v3.2.8/src/msw/dialog.cpp)
