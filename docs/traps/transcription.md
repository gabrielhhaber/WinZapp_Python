# Local transcription (Whisper, issue #112)

> Why faster-whisper is the default and whisper.cpp the option, what the
> catalogues pin, why the device and precision are decided right before a run,
> how a transcription survives every resync, and what the log may never hold.

Voice and audio messages are transcribed on the user's own machine with
Whisper; nothing leaves it. Two backends: faster-whisper (CTranslate2), which
ships with WinZapp, and whisper.cpp (`whisper-cli.exe`), downloaded on demand.

- **Pure logic** is `client/core/transcription/`: no wx, and no top-level
  import of `faster_whisper`/`ctranslate2`. They ship in `requirements.txt`,
  but a copy where they are missing or their DLLs will not load must still
  open. If they are missing, the run falls back to whisper.cpp when its
  program is installed, or answers `BACKEND_MISSING`
  (`backend.available_backend_ids()`, which `is_available()`'s `find_spec()`
  feeds). If they are present but their DLLs will not load, `is_available()`
  cannot tell, and the first load answers `BACKEND_MISSING`
  (`faster_whisper_backend._whisper_model_class()`). Either way, nothing takes
  the conversation window down at import.
  `tests/test_transcription_core.py::test_the_backend_is_never_imported_at_module_level`
  pins it. The helpers every module of the package shares (cancellation,
  progress, `unlink`, `remove_empty_dir`, `kill_process`, `private_temp_dir`)
  are `_fileops`: use them, do not write a seventh copy.
- **The flow** from the keypress to the text is
  `client/ui/transcription_flow.py`; the dialogs are
  `client/ui/dialogs/transcription_progress.py` and
  `client/ui/dialogs/transcription_result.py`.
- **The Settings tab** is `client/ui/dialogs/transcription_tab.py`
  (`TranscriptionTabMixin`), with
  `client/ui/dialogs/transcription_external.py`,
  `client/ui/dialogs/transcription_whisper_cpp.py` and
  `client/ui/dialogs/transcription_precision.py` beside it, all mixed into
  `SettingsDialog`.
- **The window's half** (storing and deleting) is
  `client/main_window/transcription_store.py`; the panel's wiring is in
  `client/ui/conversation_panel/accelerators.py`,
  `client/ui/conversation_panel/message_menu.py`,
  `client/ui/conversation_panel/message_accels.py`,
  `client/ui/conversation_panel/conversation_navigation.py` and
  `client/ui/conversation_panel/media_files.py`.

Nearly every module opens with a docstring listing the decisions it carries.
What follows is what crosses modules or has already cost a bug.

## Two backends, and why faster-whisper is the default

The issue was reported from a Blackwell card (sm_120). `backend.BACKEND_IDS`
is the order "automatic" tries: faster-whisper first wherever it can run,
whisper.cpp only when the user chooses it or faster-whisper cannot run.

**faster-whisper never gets int8 on a card under "automatic".**
`device.select_compute_type()` answers int8 at *no* compute capability: float16
from 7.0 (and for an unknown capability), float32 below it. It answers int8
only on the processor, where int8 is what makes CPU transcription usable at
all. int8 on a card is reachable only as
the user's own precision choice (see Precision below), and there the sm_120
veto applies: `device.gpu_supports_int8()` is False from (12, 0) up *and* for
an unknown capability. The reason: the CTranslate2 builds that run on sm_120
(>= 4.6.3, CUDA 12.8) are compiled with int8 disabled for it, true of the
pinned `ctranslate2==4.8.2`, so every int8 flavour fails at model load.
`_int8_safe()` guards `select_compute_type()`'s own answer; a user's choice is
vetoed in `precision.offered_compute_types()`. Re-check both on a CTranslate2
bump.

