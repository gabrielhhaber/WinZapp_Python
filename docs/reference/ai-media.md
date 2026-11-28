# AI transcription and description of media

> One feature, built from two pull requests (the photo-description work and the
> transcription work). This page is what a maintainer needs to change it
> without breaking what the screen-reader user relies on.

A message's media can be sent, on demand, to the AI providers the person set
up: a voice message is **transcribed**, a photo, sticker or video is
**described** (and, for photos and videos, questions can follow), a PDF is
**converted to accessible text**. Off by default; nothing is ever sent
automatically for an incoming message.

## Where it lives

| Responsibility | Module |
|---|---|
| Providers, kinds, limits, preferences, which action a message offers | `client/core/ai_media/config.py` |
| What goes to a provider, and reading its answer (pure functions) | `core/ai_media/providers.py` |
| One request, the fallback chain, the deadlines, the connection test | `core/ai_media/service.py` |
| Validating and shaping the media before it leaves | `core/ai_media/payload.py`, `image_input.py`, `media_input.py` |
| What the provider is told | `core/ai_media/prompts.py` |
| Reviewed model catalogue and the opt-in model list | `core/ai_media/model_catalog.py` |
| The window's state, immune to late callbacks | `core/ai_media/session.py` |
| The waiting sound | `core/ai_media/feedback.py` |
| The API keys (install-wide, encrypted) | `client/core/ai_credentials.py` |
| Frames out of a video, and the mosaic they are joined into (the audio is never opened) | `core/ai_media/video_frame.py` |
| Menu item, Describe button, Ctrl+Shift+I, the bounded download, the window's lifecycle | `client/ui/conversation_panel/ai_actions.py` |
| Describe button of the media viewer and of the Status tab (inline player and viewer) | `client/ui/ai_media_launch.py`, `client/status_tab/status_ai.py`, `client/ui/media_viewer.py` |
| Result window and consent | `client/ui/dialogs/ai_result_dialog.py` |
| Hierarchical summary: plan and runner (pure) / the window's part | `core/ai_media/summary_parts.py` / `client/ui/dialogs/ai_summary_parts.py` |
| Quick analysis modes, caption/quote context (window part and consent) | `client/ui/dialogs/ai_media_modes.py`; pure parts `core/ai_media/modes.py`, `core/ai_media/context.py` |
| Writing help: free instruction and suggested reply | `core/ai_media/writing.py`, `client/ui/dialogs/ai_writing_dialog.py`, `client/ui/conversation_panel/ai_writing.py` |
| Settings page and the provider window | `client/ui/dialogs/ai_settings_page.py`, `ai_provider_models.py` |
| Manual demo without WhatsApp | `client/ai_media_demo.py`, `client/ui/ai_media_demo.py` |

Preferences live in the install-wide `app.json` under `ai_media` (shared by
every account); the keys are in `ai_credentials.enc`, never in a settings file
and never in an export.

## Providers

Every provider is reached over plain HTTPS with `requests`; no vendor SDK is
shipped (the three SDKs would have added seventeen pinned packages for what is
one POST per request). The table in `config.PROVIDERS` is the single source of what each
accepts today:

| Provider | Photos, stickers, video frames | Voice messages | PDF |
|---|---|---|---|
| Gemini | yes | yes | yes |
| OpenAI | yes | yes (dedicated transcription model) | yes |
| Claude | yes | no | yes |
| Groq | yes | yes (Whisper) | no |
| OpenRouter | yes | no | yes |
| Mistral | yes (selected vision model) | yes (chosen Voxtral model) | yes (fixed Mistral OCR) |

A video is never sent as a video: `payload.prepare_media` decodes frames
locally (only the video stream is opened, so the sound is never read), shapes
the result like a photo and sends it as an image. So every provider that takes
pictures describes videos, the original may be up to 64 MiB (it never travels),
and `providers.build_request` refuses a `video` payload outright.

