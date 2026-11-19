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
| One still frame out of a video (the audio is never opened) | `core/ai_media/video_frame.py` |
| Menu item, Describe button, Ctrl+Shift+I, the bounded download, the window's lifecycle | `client/ui/conversation_panel/ai_actions.py` |
| Describe button of the media viewer and of the Status tab (inline player and viewer) | `client/ui/ai_media_launch.py`, `client/status_tab/status_ai.py`, `client/ui/media_viewer.py` |
| Result window and consent | `client/ui/dialogs/ai_result_dialog.py` |
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

A video is never sent as a video: `payload.prepare_media` decodes one frame
locally (about one second in, or the middle of a shorter clip; only the video
stream is opened, so the sound is never read), shapes it like a photo and sends
it as an image. So every provider that takes pictures describes videos, the
original may be up to 64 MiB (it never travels), and `providers.build_request`
refuses a `video` payload outright. Only that frame can be described: the
prompt tells the model it cannot know what happens before or after it.

The person orders the providers and switches each on or off; the order is the
order they are tried in. A provider is only tried for a kind it accepts and
when it holds a saved key.

## What a request is

- **Stateless and inline.** The media travels base64 in the request; nothing is
  uploaded to a provider's file store and no provider-side conversation or
  thread is used. Each follow-up resends the original and the earlier turns as
  text. `store=false` for OpenAI.
- **No redirects, no automatic retry, TLS verification on**, credentials only in
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
  audio transcription model); the chain does not choose arbitrary new models
  or retry the same provider. Unreadable or missing keys skip that provider.
  Switching providers after a timeout may incur usage for both attempts if
  the first request already reached its provider.
- **Language.** The reply language is an instruction in the system prompt
  (`prompts.instructions`), taken from the current application language each
  time; the prompts themselves are English. Audio is transcribed in the
  language spoken, without translating. It is a request contract, not a
  guarantee that a provider follows it perfectly.

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
- Photos, stickers and the frame taken from a video are re-encoded (metadata
  removed); voice messages and PDFs go out exactly as they are, and the consent
  text says so (a video's consent says that only one picture, without sound, goes).
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
