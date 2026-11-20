"""Message translation without an online AI (issue #798).

The AI providers live in ``core/message_translation.py``; this package adds
the engines that are not a language model:

* ``languages`` — the languages offered, by endonym, and each engine's code
  for them;
* ``local_catalog`` — the offline models (CTranslate2 conversions of NLLB-200,
  M2M-100, mBART-50, SMaLL-100 and MADLAD-400), pinned like the Whisper catalogue;
* ``local_store`` / ``local_text`` — their folder, download
  and removal (through the transcription model store) and what the settings
  say about them;
* ``local_engine`` — loading a model and translating with it, in-process;
* ``services`` — translation services with a REST API (LibreTranslate,
  DeepL, Google Cloud Translation, Microsoft Translator) and the free Google
  Translate web endpoint ``core/ai_media/gtranslate.py`` already uses.

No wx here, and no top-level import of ctranslate2, tokenizers, sentencepiece
or py3langid: a copy where one is missing must still open
(``local_engine.missing_components()`` says which).
"""