By default (`ai_media.video_frames` = `"several"`) the picture is a **mosaic**
of up to `video_frame.FRAME_COUNT` (5) frames: `frame_times()` takes the middle
of equal slices of the clip (never the often black first frame; a clip too short
for five frames, at least `_MIN_GAP_SECONDS` apart, gets fewer), `extract_mosaic()`
shrinks each frame as soon as it is decoded (so a large video never holds
several full-size pictures), skips a repeated frame, and `grid_shape()` lays the
cells out left to right, then top to bottom (1-3 in a row, 4 as 2x2, 5-6 as 3x2),
with white gaps and no drawn numbers (no text rendering, so no font). It is one
request, one image, so no provider's contract changed; the mosaic goes through
`prepare_image` like any photo (metadata removed, 8 MiB and `MAX_PIXELS`
limits) and the video's `MAX_PIXELS` check still applies to its stream. The
prompt (`prompts.instructions(..., video_frames)`) says the image is a mosaic in
chronological order, that the model does not know what happens between the
frames and cannot hear the video, and keeps the "do not guess what is said or
any sound" rule. Choosing `"one"` (Settings, Online AI, "Types of media
handled...", "Describe videos from several frames" off) goes back to the single
frame about one second in (or the middle of a shorter clip) and the old prompt
(`extract_frame`). Callers read the choice with
`config.get("video_frames", "several")`: the result window, the automatic
description (`main_window/auto_ai_media.py`) and the Windows OCR button. The
consent says which: `ai_video_frames_notice` (several, without sound) or
`ai_video_frame_notice` (one).

Not done: an "understand the whole video" action that would join the local
Whisper transcription of the sound with the description. Reusing the
transcription flow means a second modal progress run inside the result window,
a second consent (only transcribed text may go to a provider, never the audio)
and merging two answers with two failure modes. It was left out rather than
shipped fragile; see `docs/traps/transcription.md` ("Videos") for the local
half, which already transcribes a video's sound track on its own.

The person orders the providers and switches each on or off; the order is the
order they are tried in. A provider is only tried for a kind it accepts and
when it holds a saved key.

## What a request is

- **Stateless and inline.** The media travels base64 in the request; nothing is
  uploaded to a provider's file store and no provider-side conversation or
  thread is used. Each follow-up resends the original and the earlier turns as
  text. `store=false` for OpenAI.
- **No redirects, TLS verification on**, credentials only in
  headers. Responses are read through `core/bounded_http.py`.
- **Size and time.** Originals up to 20 MiB for pictures (re-encoded below
  8 MiB, metadata removed, orientation applied) and 14 MiB for the rest, which
  is what fits Gemini's 20 MB inline request once base64 has added a third.
  Each provider gets one attempt of 60 s (photos) to 150 s (PDF); one action
  gets two and a half attempts' worth in total (`service.ATTEMPT_SECONDS`).
- **Complete text.** Audio transcripts and converted PDFs above their character
  limits (50,000 and 100,000 respectively) fail with `output_limit`; their endings
  are never silently dropped and reported as a complete result. This local limit
  ends the action without trying another provider automatically.
- A provider's own output cutoff (`MAX_TOKENS`, `max_tokens`, `length` or
  OpenAI's incomplete status) is different: the parser rejects the partial
  answer as `response`, so the next configured provider may still be tried.
  A shorter answer alone does not prove that a transcript or PDF is complete.
- **The fallback chain.** On authentication, quota, server, request, refusal,
  response, network or per-attempt timeout errors the next provider is tried.
  Bad media, a cancel and the overall deadline end the action at once: they
  would fail identically anywhere. When every provider fails the window lists
  who failed how (`service.ChainFailed`).
- Each provider gets one attempt with its configured model (or its dedicated
  audio transcription model); the chain does not choose arbitrary new models.
  The single exception is a short transient pause: a 429 or 503 that carries a
  `Retry-After` of at most `service.MAX_RETRY_AFTER` (5 s, delta-seconds form
  only, read by `errors.parse_retry_after`) gets **one** more attempt at the
  same provider after that pause, when at least `service.MIN_RETRY_SECONDS`
  of the action's budget would remain (`service.retry_pause`). The pause waits
  on the operation's cancel flag, so a cancel ends it at once; a second
  failure goes to the next provider like any other. Without the header, or
  with a longer one, nothing is retried. Unreadable or missing keys skip that provider.
  Switching providers after a timeout may incur usage for both attempts if
  the first request already reached its provider.
- **Language.** The reply language is an instruction in the system prompt
  (`prompts.instructions`), taken from the current application language each
  time; the prompts themselves are English. Audio is transcribed in the
  language spoken, without translating. It is a request contract, not a
  guarantee that a provider follows it perfectly.

## Streamed answers (opt-in)