**whisper.cpp** (the `client/core/transcription/whisper_cpp_*.py` modules,
`client/core/transcription/management_whisper_cpp.py`,
`client/core/transcription/external_ggml.py`,
`client/ui/dialogs/transcription_whisper_cpp.py`) is pinned to release
**b4938** (`whisper_cpp_builds.RELEASE_TAG`; researched 2026-09, the CLI flags
re-read 2026-10-05). What crosses modules:

- **The CPU build is the floor.** `WhisperCppBackend.is_available()` asks for
  it, and the management actions never leave the CUDA build without it.
- **The CUDA 12.4 build only for capability [5.0, 12.0)**
  (`whisper_cpp_builds.cuda_build_supported()`; 11.8 is not offered). It has
  no Blackwell kernels, so `device.resolve_whisper_cpp_device()` sends sm_120
  or an unknown capability to the processor with
  `REASON_CUDA_BUILD_UNSUPPORTED`, said even under "automatic".
- **whisper-cli's `-l` defaults to "en"**, so
  `client/core/transcription/whisper_cpp_cli.py` always passes one: `auto`
  unless a language is forced. Forgotten, a Portuguese note comes back as fluent
  invented English.
- **It runs at `BELOW_NORMAL_PRIORITY_CLASS`**, or the screen reader starves.
- **The voice-activity model** (`whisper_cpp_catalog.VAD_MODEL`, from
  `ggml-org/whisper-vad`) comes with the first GGML model, and best-effort
  with the program (`management_whisper_cpp.ensure_vad()`, `strict=False`):
  a network that blocks Hugging Face must not block installing from GitHub.

**Why faster-whisper stays the default.**

- It ships in the install, while whisper.cpp is a download.
- It runs on the sm_120 card the issue came from, in float16. The pinned
  whisper.cpp CUDA build does not, and won't while the pin predates
  Blackwell.
- It keeps a model loaded between notes. whisper-cli loads the model inside
  every run.
- Two of whisper-cli's outputs, the detected language and its probability,
  were not yet confirmed on a real run (the docstring of
  `client/core/transcription/whisper_cpp_cli.py`).

Revisit the order only after a new pin has been run on Blackwell.

## Catalogues

Both model catalogues pin a Hugging Face revision (a commit sha, never `main`)
and every file's exact size; `client/core/transcription/model_catalog.py` has a
sha256 for model.bin only, `client/core/transcription/whisper_cpp_catalog.py`
for every file. The program's zips are pinned by release tag, size and sha256
(`client/core/transcription/whisper_cpp_builds.py`), the cuBLAS wheel by URL,
size and sha256 (`client/core/transcription/cuda_runtime.py`).

A wrong revision or a renamed file looks fine to a fake session. The live
tests, `TestTheCatalogueUrlsAreLive`
(`tests/test_transcription_model_store.py`), `TestTheGgmlUrlsAreLive`
(`tests/test_transcription_whisper_cpp_catalog.py`) and the runtime ones,
run only with `WINZAPP_RUN_NETWORK_TESTS=1`. Run them whenever a pin changes.

**Single-language models are offered, and fenced.** These cannot detect a
language or hear another one:

