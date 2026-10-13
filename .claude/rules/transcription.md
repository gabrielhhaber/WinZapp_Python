---
paths:
  - "client/core/transcription/**"
  - "client/ui/transcription_flow.py"
  - "client/ui/dialogs/transcription_*.py"
  - "client/main_window/transcription_store.py"
  - "client/main_window/transcription_background.py"
  - "client/main_window/auto_transcription.py"
  - "client/ui/conversation_panel/bulk_transcription.py"
  - "client/ui/conversation_panel/accelerators.py"
  - "client/ui/conversation_panel/message_accels.py"
  - "client/ui/conversation_panel/message_menu.py"
  - "client/core/tls_trust.py"
  - "client/core/database.py"
  - "tests/test_transcription_*.py"
---

# Local transcription

**Read `docs/traps/transcription.md` before changing these files.** Short form:

No top-level import of the backend or wx in `client/core/transcription/`: a copy without it must still open (whisper.cpp, or `BACKEND_MISSING`). No int8 on a card unless chosen, and never from sm_120 up or on an unknown capability. whisper.cpp's CUDA build only for [5.0, 12.0), and whisper-cli always gets `-l`. Catalogues pin revision, sizes and sha256. A single-language model forces its language, and the automatic choice never downloads a third-party model or a 32-bit GGML file. Resolve an unsupported precision before the load, because CTranslate2 refuses it. Probe free memory right before the run, off the wx thread. The models folder is install-wide: `coord_locks.models_lock()`, `canonical_dir()`, `_app_settings` with the underscore. A user's own model is never copied or deleted. Downloads go through `tls_trust`. `vad_used is False` is always spoken. The stored text lives in the encrypted record and needs both defences. Never log the text, the contact or the message id; a file path is logged as its basename. New temp prefixes go into `temp_sweep.PREFIXES`. A status's text lives in the status's own record (`core/transcription/statuses.py`) and is carried over whenever the tab's list is rebuilt.