`core/ai_media/streaming.py` reads a provider's answer as server-sent events.
**Off by default** (`ai_media.ai_streaming`, check box "Show answers as they
arrive" in the "Types of media handled" window); `run_chain(..., on_partial=)`
only streams when a caller passes a listener, and today only the result window
does (`ui/dialogs/ai_result_streaming.py`).

- **What streams.** Text answers of the six providers: descriptions, video
  mosaics, converted PDFs, chat summaries (`streaming.supports_streaming`).
  Voice messages (multipart upload) and Mistral OCR (not a chat request) never
  do, and neither does the writing help window or the automatic description,
  which have no result field to fill. Anything else takes the plain request.
- **The request** is the plain one plus `"stream": true` and `Accept:
  text/event-stream`; Gemini uses `:streamGenerateContent?alt=sse` (key still
  in a header). Same URL host, no redirects, TLS on. A provider that answers
  200 with a plain JSON body anyway is read the old way (`read_stream`).
- **Same answer.** The final text goes through `providers.clean_answer`, the
  tail `parse_answer` uses, and OpenAI's `response.completed` object is parsed
  by `parse_answer` itself. Refusal, `MAX_TOKENS`/`length`/`model_length`/
  `content_filter` and an in-stream `error` raise the same `DescriptionError`
  categories. A stream that ends without its own end marker (`message_stop`,
  `response.completed`, a finish reason, `[DONE]`) is cut off: `response`,
  never a complete answer. Partial text is only offered to `on_partial`; if an
  attempt fails after offering some, the listener gets `("", False)` before
  the next provider starts.
- **Limits.** Cancel and the deadline are checked around every `read1`, and a
  cancel shuts the socket (`RequestToken`) as for a plain body. SSE framing
  makes a body several times larger than the answer, so a stream is bounded by
  `MAX_STREAM_BYTES` (8 x `MAX_RESPONSE_BYTES`) and one event by
  `MAX_EVENT_BYTES`; the text still obeys `MAX_OUTPUT_CHARS`/`output_limit`.
  `Retry-After`, the fallback chain and the usage counters work as before: they
  act on the status, and on the final outcome of each provider.
- **The window** shows only settled text (`settled_prefix`: whole lines, or
  whole sentences while no line has ended), at most once a second
  (`PartialPacer`), with `ChangeValue` and the person's caret or selection put
  back (text only grows at its end). The streamed layout is the final layout up
  to where the answer starts (`stream_base`), so the finished answer replaces
  it without moving anything. **Speech is deliberately not incremental**
  (`docs/traps/screen-reader-speech.md`: announcements queue asynchronously and
  would talk over each other and over a person reading the field): at the first
  settled text the waiting sound stops and the window says once "The answer is
  arriving" (also in the status field); the answer is read once, when complete,
  as without streaming. A failure, cancel or timeout restores what the field
  showed before; Esc and Cancel close the connection at once.
- **Not verified live.** Event shapes follow each provider's documentation and
  synthetic fixtures; no live request was made, and the effect of rewriting the
  focused read-only field on NVDA, JAWS and Narrator is untested. Hence off by
  default. Still open: reading the first paragraph aloud while the rest arrives,
  and streaming the writing help.

## Quick analysis modes

The result window of a picture, sticker, video or PDF has, between the result
and the buttons, a plain `wx.Choice` ("Quick analysis"), a one-line field
("Your instruction", enabled only for the last choice) and an "Analyze" button
(no mnemonics: the window stays free of them, and Enter in the field runs it).
`core/ai_media/modes.py` holds the presets: **extract only the text**, **read
receipt, bill or PIX proof** (fields really present, copied character by
character; an ambiguous digit, amount, date or code is written as seen and
marked "to confirm", never picked; it ends naming what could not be read) and
**read table or menu**, plus the person's own instruction.

A mode changes only the request. It goes through `AIResultDialog._start(...,
regenerate=True, task=True)`: same provider chain, same remembered consent (the
media consent already given in the window), and its answer **replaces** the
first one (a follow-up would echo a long English preset as the "question").
`prompts.instructions(..., task=True)` adds a sentence saying the request is a
specific task that replaces the default layout without inventing anything.
The instruction is the task; text inside the media stays evidence under
`_UNTRUSTED`. Because such an answer is not a description, "Put the description
in the line" is disabled while it is on screen (`_task_answer`).

## Opt-in context for descriptions

Two preferences, both **off by default** and validated in `config.preferences()`
(`context_caption`, `context_quote`), live as check boxes in the "Types of media
handled" window. When on, `core/ai_media/context.py` appends to the opening
description request of a picture, sticker or video (not follow-ups, not mode
runs, not the automatic description, not the saved description) the caption
(300 characters) and the readable text of the message it replies to (160).
Never the chat name, the sender or a JID. It travels only if the message has
such text. The system prompt says it is evidence, not instructions.

The media consent says captions are not sent, so a request that carries them
asks a **second consent** (`ai_context_consent`, remembered in
`consented_context`; a locked chat asks once per window and never remembers).
Removing a key drops this consent with the others.

## Writing help: free instruction and suggested reply

Besides the four fixed actions, `core/ai_media/writing.py` offers **Use my own
instruction** (a one-line field, 500 characters, empty fails with
`ai_error_question` before any consent) and **Suggest a reply draft**. The
instruction is trusted as the task; the draft stays the person's text.

The reply is the only writing action that sends **messages of the chat**: the
last `REPLY_MESSAGES` (10) usable ones, built by
`AIWritingMixin._writing_recent_messages` with the summary's own line format
and `build_unread_transcript` (4,000 characters, newest kept; voice messages are
only the marker, never transcribed), plus the draft (clipped to 2,000
characters; it is guidance, so the action needs a non-empty draft like every
writing action). It is offered only when there is at least one such message.
Its consent (`ai_writing_reply_consent`) says the conversation messages and the
sender names are sent, is remembered in `consented_reply` and is asked again
for every window of a locked chat. The suggestion lands in the result field
like every answer; the draft is replaced only by "Replace draft", as before.
`instructions(..., "writing", context=True)` states that the messages are
evidence, and the question forbids inventing facts, dates, amounts or
commitments.

## Usage counters

`core/ai_media/usage.py` counts, per provider, the answers and the failure
categories of each chain's final outcome (a retried 429 counts once; a cancel,
bad media or a local output limit counts for no provider). Callers of
`run_chain` pass `usage_sink=usage.persist`, which writes into
`ai_media.usage` of the install-wide settings through `AppSettings.update()`
(several account processes share the file); `config.preferences()` validates
it (`usage.validated`: known providers and categories, bounded counts). Only
ids, categories and numbers are stored, never a prompt, answer or file. The
Settings page shows them in the "Usage summary" window
(`ui/dialogs/ai_usage_options.py`: a read-only multiline field and a reset
button). Task routing (a preferred provider per kind) is not implemented.