- the English-only ones: the `.en` models, and the distilled ones
  (distil-small.en, distil-medium.en, distil-large-v2, distil-large-v3 — its
  model card lists "en" alone — and distil-large-v3.5, which is official,
  from Hugging Face's distil-whisper team). For whisper.cpp the first four
  also come as f32, the only 32-bit files: planned at the twin's figure plus
  the bytes they add over their f16 sibling, and never picked by the
  automatic choice;
- third-party fine-tunes: KBLab (Swedish), ivrit-ai (Hebrew, Yiddish) and
  kotoba (Japanese).

Handed audio in another language, such a model answers with confident text in
its own. So `language` is what everything keys on:

- `preferences.resolve()` forces the run to the model's language, and the
  run says so.
- `device.auto_select_model()` never picks one unless it is the user's
  language.
- Rule 2 of `auto_select_model()` (download the largest that fits) never
  picks a third-party model. ivrit's large-v3 weighs exactly what large-v3
  does, so size alone would choose one by accident. One already on disk is the
  user's own, and rule 1 may still use it.
- Neither rule ever picks a 32-bit GGML file, not even one on disk: ranked by
  its larger memory figure it would beat a better model (distil-small.en at
  32 bits over small.en). Only the user's own choice runs one.

`kotoba-whisper-bilingual-v1.0-faster` is deliberately absent: its repository
has no `tokenizer.json`, and faster-whisper would fall back to
`openai/whisper-tiny`'s pre-v3 vocabulary for a v3 model.

**Downloads** go through `client/core/transcription/model_store.py`, for the
GGML files too (`client/core/transcription/whisper_cpp_store.py`). A file
streams into `<name>.part`, is hashed as the bytes go past, and is
`os.replace()`d onto its final name only after it verifies. A file at its final
name with the right size is *believed* installed, because hashing 3 GB every
time the model list is drawn is not an option. Only a file with a digest resumes
with `Range`. For the others, a stale `.part` spliced onto the rest would pass
the size check and die later as an opaque tokenizer error.

**The models folder is install-wide.** Every account process shares it, and
the GGML files sit in their own `ggml-<name>` folders in the same root (one
"Models folder" setting, one move). So:

- Every mutation holds `coord_locks.models_lock()` (`client/coord_locks.py`).
- Two spellings of the folder are compared with `coord_locks.canonical_dir()`,
  never `abspath`, which resolves neither case, junctions nor `subst` drives.
- The setting is reached through `SettingsDialog._install_wide_settings()`,
  which reads `MainWindow._app_settings`, **with the underscore**. Nothing
  ever sets a bare `app_settings`. The models folder has no fallback copy in
  `settings["general"]`, so the bare spelling silently never writes or reads
  the folder the user chose. The online AI had the same bug
  (`tests/test_ai_media_tls_and_shared_settings.py`).

## Precision (part 11)

`client/core/transcription/precision.py` and
`client/ui/dialogs/transcription_precision.py`; faster-whisper only (a GGML
file's precision is its quantization).

- **CTranslate2 refuses, it does not convert.** An explicit compute type the
  device cannot run raises a `ValueError` at load. So
  `precision.resolve_compute_type()` replaces the choice *before* the load and
  the run says "you chose X, Y is used". A refusal that still gets through is
  `PRECISION_UNSUPPORTED` (`faster_whisper_backend.classify_backend_error()`).
- **The list comes from the tab's off-thread probe**
  (`ctranslate2.get_supported_compute_types()`), minus the sm_120 int8 veto,
  which also applies to an unknown capability.
- **The memory estimate rescales, the ranking does not.**
  `precision.memory_compute_type()` feeds `device.model_fits()`, but
  `auto_select_model()`'s `_rank()` keeps the catalogue's figure: rescaled, it
  would rank medium over large-v3-turbo under int8.

## Models already on disk (parts 10a, 10b)

Read the docstrings of `client/core/transcription/external_models.py` (folders)
and `client/core/transcription/external_ggml.py` (single GGML files); the job is
`client/core/transcription/external_job.py` and the tab
`client/ui/dialogs/transcription_external.py`. The user's files are never
copied, moved or deleted, and only a digest makes one a catalogue model. What
crosses modules:

- **A custom model is never in `usable_catalogue_ids()`**, which is what
  `preferences.resolve()` and `device.auto_select_model()` are handed: nothing
  says how much memory it needs.
- **A reference inside WinZapp's own models root is refused**, because
  `model_store.remove_model()` would otherwise delete what the user believes
  is external.
- **A file is called a file.** A blind user told "the folder ggml-small.bin"
  looks for a folder; the file/folder wording lives in
  `client/core/transcription/external_view.py`.
- **A recheck that finds other contents keeps the stored id**
  (`client/core/transcription/external_ggml.py`, `accept_ggml_file()`): the
  dialog names the entry whose size the file now has, the record keeps the
  model the user chose, marked unverified, and an unverified reference never
  runs. Relabelling it after the first entry of that size turned a changed
  distil-large-v3 into a "damaged distil-large-v3.5".

## CUDA libraries (faster-whisper)

The ctranslate2 wheel does not ship cuBLAS. `ctranslate2.dll` opens
`cublas64_12.dll` by name when the model first goes on the GPU. So on a machine
with an NVIDIA driver and no CUDA Toolkit (nearly every release install) the
device is counted, CUDA is chosen, and the load dies. Counting devices proves
nothing:

- **`device.probe_cuda_libraries()` is the test**, and `cuda_usable()` vetoes
  the GPU when it measures False.
- **The runtime** (cuBLAS + cuBLASLt from a digest-pinned NVIDIA wheel; cuDNN
  measured unnecessary) is downloaded on demand by `cuda_runtime`.
- **The probe must search the way CTranslate2 searches**: the `winmode` pair
  in `_load_cuda_library()`. The bare name gets `winmode=0`, because ctypes
  otherwise forces `LOAD_LIBRARY_SEARCH_DEFAULT_DIRS`, while CTranslate2's raw
  `LoadLibrary` follows the process's search order, which a frozen build's
  PyInstaller bootloader sets through `SetDllDirectory`. With default flags, a
  real onedir build reported a cuBLAS reachable through `PATH` as missing.
- **The answer is memoized per process.** Every change to the runtime on disk
  must invalidate it: `register_cuda_library_directory()` does so itself,
  and a removal (which a repair goes through) calls
  `forget_cuda_library_answer()`. A stale True keeps choosing a GPU whose
  DLLs are gone.

None of this applies to whisper.cpp, whose CUDA build carries its own
libraries (`resolve_whisper_cpp_device()`).

## TLS

The downloads and probes below go through
`client/core/tls_trust.py`, which verifies against the Windows trust store via
`truststore` and falls back to a plain session without it. `requests` with
certifi fails with `CERTIFICATE_VERIFY_FAILED` wherever HTTPS is intercepted
locally (an antivirus with HTTPS scanning, a corporate proxy). That was
measured against huggingface.co, nodejs.org and api.github.com.

On it today:

- the transcription downloads: `model_store`, `cuda_runtime` and
  `client/core/transcription/whisper_cpp_runtime.py`;
- Node.js (`client/ui/dialogs/node_download.py`) and the updater
  (`client/updater.py`);
- the WhatsApp reachability probe (`client/main_window/connection.py`);
- the online AI (`client/core/ai_media/`).

Still outside it: `client/ui/dialogs/api_setup.py` (the GitHub release lookup
and the WPPConnect Server download) and `client/core/link_preview.py`.

`tests/test_tls_trust.py`'s `_DOWNLOADERS` forbids a bare `requests.get(` or
`requests.Session()` in the files it lists. `cuda_runtime`'s default session
is also pinned by
`tests/test_transcription_cuda_runtime.py::TestInstalling::test_the_session_goes_through_the_system_trust_store`.
A new downloader belongs on that list.

## Probe right before deciding, and never on the wx thread

Device and model are chosen against **free** VRAM/RAM measured immediately
before the run (`client/core/transcription/message_run.py`,
`client/core/transcription/job.py`), never against a probe from
startup. At startup Chromium has not loaded WhatsApp Web yet, so the free
figure is the high one. A model chosen against it no longer fits when it
loads, and CTranslate2 aborts the run rather than degrading.

The first `device.probe_hardware()` of a process was measured at 0.67 s on a
machine with no graphics card, almost all of it the ctranslate2 import; on a
machine with a card NVML and the compute-type queries add more, so every probe
stays off the wx thread, where NVDA could not query the control the user just
reached meanwhile:

- `MessageTranscription` probes, downloads and decrypts on one worker behind
  the already-open progress dialog, and Cancel works from the first moment.
- The Settings tab takes its first probe through
  `management.probe_in_background()`, never on opening the dialog.

## Threads and modal dialogs

**Callbacks cross only through `wx.CallAfter`.** Run and job callbacks fire on
the worker thread
(`tests/test_transcription_flow.py::TestTheRunsThreadsOnlyCrossWithCallAfter`).

**`TranscriptionProgressDialog.run()` queues the job's start with
`wx.CallAfter` before `ShowModal()`.** wx allows `EndModal()` only on a loop
that is running. A job that finished before the modal loop existed would
assert (the same rule as the pairing dialogs, `docs/traps/pairing-flow.md`).
`tests/test_transcription_progress_dialog.py::TestTheEnd::test_the_job_is_started_from_inside_the_modal_loop`
pins the order.

**The device is announced only on entering `PHASE_LOADING_MODEL`**
(`phase_status_text()`). The job fills in `device`/`device_reason` just before
that phase. Read earlier they are None, and `device_reason_i18n_key(None)`
answers "you asked for the processor", which is false on a machine with a
working GPU.

**Opening Settings on the tab goes through
`SettingsDialog.show_transcription_tab()`**: `FindPage()`, then
`ChangeSelection()`, then a queued `wx.CallAfter`. `SetSelection()` fires the
page-changed event before the window exists, so the tab's one-time warning (a
replaced model, folders it cannot delete) is spoken and spent under the new
window's own announcement.

**The tab is found by `FindPage()`, never by index**, there and in
`_refresh_dialog_labels()`. The optional "Locked chats" tab shifts it between
two positions. It is appended after the AI page; the Shortcuts mixin then
appends its own page after Transcription. The hardcoded indexes of older tabs
do not move, and both later pages are located through `FindPage()`.

## In the conversation

**The default shortcut is `Alt+Shift+T`**, in the panel's table
(`client/ui/conversation_panel/accelerators.py`). `Alt+T` is already the
conversation's presence announcement, and that one is registered in
MainWindow's own table (`client/main_window/shortcuts.py`), so grepping
`client/ui/conversation_panel/` alone misses the clash.

**Local media is Fernet-encrypted** (`voice_messages/<id>.msv`,
`media/<id>.wzmedia`). `client/core/transcription/message_audio.py` decrypts it
to a randomly named temporary, because the real name is the message id. The
temporary is deleted on every way out: success, failure, cancellation, CPU
re-run.

**The format is sniffed first, and anything unrecognised is refused.** WinZapp's
own voice note can fall back to headerless PCM in its `.msv`, with the
capture rate recorded nowhere. The sniff does *not* prevent invented text:
under `audio_prep`'s arguments ffmpeg never guesses a rate for headerless PCM
(measured). What it does is make the refusal `UNSUPPORTED_AUDIO_FORMAT` every
time. Without it, the code would be whichever of three codes ffmpeg's stderr
maps to, and only that one is a sentence a blind user can act on.

**An own note that has no file yet is not a download.** `message_run` sets
`MEDIA_PREPARING` while the recording is still being written, and
`MEDIA_SEND_FAILED` when the send failed before the audio reached the disk.
"The link may have expired" would be false for both, and "try again in a
moment" would be false for ever for the second.

**`temp_sweep` removes what a killed run left in `%TEMP%`**, at startup, for
names beginning with `temp_sweep.PREFIXES`. A new temporary with a new prefix
that is not added there is plaintext nobody ever collects. Its grace is a day
(newest of mtime and atime), because a run of a long recording outlasts an
hour and another account's live run must not be touched; a folder with a
locked file loses what it can and is tried again at the next start.

**A full disk while decrypting is `TEMP_NO_DISK_SPACE`**, not the downloads'
`NO_DISK_SPACE`. Nothing was being downloaded, and `%TEMP%` may be on another
drive. The sentence names the drive, and has a second wording for a `%TEMP%`
with no drive letter: `errors.error_i18n_key()` picks the wording and
`errors.error_i18n_values()` supplies `{drive}`.

**Afterwards focus returns to the message by id**
(`MessageTranscriptionFlow._focus_message()`), never by row: minutes of new
messages re-sort the list. Speech is queued *after* the focus move, because a
screen reader cancels speech when focus changes.

**Error sounds are guarded.** `Sound.play()` can raise when the output device
vanished (`docs/traps/audio-devices.md`). Unguarded, the sound before the
result window lost minutes of transcription to `sys.excepthook`
(`tests/test_transcription_flow.py::TestASoundThatRaisesCostsNothing`).

**The chat vault.** The auto-lock can close a locked chat while a run is in
the modal loop. Before anything of the result is shown or said, the flow asks
`MainWindow.is_chat_hidden_by_vault()` again. If the chat is hidden:

- no window opens, and no headline or note is said. "No speech was found" is
  about the recording too;
- the result is still stored when the chat's records hold the message, and
  one sentence says whether it was kept;
- a stored transcription does not open with the vault closed.

"Inserir na mensagem" asks again and is refused with its own sentence, or the
text would land in whichever conversation opens next
(`tests/test_transcription_chat_lock.py`).

## The voice filter

Without its voice-activity filter, Whisper answers the silence at the end of
a note with an invented sentence, in the same voice as the real ones, and a
listener cannot tell. Both backends transcribe without the filter rather than
fail when it cannot load (`faster_whisper_backend._run()`; whisper.cpp when
`VAD_MODEL` is missing or fails to load), and say so in
`TranscriptionResult.vad_used`.

**`vad_used is False` is always told to the user.** It is spoken, not only
shown, including on an empty result and on reopening a stored transcription
(`narration.result_notes()`, `client/core/transcription/stored.py`;
`tests/test_transcription_flow.py::TestTheVoiceFilterWarningIsAlwaysSpoken`).

faster-whisper's filter is onnxruntime, reached only from inside
faster-whisper. That is why `build.py` carries `--collect-all onnxruntime`:
without it the frozen build alone loses the filter, and every note then
carries that warning.

## Persistence

Read the docstring of `client/core/transcription/stored.py` before touching a
write path.

**Where it lives.** The transcription is stored under `_transcription` at the
**top level** of the message record, inside the Fernet-encrypted
`message_json`. It is never:

- a plaintext column;
- inside `message`, which a reply copies into `quotedMessage`;
- in the chat's `last_message_json` preview.

It is also popped from the body `get_base64_from_media()` posts to WPPConnect.

**It has two defences, because the server knows nothing of it.** Every resync
writes the server's copy over the record. So, as with the measured video
duration (`MEASURED_SECONDS_KEY`):

1. **The database inherits it** on every write path, under the same
   `_write_lock` hold as the write: `insert_message` through
   `_with_known_local_fields()`, and `insert_messages_batch` and
   `import_from_dict` through `_stored_rows_by_id()` + `_apply_stored_row()`
   on rows read ahead in blocks.
2. **Memory carries it over**: `carry_over_transcriptions()` in
   `sync_chat_messages()`, and again when a conversation is reopened from the
   database. Lose this one and the text survives on disk while the menu offers
   "Transcrever" again.

**Saving writes the key only, normally.** Both saving and deleting run through
`MainWindow._transcription_write_queue`:

- **Saving** goes `MainWindow.store_message_transcription()` →
  `DatabaseManager.set_message_transcription()` →
  `_rewrite_transcription()`, which `UPDATE`s the
  key alone under every JID the conversation's rows may be filed under,
  including the `@lid` twin. The record the UI holds has minutes-stale status
  and reactions, and `INSERT OR REPLACE` would undo them. **Only when no row
  exists yet** (a message that arrived live and has not been persisted) is a
  snapshot of the record written whole through `insert_message()`. Its rule
  keeps whichever decision is later.
- **The database is `secure_delete=ON`**: a deleted or overwritten text page
  is zeroed in the main file. The `-wal` file is not covered: until a
  checkpoint it can still hold the old page, so a delete is not a promise that
  the text is gone from the disk at once.
- **Deleting writes a dated tombstone** instead of removing the key. A removed
  key lets any stale copy still holding the text win it back.
- **`at` only moves forward** (`next_decision_time()`).
- **The queue has exactly one thread.** Arrival order is what stops a save
  queued behind a sync from landing after a delete the user already heard
  confirmed.

**Merges keep it.** The `@lid`→phone merge folds the twin's transcription into
the survivor before dropping it: `fold_transcription()`, called from
`merge_or_rename_chat()`, `_merge_lid_into_phone()` and `deduplicate_chats()`.

**Some messages never keep one:**

- an own message still without its real id;
- a message deleted for everyone, which neither keeps one nor accepts one.

A full resync (F5) or a logout deliberately wipes them, as it wipes the voice
notes they came from.

## The log

The issue forbids the log to hold transcribed text, the contact, the number or
the message id. A media file's name *is* the message id, and it arrives inside
exception text.

- **`errors.scrub_media_names()` replaces that name and nothing else.**
- **`errors.exception_report()` stands in for `logging.exception()`**
  throughout the package. It gives the type, scrubbed text and frames for the
  whole chain, walked the way `traceback` walks it: `__cause__`, then
  `__context__` unless `__suppress_context__` is set. That matters because
  `message_audio.py` raises `from None` precisely to leave the exception
  carrying the path behind.
- **A file path is logged as its basename.** The folders above it carry the
  Windows user name (and `%TEMP%` paths, the media's own); a log line that
  needs to name a file says `os.path.basename()` of it.
- **Folders, error codes, DLL names and model ids stay.** They are what a
  failure is diagnosed with, and the beta's log is not to be trimmed.
- **`TranscriptionError.__str__` returns the code alone.** The detail travels
  in `log_line`, because `wx.MessageBox(str(exc), …)` is an idiom in this repo
  and would read a path aloud.

`tests/test_transcription_backend.py` walks the package's logging calls.

## What is verified before a run

**The whisper.cpp program is hashed on every launch; the models are not.**
`whisper_cpp_runtime.verify_executable()` reads every file the manifest lists
(once per process, cached on path, size and mtime) before whisper-cli starts,
because that code runs with the user's audio and the folder is writable by
anything running as the user. The manifest sits in that folder too, so it
cannot vouch for itself: the digests of the executable and every DLL are
pinned in `whisper_cpp_builds` (read off the pinned zips), and a manifest that
disagrees or omits one is refused. Models are data, not code: a swapped one
can give wrong text but not run anything, and hashing gigabytes at every
transcription would cost far more than it protects, so they are checked at
download and by the explicit verify. Accepted limit: a swap that restores size
and mtime after a file has passed is not seen again in that process.

## Coexistence with online AI transcription

The online AI (`client/core/ai_media/`,
`client/ui/conversation_panel/ai_actions.py`, `Ctrl+Shift+I`) also
transcribes, and it sends the audio to a third party. Picking the wrong one is
a privacy mistake, so the two are told apart by name alone, in every locale:

- the local feature says "local" / "on this computer";
- the online one says "online AI";
- every sentence that names a tab uses that tab's current name.

`tests/test_transcription_local_vs_online_names.py` checks this. The online AI
goes through `tls_trust` too, and reads `_app_settings` with the underscore.

## The window's language

The interface language is used in two places:

- With detection off and the language left at "interface language", the run
  is *forced* to it (`preferences._resolve_language()`).
- `preferences.preferred_language()` uses it to say "this one was transcribed
  as X".

Both take it from `main_window.i18n.language` (the flow passes
`ui_language`): the language this window shows, not the setting on disk. The
language setting is install-wide. Another account can change it, and the
window applies the change only once it is inactive. The window's own `I18n`
reads the settings only until it has read them once (`client/core/i18n.py`).
Read it the same way. Reading `settings["general"]["language"]` directly would
force a run into a language the window does not show yet.
