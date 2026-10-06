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
| Menu item, Describe button, Ctrl+Shift+I, the bounded download, the window's lifecycle | `client/ui/conversation_panel/ai_actions.py` |
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

| Provider | Photos, stickers | Voice messages | Video | PDF |
|---|---|---|---|---|
| Gemini | yes | yes | yes | yes |
| OpenAI | yes | yes (dedicated transcription model) | no | yes |
| Claude | yes | no | no | yes |
| Groq | yes | yes (Whisper) | no | no |
| OpenRouter | yes | no | no | yes |

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
- Photos and stickers are re-encoded (metadata removed); video, voice messages and
  PDFs go out exactly as they are, and the consent text says so.
- View-once media, documents that are not PDFs and statuses are never offered.
- Chat names, JIDs and captions are never sent. Answers and the question
  history live in memory only.
- The media is decrypted from the encrypted cache **in memory**; no plain copy
  is written to disk. The original may be downloaded into the usual encrypted
  cache first, through the same bounded download as everywhere else.
- Keys are encrypted with a key stored beside them: this protects against
  accidental disclosure of the file, not against someone who takes the whole
  data folder (stated to the user under Technical information).

## Accessibility contract

- Plain wx controls only: a read-only multiline text for the result, a plain
  `ListBox` whose rows say "name, state, key" for the providers (not a
  `CheckListBox`, whose state NVDA does not reliably announce).
- **No shortcut or mnemonic is written into a label or accessible name.**
  Ctrl+Enter (ask) is announced by `ui/accessible.AccessibleAskQuestion`;
  Ctrl+Shift+I is an accelerator whose menu item shows it the way every other
  message-menu item does; the Describe button reports it through
  `ui/accessible.AccessibleDescribeButton`. `tests/test_ai_media_i18n_keys.py` fails if a label
  carries one.
- All speech goes through `MainWindow.output` (the `speak_output` gate). A
  request speaks its status once at the start; the waiting sound
  (`sound_event_ai_processing`, bundled `ai_processing.wav`) loops until the
  answer exists and is stopped and freed **before** the answer is spoken.
  Reading answers aloud is an option (on by default); only the spoken copy has
  its line breaks joined.
- The API key field is masked; a button reveals it in a read-only field so the
  screen reader can read it back on request.

## The model catalogue

"Automatic (recommended)" follows the provider model in `config.PROVIDERS`;
it saves an empty model id and locks the model fields and "Get models".
Unchecking it allows a fixed model id. This choice is preserved when keys
are reset and re-entered; it does not change the sending-consent rules.

`model_catalog.COMPATIBLE_MODELS` is a short list of exact model ids per
provider, reviewed against the provider's own model pages. "Get models" intersects
what the provider returns for the person's key with that list; it never sends
media or generates anything, follows no redirects and is bounded in pages, bytes
and time. Unknown ids stay usable through the advanced field but are not offered
as reviewed choices. **Adding a model needs the provider's page saying it takes
the input kinds and returns text, and a test** — not a guess from its name.

## Adding a provider

1. `config.PROVIDERS`: name, default model, key/billing/privacy links,
   capabilities, transcription model if it has one.
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