## Hierarchical chat summary (opt-in, IA-04)

`core/ai_media/summary.py` keeps the newest `MAX_MESSAGES` (200) /
`MAX_CHARS` (15,000) and drops the older messages; the window says how many
(`ai_summary_left_out`). That stays the default. The preference
`ai_media.summary_hierarchy` (**off by default**; check box "Summarize older
messages too (several requests)" in the "Types of media handled" window) lets a
backlog that does not fit be summarized in parts: `core/ai_media/summary_parts.py`.

- **Plan.** `plan_parts` formats every usable message line, groups them with the
  rule of `chunk_lines` (parts of at most `PART_CHARS` = 12,000 characters,
  chronological, **never inside a line**; a line longer than the limit is a part
  of its own, whole; inside the plan such a line is first truncated with the
  `[truncated]` marker, like the ordinary transcript) and keeps the **newest
  `MAX_PARTS` (6)**. Older messages beyond that are left out and counted. One
  part is just an ordinary summary (one request, the usual window); two or more
  go through `run_parts`.
- **Requests and time.** At most `MAX_CALLS` = 7 requests: one per part and one
  final join, all to the chain's providers. Each is a full `run_chain` (fallback,
  `Retry-After` retry) under an `Operation("text")` **of its own**, so a slow part
  cannot starve the next. The window's `Operation` is built with `steps=len(parts)+1`
  (`MediaSession.begin(steps=)`): its `total_seconds` is the sum of the steps', which
  is what the watchdog waits for (+5 s), local providers included (`local=True`
  gives every step `LOCAL_ATTEMPT_SECONDS`; the worst case is long on purpose and
  Cancel is always there). `parent.adopt(child)` makes a cancel (Cancel, Esc, the
  vault, closing) reach the request in flight at once, and `parent.check()` between
  requests stops before the next one starts. `MediaSession.requests`/`MAX_REQUESTS`
  count **actions** (one hierarchical summary is one); the usage counters count every
  request, through the same `usage_sink` of each `run_chain`.
- **Prompts.** `prompts.parts_instructions`: a part gets the usual summary prompt
  plus "this is one part, keep every question, date and link"; the join gets its own
  prompt (merge in chronological order, one final list of what asks something of the
  reader, no invention). The question of a part says "part N of M".
- **Speech.** Not one line per request: the status and the start say "Summarizing
  part 1 of M...", then one line per later part and one for the join
  (`_parts_progress`, ignored once the generation changed). No streaming in this
  mode.
- **Failure.** If a part fails after its chain was tried, **no summary is shown**:
  `PartFailed` names the part and carries the chain's attempts (`failure_text`
  prefixes `ai_summary_part_failed`). A summary with a gap would read as complete
  to a person who cannot see it, and the earlier requests are not wasted, they are
  just not presented as the answer. If only the join fails, every part did succeed,
  so their summaries are offered in order under "Part N of M" with a note
  (`ai_summary_parts_merge_failed`). A cancel or the deadline is never reported as a
  part's failure.
- **Consent.** The text of **all** the messages travels, in several requests, to
  the same consented providers, so the consent is the summary's own plus
  `ai_summary_parts_consent`, which says at most how many parts and requests.
  It is added only when the preference is on and `may_need_parts` (estimated before
  the voice messages are transcribed, leaning to yes) says the backlog may not fit.
  The consent stays remembered per provider as before (`consented_text`); a
  provider already remembered is not asked again, the check box in the settings is
  the person's informed choice and the window announces every part. If the backlog
  grew past the limits after the consent (voice transcriptions), the ordinary cut is
  used instead of making requests nobody was told about.
- **Follow-up questions and "Generate again".** A follow-up is the ordinary single
  request; its input (`session.media`) is the partial summaries as the join got
  them (`ai_summary_parts_notice` says so), not the full messages. "Generate again"
  repeats all the requests of the plan. Not done: choosing the parts to redo,
  re-asking only the failed one, and a per-summary price estimate. Not verified live
  with any provider; tests use fakes (`tests/test_ai_summary_parts.py`).

## Consent and privacy

- Before anything is sent the window names **every provider the media may reach,
  in order**, and asks. "Remember" stores the consent per provider; a provider
  whose key is removed loses it. A **locked chat** asks every time the window
  opens and cannot be remembered; locking the vault, leaving the chat, hiding
  or closing the app and deleting the message all close the window and drop its
  media and answers (`close_ai_media`).
- Resetting all keys also revokes every remembered provider consent on Apply/OK,
  even if a replacement key is entered before applying. Cancel keeps the saved
  keys and consents unchanged.
- Photos, stickers and the frames taken from a video are re-encoded (metadata
  removed); voice messages and PDFs go out exactly as they are, and the consent
  text says so (a video's consent says that pictures, without sound, go: several joined in one image, or one if the person chose that).
- View-once media and documents that are not PDFs are never offered. Photo and
  video statuses are offered in the Status tab (Alt+5), in the inline player
  and in the separate viewer, under the same rule as the message menu; a status
  is not a chat, so its window's identity uses `status@broadcast`, it is never
  locked, and its original comes from `get_base64_from_media` (not the encrypted
  cache), checked against the same size limit.
- Chat names, JIDs and captions are never sent. Answers and the question
  history live in memory only.
- The media is decrypted from the encrypted cache **in memory**; no plain copy
  is written to disk. The original may be downloaded into the usual encrypted
  cache first, through the same bounded download as everywhere else.
- Keys are encrypted with a key stored beside them: this protects against
  accidental disclosure of the file, not against someone who takes the whole
  data folder (stated to the user under Technical information).

## Accessibility contract

- The Describe button sits right after Save as everywhere: in the message
  actions, in the media viewer's bottom row and in the Status tab's inline
  viewer. The viewer and the Status tab bind Ctrl+Shift+I too, so the shortcut
  `AccessibleDescribeButton` reports is true. The result window opens over the
  viewer (its parent), not over the panel behind it, and focus returns to it.
- Plain wx controls only: a read-only multiline text for the result, a plain
  `ListBox` whose rows say "name, state, key" for the providers (not a
  `CheckListBox`, whose state NVDA does not reliably announce).
- **No shortcut is written into a label or accessible name, and the result
  window carries no mnemonic.** Ctrl+Enter (ask) is announced by
  `ui/accessible.AccessibleAskQuestion`;
  Ctrl+Shift+I is an accelerator whose menu item shows it the way every other
  message-menu item does; the Describe button reports it through
  `ui/accessible.AccessibleDescribeButton`. A mnemonic also fires on the bare
  letter while a button has focus, so the result window (which has a question
  editor next to its buttons) stays free of them. The consent window, the
  Settings page and the provider window do carry mnemonics, one letter per
  control and never the letter of OK, Apply or Cancel;
  `tests/test_ai_media_i18n_keys.py` lists those controls and fails on a
  collision, on a missing one, or on a mnemonic anywhere else. A control's
  `name` is its label without the `&`.
- Declining the consent prompt of the first request closes the result window:
  nothing was shown, so focus must not land in an empty result.
- The provider list always has the first provider selected, so it announces it
  when focused. The provider window opens with the configured API key filled in
  (masked), and only stages a key that differs from it.
- All speech goes through `MainWindow.output` (the `speak_output` gate). A
  request speaks its status once at the start; the waiting sound
  (`sound_event_ai_processing`, bundled `ai_processing.ogg`) loops until the
  answer exists and is stopped and freed **before** the answer is spoken.
  Reading answers aloud is an option (on by default); only the spoken copy has
  its line breaks joined.
- The API key field is masked; a button reveals it in a read-only field so the
  screen reader can read it back on request.

## The model catalogue

"Use the model recommended by WinZapp" (the automatic checkbox) follows the provider model in `config.PROVIDERS`;
it saves an empty model id and hides the model fields and "Get models" (a
disabled edit field is announced as unavailable or read-only by screen
readers). Unchecking it shows them and allows a fixed model id. This choice is preserved when keys
are reset and re-entered; it does not change the sending-consent rules.

`model_catalog.COMPATIBLE_MODELS` is a short list of exact model ids per
provider, reviewed against the provider's own model pages. "Get models" intersects
what the provider returns for the person's key with that list; it never sends
media or generates anything, follows no redirects and is bounded in pages, bytes
and time. Unknown ids stay usable through the advanced field but are not offered
as reviewed choices. **Adding a model needs the provider's page saying it takes
the input kinds and returns text, and a test** — not a guess from its name.

## Model routing by provider

| Provider | Selected model field | Audio | PDF |
| --- | --- | --- | --- |
| OpenAI | Images/stickers/video frames/PDF | Fixed `gpt-4o-mini-transcribe` | Selected model |
| Groq | Images/stickers/video frames | Fixed `whisper-large-v3-turbo` | Unsupported |
| Gemini | Every supported media kind | Selected model | Selected model |
| Claude / OpenRouter | Every supported media kind | Unsupported | Selected model |
| Mistral | Images/stickers/video frames (`ministral-3b-latest` by default) | Chosen voice-message model (`voxtral-mini-transcribe-3` by default) | Fixed `mistral-ocr-4` |

Mistral's vision request uses `/v1/chat/completions`, with a base64 data URL,
the system instruction, question and earlier text turns. Audio is an inline
multipart `/v1/audio/transcriptions` request with the original file; no forced
language, translation prompt or unsupported `response_format` field. PDFs use
one `/v1/ocr` request with a base64 PDF document and `include_image_base64=false`.
No remote file store or page subset is used. OCR pages are validated, ordered
and joined; gaps, duplicates, a `usage_info.pages_processed` that disagrees and
oversized output fail instead of appearing complete. OCR extracts text, not
descriptions of PDF images. Its Markdown is made readable before it is shown
(`mistral_ocr.readable_text`): image links such as `![img-0.jpeg](img-0.jpeg)`
are dropped, `<br>` becomes a line break (a space inside a table row), HTML
entities are decoded and runs of blank lines collapse; headings, lists and
tables are kept.

The OCR id is a major alias, not `-latest`, so a new generation cannot
change the answer's shape under a released build. `mistral-ocr-4` is OCR 4.1
(`mistral-ocr-4-1`, GA since 2026-07-16); a minor id is not pinned because
OCR 4.0 was retired three months after release.

Audio goes to the voice-message model chosen in the provider window, from a
reviewed list (`Provider.transcribe_choices`, `config.TranscribeModel`):

| Id | Model | Status |
| --- | --- | --- |
| `voxtral-mini-transcribe-3` (default) | Voxtral Mini Transcribe 3 | Public Preview since 2026-10-20 |
| `voxtral-small-transcribe-3` | Voxtral Small Transcribe 3 | Public Preview since 2026-10-20 |

These are the two models Mistral's file-transcription guide accepts; its error
table lists any other `model` under `400 Invalid model`. The GA
`voxtral-mini-2602` (Voxtral Mini Transcribe 2) is left out for that reason
(it was not tried live), and the realtime model because it takes a stream, not
a file. Mini is the default because it is the one tested live. Both are
previews, which may change or be withdrawn without notice, so the list marks
them "(preview)"; when one becomes GA, drop its flag (and pick the GA one as
the default). The choice is saved as `ai_media.transcribe_models`;
`config.transcribe_model()` keeps a saved id only when it is on the list, and
`providers._transcription()` resolves the model it is given the same way, so
neither a hand-edited settings file nor a vision id reaching the audio path
can send another model. `config.models_for(config, kind)` hands the voice
model to the chain for audio only. Adding a model needs its page on
docs.mistral.ai saying it transcribes files, plus a test. Statuses read on
Mistral's models pages on 2026-10-28. OpenAI and Groq keep their one fixed
model.

Before media goes to Mistral the consent window adds `ai_mistral_notice`, like
`ai_gemini_notice` for Gemini: on Mistral's free Experiment plan API requests
may be used to train its models unless the person opts out in their Mistral
account, while the paid Scale plan is opted out by default (Mistral help
center, "Can I opt out of my input or output data being used for training?",
read 2026-10-28). WinZapp cannot tell the plan from a key, so the notice is
shown whenever Mistral may receive the media: in the consent before one
request and in the one that turns automatic transcription or description on
(`config.notice_keys`, shared with Gemini's notice).

Get models lists reviewed vision models available to the key, excluding
Voxtral, OCR, TTS and realtime models. The provider window explains which
model each kind goes to in a read-only field before the model fields
(`Provider.model_note`), followed by the voice-message model list. Other
providers' routing is unchanged.

Live acceptance on 2026-10-25 used generated media only: Voxtral Mini Transcribe 3
transcribed a locally generated spoken sentence and Ministral 3 3B described a
generated picture with the expected number. Mistral Small returned 429; Mistral
OCR returned 429 (code 1300). OCR payload/parser tests pass against synthetic
responses; successful live OCR is not claimed. No real messages or documents
were sent. Model-list HTTP 200 proves availability, not free billing: Free mode
has included usage and per-model/audio/OCR limits, visible in the Admin Limits
page. No billing settings were changed.

Contract sources (checked 2026-10-25):
[audio models](https://docs.mistral.ai/studio/audio/overview),
[transcription endpoint](https://docs.mistral.ai/api/endpoint/audio/transcriptions),
[model discovery and retrieval](https://docs.mistral.ai/api/endpoint/models).

[Vision](https://docs.mistral.ai/studio/conversations/vision),
[OCR](https://docs.mistral.ai/studio/document-processing/basic_ocr),
[included usage and limits](https://docs.mistral.ai/admin/billing-usage/usage-limits),
[API pricing](https://docs.mistral.ai/inference/pricing).

## Local models (Ollama, LM Studio)

Design (IA-01). Two providers, `local_ollama` and `local_lm_studio`, are
entries of `config.PROVIDERS` with `Provider.local=True` and a `default_url`
(`http://localhost:11434/v1`, `http://localhost:1234/v1`), capabilities
`{image, text}` only (so photos, stickers, video mosaics, summaries, writing
help, quick modes; never voice or PDF). They are entries, not a parallel table,
because every place that names a provider (`PROVIDERS[p].name`, usage counters,
streaming, `build_request`, the chain) already reads this table. The ids are
not `ollama`/`lm_studio`: message translation uses those for its own,
unrelated setting (`message_translation.LOCAL_LLM_URLS`); translation reads
`config.KEYED_PROVIDERS` (the online ones) so it never lists the new entries.

Audit of what iterates `PROVIDERS`:

| Place | Treatment |
|---|---|
| `ai_credentials.CredentialStore._accepts` | `KEYED_PROVIDERS`: a local server never has a key, and an older build reading the vault would call an unknown name a damaged store |
| `config.preferences` | order/disabled/models/consents accept them; `local_urls` is new (strict reading, `""` when invalid, never another host); no "recommended model" (`auto_models` lists keyed providers only) |
| `config.chain` / `offered_kind` / `auto.usable_providers` / summary / writing | a local provider counts when it is on, takes the kind and `local_ready` (address and a chosen model); `has_key` is ignored for it |
| `providers.build_request` / `parse_answer` | chat-completions shape, `image_url: {url}`, no `Authorization` header, address re-read with `local_url.normalize_url` at build time; `<think>...</think>` at the very start of an answer is dropped (`clean_answer`) |
| `service.run_chain` | `key_for` is never called for a local provider; the address comes from `endpoints=config.endpoints(config)`; a bad address is `local_url`, which moves on to the next provider |
| `service.probe_request` / `probe_connection` | local: `GET {base}/models`, no key, no model needed |
| `message_translation` (`PROVIDER_NAMES`, `list_models`), `translation_settings.ORDER` | `KEYED_PROVIDERS`; tested |
| consent (`ai_result_dialog.needs_consent`/`ask_consent`, writing window, context consent, settings' auto toggle) | `config.outbound(config, providers)`: only providers whose data leaves the computer are named, asked and remembered |
| `summary_voice_choice` | audio only; local providers do not take audio, so unchanged |

Privacy and security:

- `local_url.normalize_url`: only `http` (loopback only: `localhost`, `127.0.0.0/8`,
  `::1`) or `https` (anything else); no user name or password, query,
  fragment, `;`, `%`, `..` or space; at most 200 characters; `/v1` when no path.
  Redirects are never followed (`post_answer`), so nothing goes to a second host.
- An address that is not this computer is a **remote server**: `config.sends_out`
  is true, so it asks for consent like any online provider (media, text, writing,
  context and reply lists), with `ai_local_remote_notice`; the window and the
  list row say so. Plain `http` to a LAN host is refused on purpose (private
  pictures in clear text); use `https`. Changing the address drops its consent.
  Pressing OK in the provider window also says the warning aloud
  (`ai_local_provider_dialog.spoken_warning`, once, only when the way it sends
  out is new), since the read-only field may never have been visited.
- An Ollama **cloud model** (id ending in `-cloud` or `:cloud`, case-insensitive:
  `config.is_cloud_model`) answers on localhost but Ollama forwards the prompt
  and the images to its cloud. The address says nothing about that, so
  `config.sends_out` is also true for a local provider whose chosen model is a
  cloud one: it asks for consent in every list (media, text, writing, context,
  reply, automatic runs), is never remembered for a locked chat, and the consent
  carries `ai_local_cloud_model_notice` (`notice_keys(providers, config)`; with a
  remote address too, both notices). The list row reads `ai_local_state_cloud`
  (`local_status` is `"cloud"`). Moving the model to or from a cloud id drops the
  provider's remembered consent, like a changed address
  (`ai_local_settings.changed_cloud_models`).
- A local request sends no credential at all, never another provider's key, and
  a loopback request ignores the proxy settings of the environment
  (`Request.loopback` -> `session.trust_env = False`), so a proxy never sees it.
- A locked chat asks nothing when every provider of the chain is on loopback; if
  an online (or remote) provider is also in the chain, the consent is asked and
  names only that one. "Every provider on loopback" means the address is
  loopback and the model is not a cloud one. Nothing logs an address, a model
  answer or a prompt.
- Local audio is not AI media, but it touches disk too: live captions
  (`call_captions_transcriber`) and dictation write one short temporary WAV
  per phrase/recording into a private `winzapp-dictation-*` folder for ffmpeg,
  deleted when the run ends and swept at startup (`temp_sweep.PREFIXES`). A hard
  kill (power loss, Task Manager) can leave it there until the next start. The
  user guide says so in the Calls section (every locale: "short temporary audio
  file ... deleted right after").
- Timeouts: `ATTEMPT_SECONDS` is unchanged. A local attempt gets
  `service.LOCAL_ATTEMPT_SECONDS` (240 s, writing 120 s) because the model may
  have to be loaded first and a vision model on a CPU takes minutes;
  `Operation(local=True)` adds only the difference to the total, so the online
  providers behind it keep their time. Callers pass `local=config.has_local(providers)`.
- The model is chosen by the person ("Get models" lists `/models`, or type the
  id): there is no default, so with no model the provider is not in the chain
  and the window and the list row say why (`local_status`).

UI: `ai_local_provider_dialog.py` (address, Get models, Model, Test connection;
reuses `ModelSelectionMixin`, whose `_model_source()` is the hook) and
`ai_local_settings.py` (the list row and the window's hand-off). Not verified
live against a running Ollama or LM Studio: shapes follow their OpenAI-compatible
documentation and synthetic fixtures. Not done: checking that the chosen model
is listed and sees images (the test only proves the server answers), and
streaming of `<think>` text in the partial view.

## Adding a provider

1. `config.PROVIDERS`: name, default model, key/billing/privacy links,
   capabilities, transcription model if it has one, and a `model_note` key
   when the model field does not serve every kind.
2. `providers.build_request` / `parse_answer`, with the exact shape its docs
   specify, and a test in `tests/test_ai_media_providers.py`.
3. `service.probe_request` and `model_catalog` (list endpoint and reviewed ids).
4. Nothing in the UI changes: the list, the provider window and the "handles"
   line read the table. Provider names are not translated.

## What is and is not verified

Automated tests use synthetic data and fake transports: every payload shape,
the parsers, the chain, the deadlines and cancellation, consent, the settings
logic and the localized strings. **Calls to the live providers were not made
for this merged feature**: the request shapes were written from each provider's
documentation. Only the Gemini photo path descends from code its author
exercised live; the OpenAI, Claude, Groq and OpenRouter requests and every
audio, PDF and video path were not. Real NVDA/JAWS behaviour of the merged windows, the
packaged executable, and the Turkish and Romanian wording (translated from
English) still need a native, manual pass.
Mistral was added later with live smoke tests on synthetic media: the audio
transcription and the image description succeeded, while the live OCR request
returned HTTP 429, so the PDF path is covered only by the automated tests.
