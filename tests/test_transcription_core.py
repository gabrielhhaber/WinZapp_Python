"""The pure core of local (Whisper) transcription: catalogue, device, errors.

Everything here is a decision that is impossible to observe on the machine that
made it, which is why the decisions were written as functions over data in the
first place:

* **The catalogue is a contract with Hugging Face.** The file list is not the
  same for every repository — the Systran tiny..medium conversions ship
  ``vocabulary.txt`` and no ``preprocessor_config.json``, large-v3 and the turbo
  conversion ship ``vocabulary.json`` *and* ``preprocessor_config.json``. A
  download built on one assumed shape leaves a model that looks complete and
  that CTranslate2 refuses to load, which the user experiences as "it just says
  internal error".

* **sm_120 must never be given int8.** The CTranslate2 builds that support
  Blackwell (>= 4.6.3, CUDA 12.8) are compiled with INT8 disabled for it, so
  every int8 variant fails at model load on the reporter's own card. That is a
  property of a machine no test runner has, so it is pinned as a property of
  ``select_compute_type()`` instead.

* **Asking for CUDA on a machine without it is not an error.** It falls back to
  the CPU with a reason code the UI announces. A raised exception here would
  replace a slower transcription with nothing at all. And the reason has to come
  from ``cuda_probe_error``, never from the ``driver_error`` aggregate: the
  aggregate is non-empty on every machine that has not installed the backend
  yet, so reading it as a driver fault told users with no NVIDIA hardware at all
  to go and reinstall a graphics driver.

* **Counting CUDA devices never proved a transcription could run on one.**
  `ctranslate2.get_cuda_device_count()` answers with the NVIDIA driver alone,
  while the run also opens cuBLAS by name — a library no package in
  requirements.txt ships. So on every machine with a driver and no CUDA
  Toolkit, which is nearly every machine that installs a release, the old
  decision chose "cuda" and the model load died with "Could not load library
  cublas64_12.dll"; on a developer's machine, which has the Toolkit, it worked.
  The libraries are therefore checked as well, and the reason says *that*
  rather than "no card was found", which is false and sends the user looking
  in the wrong place.

* **Memory is planned against what is free, not what is installed.** A 16 GB
  machine running Chromium, WhatsApp Web and a screen reader is a 5 GB machine
  for this purpose; deciding on the 16 buys a 3 GB download and then an
  allocation error mid-transcription. Pinned on both devices, symmetrically.

* **A code with no translation is spoken as the code.** ``I18n.t()`` is
  ``translations.get(key, key)``, so a failure mode nobody translated has NVDA
  read "transcription_error_ffmpeg_failed" out loud. The last two tests walk
  every code there is, in every locale.
"""

import ast
import dataclasses
import inspect
import json
import os
import sys
import threading
import types

from tests.locales import load_strings, registered_locale_codes
import pytest

from core.transcription import device, errors, model_catalog


LOCALES = registered_locale_codes()

# Repository, pinned revision, model.bin digest and the exact size of every
# file, as read from the Hugging Face API on 2026-09-03 — restated here rather
# than imported, so the catalogue is checked against the observation instead of
# against itself. Every size is a constant because the revision is pinned, and
# that is what lets "the wrong size" be a detectable state for all five files
# rather than only for the weights.
_EXPECTED_REPOS = {
    "tiny": (
        "Systran/faster-whisper-tiny",
        "d90ca5fe260221311c53c58e660288d3deb8d356",
        "dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1",
        {"config.json": 2_249, "model.bin": 75_538_270,
         "tokenizer.json": 2_203_239, "vocabulary.txt": 459_861},
    ),
    "base": (
        "Systran/faster-whisper-base",
        "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
        "d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9",
        {"config.json": 2_309, "model.bin": 145_217_532,
         "tokenizer.json": 2_203_239, "vocabulary.txt": 459_861},
    ),
    "small": (
        "Systran/faster-whisper-small",
        "536b0662742c02347bc0e980a01041f333bce120",
        "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
        {"config.json": 2_370, "model.bin": 483_546_902,
         "tokenizer.json": 2_203_239, "vocabulary.txt": 459_861},
    ),
    "medium": (
        "Systran/faster-whisper-medium",
        "08e178d48790749d25932bbc082711ddcfdfbc4f",
        "9b45e1009dcc4ab601eff815b61d80e60ce3fd8c74c1a14f4a282258286b51ae",
        {"config.json": 2_257, "model.bin": 1_527_906_378,
         "tokenizer.json": 2_203_239, "vocabulary.txt": 459_861},
    ),
    "large-v3": (
        "Systran/faster-whisper-large-v3",
        "edaa852ec7e145841d8ffdb056a99866b5f0a478",
        "69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1",
        {"config.json": 2_394, "model.bin": 3_087_284_237,
         "preprocessor_config.json": 340, "tokenizer.json": 2_480_617,
         "vocabulary.json": 1_068_114},
    ),
    "large-v3-turbo": (
        "deepdml/faster-whisper-large-v3-turbo-ct2",
        "4df90f75321148c3a29a9e2351b7ddf8f5b115a8",
        "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da",
        {"config.json": 2_263, "model.bin": 1_617_884_929,
         "preprocessor_config.json": 340, "tokenizer.json": 2_710_337,
         "vocabulary.json": 1_068_114},
    ),
    # Read from the Hugging Face API on 2026-10-05, at the pinned revision and
    # on main, twice, identical: the English-only and the other official
    # conversions, then the third-party fine-tunes (blob by blob, the `tree`
    # listing having swapped KBLab's rows).
    "tiny.en": (
        "Systran/faster-whisper-tiny.en",
        "0d3d19a32d3338f10357c0889762bd8d64bbdeba",
        "1a5afae06a4db91c975c9a9d78be5cc110ee4ea022ad57d55492e4550e936b2a",
        {"config.json": 2_317, "model.bin": 75_537_502,
         "tokenizer.json": 2_128_466, "vocabulary.txt": 422_309},
    ),
    "base.en": (
        "Systran/faster-whisper-base.en",
        "3d3d5dee26484f91867d81cb899cfcf72b96be6c",
        "2a166925539a16005f14ff328359f9b9adb9dc4fb631bb3b227526862e93e2ef",
        {"config.json": 2_227, "model.bin": 145_216_508,
         "tokenizer.json": 2_128_466, "vocabulary.txt": 422_309},
    ),
    "small.en": (
        "Systran/faster-whisper-small.en",
        "d1d751a5f8271d482d14ca55d9e2deeebbae577f",
        "62b2a45b05ee59acb4a5341b33ee35e041395d378d418a18acfe4c9e768ee37a",
        {"config.json": 2_657, "model.bin": 483_545_366,
         "tokenizer.json": 2_128_466, "vocabulary.txt": 422_309},
    ),
    "medium.en": (
        "Systran/faster-whisper-medium.en",
        "a29b04bd15381511a9af671baec01072039215e3",
        "11b220779aea4c6f3ce9d2549c8a95ea869ed84066864b999531ef53e594fe5b",
        {"config.json": 2_643, "model.bin": 1_527_904_330,
         "tokenizer.json": 2_128_466, "vocabulary.txt": 422_309},
    ),
    "large-v1": (
        "Systran/faster-whisper-large-v1",
        "b07c8d4be0be90092aa01a29c975077acb8d15c9",
        "a3cce8081a5414206ab09a80aa410ebf9965feef52adafeead13f4a83398b1d1",
        {"config.json": 2_352, "model.bin": 3_086_912_962,
         "tokenizer.json": 2_203_239, "vocabulary.txt": 459_861},
    ),
    "large-v2": (
        "Systran/faster-whisper-large-v2",
        "f0fe81560cb8b68660e564f55dd99207059c092e",
        "bf2a9746382e1aa7ffff6b3a0d137ed9edbd9670c3b87e5d35f5e85e70d0333a",
        {"config.json": 2_796, "model.bin": 3_086_912_962,
         "tokenizer.json": 2_203_239, "vocabulary.txt": 459_861},
    ),
    "distil-small.en": (
        "Systran/faster-distil-whisper-small.en",
        "ef77d90526ccd62cde3808ee70626a01e5cf83e4",
        "1187de3982cdcf962a2fb8f797e429fb4651b875b18fe9ce50b58b52fc9072b7",
        {"config.json": 2_812, "model.bin": 332_308_257,
         "preprocessor_config.json": 339, "tokenizer.json": 2_405_466,
         "vocabulary.json": 825_480},
    ),
    "distil-medium.en": (
        "Systran/faster-distil-whisper-medium.en",
        "80ddfce281f77766d8943d63109199fc8145dfa5",
        "d4cb75d823dcd2647191064da76f026774c06c036908f38456165368d0e2d66a",
        {"config.json": 2_574, "model.bin": 788_826_555,
         "preprocessor_config.json": 339, "tokenizer.json": 2_405_678,
         "vocabulary.json": 825_480},
    ),
    "distil-large-v3": (
        "Systran/faster-distil-whisper-large-v3",
        "c3058b475261292e64a0412df1d2681c06260fab",
        "b79368e19b6623813609431a6e5ee309a71506701ebc49fd7820e692dec7c5f5",
        {"config.json": 2_690, "model.bin": 1_512_927_867,
         "preprocessor_config.json": 340, "tokenizer.json": 2_480_617,
         "vocabulary.json": 1_068_114},
    ),
    "distil-large-v3.5": (
        "distil-whisper/distil-large-v3.5-ct2",
        "9793ccc07920e0f830e1dba0343efcdf0ef8c903",
        "c58b88b8585ffcd2135fddaaf421ce72cb223b32edea70d156aed1dea319a119",
        {"config.json": 2_690, "model.bin": 1_512_927_867,
         "preprocessor_config.json": 340, "tokenizer.json": 2_480_645,
         "vocabulary.json": 1_068_114},
    ),
    "distil-large-v2": (
        "Systran/faster-distil-whisper-large-v2",
        "fe9b404fc56de3f7c38606ef9ba6fd83526d05e4",
        "b415593cd109c0418d42ecb106c40ae45c8229cb4090e67db12243901e071ab3",
        {"config.json": 2_657, "model.bin": 1_512_556_667,
         "preprocessor_config.json": 339, "tokenizer.json": 2_481_187,
         "vocabulary.json": 1_068_103},
    ),
    "kb-whisper-tiny": (
        "KBLab/kb-whisper-tiny",
        "76d796af43a50fa34321efa562c9b9887a187463",
        "6edbc6036ceb79f12c30c5c5c2290383eac7a0bfec0be3163c480e79b26b16b9",
        {"config.json": 3_562, "model.bin": 75_538_384,
         "preprocessor_config.json": 339, "tokenizer.json": 3_931_232,
         "vocabulary.json": 1_068_103},
    ),
    "kb-whisper-base": (
        "KBLab/kb-whisper-base",
        "1499d2d2f0c7ed545bd6f2eec85287cf8d8c8b38",
        "fa942ec92ad7747aec2e9ea8c57ad8971a3695f3c9ff440018a3667bb818a5c4",
        {"config.json": 3_622, "model.bin": 145_217_646,
         "preprocessor_config.json": 339, "tokenizer.json": 3_931_232,
         "vocabulary.json": 1_068_103},
    ),
    "kb-whisper-small": (
        "KBLab/kb-whisper-small",
        "3564d61a42fc210ceaa55a22a96dd64478959c78",
        "58bf16e6878108f898c4db7983d0f4ec01c4891500f7a2b3a2c9ce98b3c0029d",
        {"config.json": 3_690, "model.bin": 483_547_016,
         "preprocessor_config.json": 339, "tokenizer.json": 3_931_232,
         "vocabulary.json": 1_068_103},
    ),
    "kb-whisper-medium": (
        "KBLab/kb-whisper-medium",
        "0abe10b9d7f75d0902656e5c06c5c4d549604dc5",
        "4a4a32952026bcfa0bcfaa76b0b006f232ffba6a8f5bccbcb12e4a1153a40494",
        {"config.json": 3_592, "model.bin": 1_527_906_492,
         "preprocessor_config.json": 339, "tokenizer.json": 3_931_232,
         "vocabulary.json": 1_068_103},
    ),
    "kb-whisper-large": (
        "KBLab/kb-whisper-large",
        "d5d5984b4d8f7c4847a8ea203f1976285fb28300",
        "69ed56887f68417f651d50fd5f225c60dfc0f9515bef85bb0f1404825c6f01de",
        {"config.json": 3_717, "model.bin": 3_087_284_276,
         "preprocessor_config.json": 340, "tokenizer.json": 3_931_383,
         "vocabulary.json": 1_068_114},
    ),
    "ivrit-large-v3": (
        "ivrit-ai/whisper-large-v3-ct2",
        "e9ed4a4a98d761b0f617d668303de2c514236c66",
        "765965efd777190f76e8b337520056f00d68e48bb99b2f95d28670b2364a02ce",
        {"config.json": 1_536, "model.bin": 3_087_284_276,
         "preprocessor_config.json": 340, "tokenizer.json": 2_480_617,
         "vocabulary.json": 1_068_114},
    ),
    "ivrit-large-v3-turbo": (
        "ivrit-ai/whisper-large-v3-turbo-ct2",
        "72ad623a37947395efcc3933132353790e5a12f5",
        "db2a2265aa012c16c7db9edda3d699c99f984efdd3f2e22a72a8ce7e9720f3a2",
        {"config.json": 1_405, "model.bin": 1_617_884_968,
         "preprocessor_config.json": 357, "tokenizer.json": 2_710_337,
         "vocabulary.json": 1_068_114},
    ),
    "ivrit-yi-large-v3": (
        "ivrit-ai/yi-whisper-large-v3-ct2",
        "58ad8942662665e762008c268ade5022e8dcd198",
        "1d3afba86e7977617945f39be197b6eb15f24960d621be5a1743ad639f7f476d",
        {"config.json": 1_536, "model.bin": 3_087_284_237,
         "preprocessor_config.json": 340, "tokenizer.json": 2_480_617,
         "vocabulary.json": 1_068_114},
    ),
    "ivrit-yi-large-v3-turbo": (
        "ivrit-ai/yi-whisper-large-v3-turbo-ct2",
        "cddb73c5ea83e4354a3926682a59c3b063f5a98e",
        "cf157e277a11895a44bdf6479c324408441613ae9b87871d69cb0a9bc01df835",
        {"config.json": 1_405, "model.bin": 1_617_884_929,
         "preprocessor_config.json": 357, "tokenizer.json": 2_710_337,
         "vocabulary.json": 1_068_114},
    ),
    "kotoba-whisper-v2.0": (
        "kotoba-tech/kotoba-whisper-v2.0-faster",
        "f44edd35eaeb2274e85ac7b31fb2c6f59ff1c4bc",
        "60d2bc2e33de9d43f2745be09caefe1161acab670f6796d4a750d8d848382b36",
        {"config.json": 2_394, "model.bin": 1_512_927_867,
         "preprocessor_config.json": 340, "tokenizer.json": 2_481_381,
         "vocabulary.json": 1_068_114},
    ),
}


@pytest.fixture(autouse=True)
def _no_memoized_cuda_answer_between_tests(monkeypatch):
    """The memo is module state, so it is reset for every test in this file.

    Two classes reset it for themselves, which is not the same thing: on a
    machine that actually has cuBLAS, a test that only means to probe the
    hardware populates it on the way past, and the next test inherits an answer
    nobody in it asked for.
    """
    monkeypatch.setattr(device, "_cuda_library_answer", None)
    monkeypatch.setattr(device, "_cuda_library_generation", 0)


def _probe(**kwargs):
    """A HardwareProbe with everything unknown unless the test says otherwise."""
    return device.HardwareProbe(**kwargs)


class _FakeLibrary:
    """What ctypes.WinDLL returns, as far as this module is concerned."""

    def __init__(self, name):
        self.name = name
        self._handle = 0x7FFFFFFFFFFF


def _cuda_probe(capability=(8, 6), free_vram_mb=8192, **kwargs):
    kwargs.setdefault("total_vram_mb", free_vram_mb)
    return device.HardwareProbe(
        cuda_available=True,
        cuda_device_count=1,
        compute_capability=capability,
        free_vram_mb=free_vram_mb,
        **kwargs,
    )


class TestModelCatalog:
    def test_ids_are_unique(self):
        ids = [m.id for m in model_catalog.MODELS]
        assert sorted(ids) == sorted(set(ids))

    def test_catalogue_is_exactly_the_verified_set(self):
        assert {m.id for m in model_catalog.MODELS} == set(_EXPECTED_REPOS)

    @pytest.mark.parametrize("model_id", sorted(_EXPECTED_REPOS))
    def test_repo_and_files_match_hugging_face(self, model_id):
        repo, _revision, _digest, files = _EXPECTED_REPOS[model_id]
        model = model_catalog.get_model(model_id)
        assert model.repo == repo
        assert dict(model.files) == files
        assert model.model_bin_bytes == files["model.bin"]

    @pytest.mark.parametrize("model_id", sorted(_EXPECTED_REPOS))
    def test_the_totals_are_the_exact_sum_of_the_files(self, model_id):
        # The totals used to add a guessed couple of megabytes for the
        # auxiliary files, and on the turbo conversion that guess was 181 KB
        # UNDER the real figure — the wrong direction for a number that feeds a
        # "does this fit?" gate and a free-space check.
        _repo, _revision, _digest, files = _EXPECTED_REPOS[model_id]
        model = model_catalog.get_model(model_id)
        assert model.download_bytes == sum(files.values())
        assert model.disk_bytes == model.download_bytes

    @pytest.mark.parametrize("model_id", sorted(_EXPECTED_REPOS))
    def test_every_model_carries_the_verified_weights_digest(self, model_id):
        # Pinned revision plus digest is what makes MODEL_CORRUPTED provable
        # for the one file where a silent corruption costs a multi-gigabyte
        # re-download to discover.
        _repo, _revision, digest, _files = _EXPECTED_REPOS[model_id]
        model = model_catalog.get_model(model_id)
        assert model.model_bin_sha256 == digest
        assert len(model.model_bin_sha256) == 64
        assert set(model.model_bin_sha256) <= set("0123456789abcdef")

    @pytest.mark.parametrize("model_id", sorted(_EXPECTED_REPOS))
    def test_every_model_is_pinned_to_the_verified_revision(self, model_id):
        # Downloads resolve by sha, not by /main/: a revision published upstream
        # must not silently replace weights an install already verified, nor
        # invalidate the byte counts above.
        _repo, revision, _digest, _files = _EXPECTED_REPOS[model_id]
        model = model_catalog.get_model(model_id)
        assert model.revision == revision
        assert len(model.revision) == 40
        assert set(model.revision) <= set("0123456789abcdef")

    def test_every_file_can_be_checked_by_size_not_just_the_weights(self):
        # errors.MODEL_CORRUPTED promises "missing or the wrong size", which is
        # only true if every file has an exact expected size: a truncated
        # tokenizer.json is as plausible-looking as a truncated model.bin, and
        # fails the load just the same.
        for model in model_catalog.MODELS:
            sizes = dict(model.files)
            assert len(sizes) == len(model.files), "duplicate file name"
            for name, size in sizes.items():
                assert isinstance(size, int) and size > 0, name

    def test_a_truncated_file_changes_the_expected_total(self):
        medium = dict(model_catalog.get_model("medium").files)
        for damaged_file in ("model.bin", "tokenizer.json", "config.json"):
            damaged = dict(medium)
            damaged[damaged_file] -= 1
            assert sum(damaged.values()) != sum(medium.values())

    def test_the_two_file_layouts_are_not_confused(self):
        # The regression this guards: one shared file list for all six repos.
        assert "vocabulary.txt" in dict(model_catalog.get_model("small").files)
        assert "vocabulary.json" in dict(model_catalog.get_model("large-v3").files)
        assert "preprocessor_config.json" not in dict(
            model_catalog.get_model("medium").files
        )
        assert "preprocessor_config.json" in dict(
            model_catalog.get_model("large-v3-turbo").files
        )

    def test_every_model_ships_the_weights_and_a_tokenizer(self):
        for model in model_catalog.MODELS:
            names = dict(model.files)
            assert "model.bin" in names
            assert "tokenizer.json" in names
            assert "config.json" in names

    def test_the_file_table_cannot_be_edited_in_place(self):
        # A dict here would let one caller rewrite the expected sizes for every
        # other caller in the process; a tuple of pairs cannot be.
        assert isinstance(model_catalog.MODELS[0].files, tuple)
        for entry in model_catalog.MODELS[0].files:
            assert isinstance(entry, tuple) and len(entry) == 2

    def test_list_models_is_ordered_by_size_class_then_language_then_bytes(self):
        # Within a size class: multilingual first, then the official
        # single-language models, then the third-party ones — a tiny.en is a
        # few hundred kilobytes smaller than tiny and must not be the first
        # line a user arrowing through the list hears.
        listed = model_catalog.list_models()
        assert len(listed) == len(model_catalog.MODELS)
        keys = [
            (model_catalog.SIZE_CLASSES.index(m.size_class),
             2 if m.third_party else 1 if m.language else 0, m.download_bytes)
            for m in listed
        ]
        assert keys == sorted(keys)

    def test_the_cheapest_model_comes_first_and_the_costliest_multilingual_last(self):
        listed = model_catalog.list_models()
        assert listed[0].id == "tiny"
        assert [m.id for m in listed if m.language is None][-1] == "large-v3"

    def test_memory_requirements_never_decrease_with_size_class(self):
        # Within a class the single-language models follow the multilingual
        # ones whatever their size, so the rule is between classes.
        for field in ("min_vram_mb", "min_ram_mb"):
            by_class = [
                [getattr(m, field) for m in model_catalog.MODELS if m.size_class == size]
                for size in model_catalog.SIZE_CLASSES
            ]
            for smaller, larger in zip(by_class, by_class[1:]):
                assert max(smaller) <= min(larger), field

    def test_every_single_language_model_says_which_language(self):
        english = {"tiny.en", "base.en", "small.en", "medium.en", "distil-small.en",
                   "distil-medium.en", "distil-large-v2", "distil-large-v3",
                   "distil-large-v3.5"}
        third_party = {
            "kb-whisper-tiny": ("sv", "KBLab"), "kb-whisper-base": ("sv", "KBLab"),
            "kb-whisper-small": ("sv", "KBLab"), "kb-whisper-medium": ("sv", "KBLab"),
            "kb-whisper-large": ("sv", "KBLab"),
            "ivrit-large-v3": ("he", "ivrit.ai"), "ivrit-large-v3-turbo": ("he", "ivrit.ai"),
            "ivrit-yi-large-v3": ("yi", "ivrit.ai"),
            "ivrit-yi-large-v3-turbo": ("yi", "ivrit.ai"),
            "kotoba-whisper-v2.0": ("ja", "Kotoba Technologies"),
        }
        for model in model_catalog.MODELS:
            if model.id in third_party:
                assert (model.language, model.publisher) == third_party[model.id]
                assert model.origin == model_catalog.ORIGIN_THIRD_PARTY and model.third_party
            elif model.id in english:
                assert model.language == "en" and model.english_only, model.id
                assert (model.origin, model.publisher) == (model_catalog.ORIGIN_OFFICIAL, "")
            else:
                assert model.language is None and not model.third_party, model.id

    def test_the_bilingual_kotoba_conversion_is_not_offered(self):
        # Its repository has no tokenizer.json, and faster-whisper would fall
        # back to openai/whisper-tiny's pre-v3 vocabulary for a v3 model.
        assert not [m for m in model_catalog.MODELS if "bilingual" in m.repo]

    def test_no_two_models_share_a_file_table(self):
        # Identifying a folder by its sizes (external_models.identify_quick())
        # picks the first match: two identical tables would hash the wrong one.
        tables = [tuple(sorted(m.files)) for m in model_catalog.MODELS]
        assert len(tables) == len(set(tables))

    def test_every_size_class_is_one_of_the_declared_ones(self):
        for model in model_catalog.MODELS:
            assert model.size_class in model_catalog.SIZE_CLASSES

    def test_unknown_id_returns_none_rather_than_raising(self):
        # It comes from settings, where a model removed between versions is a
        # normal state and must not crash the conversation window.
        assert model_catalog.get_model("large-v4") is None
        assert model_catalog.get_model(None) is None
        assert model_catalog.get_model("") is None

    def test_total_bytes_matches_the_entry(self):
        model = model_catalog.get_model("base")
        assert model_catalog.total_bytes("base") == model.download_bytes

    def test_total_bytes_of_an_unknown_id_is_none_not_zero(self):
        # 0 would be a trap: a settings file naming a model some later version
        # dropped would tell the free-space gate "it fits" and start a download
        # of something that does not exist.
        assert model_catalog.total_bytes("large-v4") is None
        assert model_catalog.total_bytes(None) is None

    def test_models_are_frozen(self):
        # The catalogue is shared module state; a caller adjusting one entry
        # would change it for every other caller in the process.
        with pytest.raises(dataclasses.FrozenInstanceError):
            model_catalog.MODELS[0].disk_bytes = 1


class TestResolveDevice:
    def test_cpu_preference_is_honoured_even_with_a_gpu_present(self):
        chosen, reason = device.resolve_device(device.PREFERENCE_CPU, _cuda_probe())
        assert chosen == device.DEVICE_CPU
        assert reason == device.REASON_CPU_REQUESTED

    def test_auto_takes_the_gpu_when_there_is_one(self):
        chosen, reason = device.resolve_device(device.PREFERENCE_AUTO, _cuda_probe())
        assert chosen == device.DEVICE_CUDA
        assert reason == device.REASON_CUDA_SELECTED

    def test_auto_falls_back_to_the_cpu_quietly(self):
        chosen, reason = device.resolve_device(device.PREFERENCE_AUTO, _probe())
        assert chosen == device.DEVICE_CPU
        assert reason == device.REASON_NO_CUDA_FOUND

    def test_cuda_preference_takes_the_gpu(self):
        chosen, reason = device.resolve_device(device.PREFERENCE_CUDA, _cuda_probe())
        assert chosen == device.DEVICE_CUDA
        assert reason == device.REASON_CUDA_SELECTED

    def test_cuda_without_cuda_is_a_fallback_not_an_exception(self):
        chosen, reason = device.resolve_device(device.PREFERENCE_CUDA, _probe())
        assert chosen == device.DEVICE_CPU
        assert reason == device.REASON_CUDA_UNAVAILABLE

    def test_cuda_with_a_driver_failure_says_so_specifically(self):
        # This state does not come out of probe_hardware(), and re-probing
        # cannot produce it either — a fresh probe would count the device again
        # and land on CUDA_SELECTED. It takes a caller that demotes the card
        # deliberately after a run failed on it, i.e. dataclasses.replace(
        # probe, cuda_available=False, cuda_device_count=0, cuda_probe_error=…),
        # which is what is built by hand here.
        probe = _probe(cuda_probe_error="nvml: could not load nvml.dll",
                       driver_error="nvml: could not load nvml.dll")
        chosen, reason = device.resolve_device(device.PREFERENCE_CUDA, probe)
        assert chosen == device.DEVICE_CPU
        assert reason == device.REASON_CUDA_DRIVER_ERROR

    def test_an_unrelated_probe_failure_is_not_a_driver_fault(self):
        # driver_error is an aggregate of everything the probe could not do —
        # the Windows memory API here. Reading it as "the graphics driver
        # failed" sends the user off to reinstall a driver they may not have.
        probe = _probe(driver_error="ram: GlobalMemoryStatusEx failed")
        chosen, reason = device.resolve_device(device.PREFERENCE_CUDA, probe)
        assert chosen == device.DEVICE_CPU
        assert reason == device.REASON_CUDA_UNAVAILABLE

    def test_a_missing_backend_is_not_a_driver_fault(self):
        # The state of EVERY install until the backend is installed: the
        # aggregate carries the failed ctranslate2 import and nothing else.
        probe = _probe(driver_error="ctranslate2: No module named ctranslate2")
        _, reason = device.resolve_device(device.PREFERENCE_CUDA, probe)
        assert reason == device.REASON_CUDA_UNAVAILABLE
        _, auto_reason = device.resolve_device(device.PREFERENCE_AUTO, probe)
        assert auto_reason == device.REASON_NO_CUDA_FOUND

    def test_a_driver_error_alongside_a_working_gpu_does_not_block_it(self):
        # NVML failing only costs us the VRAM figure; CTranslate2 can still run.
        probe = _cuda_probe(capability=None, free_vram_mb=None, total_vram_mb=None,
                            driver_error="nvml: nvmlInit_v2 failed")
        chosen, _ = device.resolve_device(device.PREFERENCE_AUTO, probe)
        assert chosen == device.DEVICE_CUDA

    def test_a_counted_gpu_with_no_cuda_libraries_is_not_chosen(self):
        # The measured bug: the driver counts the card, cuBLAS is nowhere, and
        # the model load is what discovers it — after the user has waited.
        probe = _cuda_probe(cuda_libraries_ok=False,
                            missing_cuda_libraries=("cublas64_12.dll",))
        chosen, reason = device.resolve_device(device.PREFERENCE_AUTO, probe)
        assert chosen == device.DEVICE_CPU
        assert reason == device.REASON_CUDA_LIBRARIES_MISSING

    def test_the_missing_libraries_reason_is_given_under_both_preferences(self):
        # Unlike the no-GPU pair, this one is announced under "auto" too: the
        # user has a usable card and is one download away from using it.
        probe = _cuda_probe(cuda_libraries_ok=False)
        for preference in (device.PREFERENCE_AUTO, device.PREFERENCE_CUDA):
            assert device.resolve_device(preference, probe)[1] == (
                device.REASON_CUDA_LIBRARIES_MISSING
            )

    def test_asking_for_the_cpu_still_wins_over_a_library_fault(self):
        probe = _cuda_probe(cuda_libraries_ok=False)
        chosen, reason = device.resolve_device(device.PREFERENCE_CPU, probe)
        assert chosen == device.DEVICE_CPU
        assert reason == device.REASON_CPU_REQUESTED

    def test_a_machine_the_check_could_not_run_on_still_gets_the_gpu(self):
        # None is "not asked", never "no". Inventing an obstacle nobody
        # observed would cost the GPU to every machine the check cannot reach.
        probe = _cuda_probe(cuda_libraries_ok=None)
        assert device.resolve_device(device.PREFERENCE_AUTO, probe)[0] == (
            device.DEVICE_CUDA
        )

    def test_without_a_card_a_library_fault_is_not_the_reason(self):
        # No device was counted, so the libraries are missing for an
        # uninteresting reason; saying so would send a user with no NVIDIA
        # hardware off to download a CUDA runtime they can never use.
        probe = _probe(cuda_libraries_ok=False)
        assert device.resolve_device(device.PREFERENCE_AUTO, probe)[1] == (
            device.REASON_NO_CUDA_FOUND
        )
        assert device.resolve_device(device.PREFERENCE_CUDA, probe)[1] == (
            device.REASON_CUDA_UNAVAILABLE
        )

    def test_a_zero_device_count_is_not_cuda(self):
        # ctranslate2 loaded but found nothing — "available" alone is not enough.
        probe = device.HardwareProbe(cuda_available=True, cuda_device_count=0)
        chosen, _ = device.resolve_device(device.PREFERENCE_AUTO, probe)
        assert chosen == device.DEVICE_CPU

    @pytest.mark.parametrize("preference", [None, "", "gpu", "gpu ", 3])
    def test_an_unrecognised_preference_behaves_like_auto(self, preference):
        # It is a settings value; a file from another version must not be able
        # to make transcription unavailable.
        assert device.resolve_device(preference, _cuda_probe())[0] == device.DEVICE_CUDA
        assert device.resolve_device(preference, _probe())[0] == device.DEVICE_CPU

    @pytest.mark.parametrize("reason", sorted(device.DEVICE_REASON_I18N_KEYS))
    def test_every_reason_has_an_i18n_key(self, reason):
        assert device.device_reason_i18n_key(reason).startswith("transcription_device_")

    def test_an_unknown_reason_still_resolves_to_a_real_key(self):
        assert device.device_reason_i18n_key("something_new") == (
            device.DEVICE_REASON_I18N_KEYS[device.REASON_CPU_REQUESTED]
        )


class TestComputeType:
    def test_cpu_is_int8(self):
        assert device.select_compute_type(device.DEVICE_CPU, _probe()) == device.COMPUTE_INT8

    @pytest.mark.parametrize("capability", [(12, 0), (12, 1), (13, 0)])
    def test_sm_120_and_newer_never_get_int8(self, capability):
        # The whole reason this module exists: CTranslate2's sm_120 build has
        # INT8 disabled, and every int8 variant dies at model load.
        compute = device.select_compute_type(
            device.DEVICE_CUDA, _cuda_probe(capability=capability)
        )
        assert "int8" not in compute
        assert compute == device.COMPUTE_FLOAT16
        assert device.gpu_supports_int8(capability) is False

    @pytest.mark.parametrize("capability", [(7, 0), (7, 5), (8, 6), (8, 9), (9, 0)])
    def test_modern_gpus_get_float16(self, capability):
        assert device.select_compute_type(
            device.DEVICE_CUDA, _cuda_probe(capability=capability)
        ) == device.COMPUTE_FLOAT16

    @pytest.mark.parametrize("capability", [(3, 5), (5, 0), (6, 1)])
    def test_pre_volta_gpus_fall_back_to_float32(self, capability):
        assert device.select_compute_type(
            device.DEVICE_CUDA, _cuda_probe(capability=capability)
        ) == device.COMPUTE_FLOAT32

    @pytest.mark.parametrize("candidate", ["int8", "int8_float16", "int8_bfloat16"])
    def test_the_gate_strips_every_int8_flavour_on_sm_120(self, candidate):
        # The gate select_compute_type() actually runs its answer through,
        # tested on the variants a future VRAM-saving change would reach for.
        assert device._int8_safe(candidate, (12, 0)) == device.COMPUTE_FLOAT16
        assert device._int8_safe(candidate, None) == device.COMPUTE_FLOAT16
        assert device._int8_safe(candidate, (8, 6)) == candidate

    def test_the_cuda_path_actually_runs_through_the_gate(self, monkeypatch):
        # The wiring, not the outcome. Every value-based assertion in this
        # class stays green with the _int8_safe() call deleted, because the GPU
        # path picks float16 either way today — so the one thing that keeps the
        # gate on the path is checking that it is called.
        seen = []

        def _record(compute, capability):
            seen.append((compute, capability))
            return device.COMPUTE_FLOAT16

        monkeypatch.setattr(device, "_int8_safe", _record)
        chosen = device.select_compute_type(
            device.DEVICE_CUDA, _cuda_probe(capability=(12, 0))
        )
        assert seen == [(device.COMPUTE_FLOAT16, (12, 0))]
        assert chosen == device.COMPUTE_FLOAT16

    def test_the_cpu_path_does_not_need_the_gate(self, monkeypatch):
        # The CPU answer is int8 and must stay int8: the gate is about GPU
        # kernels, and running the CPU choice through it would silence the one
        # place int8 is correct.
        monkeypatch.setattr(device, "_int8_safe", lambda *_: "SHOULD NOT BE CALLED")
        assert device.select_compute_type(device.DEVICE_CPU, _probe()) == (
            device.COMPUTE_INT8
        )

    def test_the_gate_leaves_a_float_compute_type_alone(self):
        assert device._int8_safe(device.COMPUTE_FLOAT16, (12, 0)) == device.COMPUTE_FLOAT16
        assert device._int8_safe(device.COMPUTE_FLOAT32, (6, 1)) == device.COMPUTE_FLOAT32

    def test_an_unknown_capability_is_treated_as_modern_but_not_int8(self):
        probe = _cuda_probe(capability=None)
        assert device.select_compute_type(device.DEVICE_CUDA, probe) == device.COMPUTE_FLOAT16
        assert device.gpu_supports_int8(None) is False

    def test_no_gpu_path_ever_returns_an_int8_variant(self):
        # Swept over VRAM as well as capability. A cramped card is exactly the
        # condition under which someone would later make this prefer
        # int8_float16 — the case _int8_safe() exists for — so a sweep that
        # only varies capability would stay green with the gate deleted.
        for major in range(3, 14):
            for minor in (0, 5, 9):
                for free_vram_mb in (256, 1024, 4096, 24576):
                    probe = _cuda_probe(capability=(major, minor),
                                        free_vram_mb=free_vram_mb)
                    compute = device.select_compute_type(device.DEVICE_CUDA, probe)
                    assert "int8" not in compute or (major, minor) < (12, 0), (
                        f"sm_{major}{minor} with {free_vram_mb} MB free got {compute}"
                    )


class TestAutoSelectModel:
    def test_picks_the_largest_model_the_gpu_can_hold(self):
        chosen = device.auto_select_model(
            _cuda_probe(free_vram_mb=8192), device.DEVICE_CUDA, installed_ids=()
        )
        assert chosen == "large-v3"

    def test_never_picks_a_model_that_does_not_fit(self):
        for free_vram_mb in (512, 1024, 2048, 4096, 6144, 8192, 24576):
            probe = _cuda_probe(free_vram_mb=free_vram_mb)
            chosen = device.auto_select_model(probe, device.DEVICE_CUDA, installed_ids=())
            if chosen is None:
                continue
            model = model_catalog.get_model(chosen)
            assert model.min_vram_mb < free_vram_mb, (
                f"{chosen} needs {model.min_vram_mb} MB and only {free_vram_mb} MB is free"
            )
            assert device.model_fits(model, free_vram_mb, device.DEVICE_CUDA)

    def test_nothing_fits_returns_none(self):
        # The caller turns this into INSUFFICIENT_VRAM/RAM; guessing a model
        # that cannot load would fail later and less clearly.
        probe = _cuda_probe(free_vram_mb=256)
        assert device.auto_select_model(probe, device.DEVICE_CUDA, installed_ids=()) is None

    def test_prefers_an_installed_model_over_a_bigger_download(self):
        # 3 GB nobody asked for is worse than transcribing with what is here.
        probe = _cuda_probe(free_vram_mb=24576)
        chosen = device.auto_select_model(probe, device.DEVICE_CUDA, installed_ids=["base"])
        assert chosen == "base"

    def test_prefers_the_largest_installed_model_that_fits(self):
        probe = _cuda_probe(free_vram_mb=24576)
        chosen = device.auto_select_model(
            probe, device.DEVICE_CUDA, installed_ids=["tiny", "small", "base"]
        )
        assert chosen == "small"

    def test_an_installed_model_that_does_not_fit_is_skipped(self):
        probe = _cuda_probe(free_vram_mb=2560)
        chosen = device.auto_select_model(
            probe, device.DEVICE_CUDA, installed_ids=["large-v3"]
        )
        assert chosen != "large-v3"
        assert model_catalog.get_model(chosen).min_vram_mb <= 2048

    def test_the_cpu_path_is_measured_against_ram_not_vram(self):
        # A machine with a big card and little RAM must not be handed large-v3
        # because the VRAM figure said so.
        probe = device.HardwareProbe(total_ram_mb=4096, total_vram_mb=24576,
                                     free_vram_mb=24576)
        chosen = device.auto_select_model(probe, device.DEVICE_CPU, installed_ids=())
        assert model_catalog.get_model(chosen).min_ram_mb <= 3072

    def test_unknown_memory_uses_an_installed_model_or_nothing(self):
        probe = _probe(driver_error="nvml: could not load nvml.dll")
        assert device.auto_select_model(probe, device.DEVICE_CPU, installed_ids=()) is None
        assert device.auto_select_model(
            probe, device.DEVICE_CPU, installed_ids=["medium", "tiny"]
        ) == "tiny"

    def test_a_caller_supplied_catalogue_is_ordered_by_this_module(self):
        # Reversed on purpose: "largest that fits" must not depend on the order
        # the caller happened to hand over.
        catalog = tuple(reversed(model_catalog.list_models()))
        chosen = device.auto_select_model(
            _cuda_probe(free_vram_mb=8192), device.DEVICE_CUDA,
            installed_ids=(), catalog=catalog,
        )
        assert chosen == "large-v3"

    def test_headroom_is_required_not_just_a_bare_fit(self):
        # The rule spelled out rather than borrowed back from model_fits(): a
        # model fits when the free memory covers its minimum plus 25%. 2048 MB
        # free against a minimum of exactly 2048 MB is a miss — the minimum
        # covers the model, not Chromium and the desktop beside it.
        small = model_catalog.get_model("small")
        assert small.min_vram_mb == 2048
        assert not device.model_fits(small, 2048, device.DEVICE_CUDA)
        assert not device.model_fits(small, int(2048 * 1.25) - 1, device.DEVICE_CUDA)
        assert device.model_fits(small, int(2048 * 1.25), device.DEVICE_CUDA)

    def test_no_chosen_model_ever_breaks_the_headroom_rule(self):
        for free_vram_mb in (900, 1280, 2000, 2560, 5120, 7680, 24576):
            chosen = device.auto_select_model(
                _cuda_probe(free_vram_mb=free_vram_mb), device.DEVICE_CUDA,
                installed_ids=(),
            )
            if chosen is None:
                continue
            assert free_vram_mb >= model_catalog.get_model(chosen).min_vram_mb * 1.25

    def test_free_vram_wins_over_total_vram(self):
        probe = device.HardwareProbe(
            cuda_available=True, cuda_device_count=1, compute_capability=(8, 6),
            total_vram_mb=24576, free_vram_mb=1024,
        )
        assert device.available_memory_mb(probe, device.DEVICE_CUDA) == 1024

    def test_available_ram_wins_over_total_ram(self):
        # 16 GB installed, 11 GB already taken by Chromium/WhatsApp Web, the
        # screen reader and the user own work. Planning against the 16 buys a
        # 3 GB download and then an allocation error.
        probe = device.HardwareProbe(total_ram_mb=16384, available_ram_mb=5120)
        assert device.available_memory_mb(probe, device.DEVICE_CPU) == 5120
        chosen = device.auto_select_model(probe, device.DEVICE_CPU, installed_ids=())
        assert chosen != "large-v3"
        assert model_catalog.get_model(chosen).min_ram_mb * 1.25 <= 5120

    def test_total_ram_is_the_fallback_when_available_is_unknown(self):
        probe = device.HardwareProbe(total_ram_mb=16384)
        assert device.available_memory_mb(probe, device.DEVICE_CPU) == 16384


class TestASingleLanguageModelIsOnlyPickedForItsLanguage:
    """The automatic choice never lands on a model that cannot hear the
    user's language: an English or Swedish one installed and picked for a
    Portuguese note answers with confident text in the wrong language."""

    def test_installed_single_language_models_are_passed_over(self):
        chosen = device.auto_select_model(
            _cuda_probe(free_vram_mb=24576), device.DEVICE_CUDA,
            installed_ids=["kb-whisper-large", "medium.en", "distil-large-v3.5"],
        )
        assert model_catalog.get_model(chosen).language is None

    def test_detection_never_downloads_one_either(self):
        for language in (None, "pt"):
            chosen = device.auto_select_model(
                _cuda_probe(free_vram_mb=24576), device.DEVICE_CUDA, installed_ids=(),
                language=language,
            )
            assert chosen == "large-v3"

    @pytest.mark.parametrize("language, installed", [
        ("en", "medium.en"), ("sv", "kb-whisper-large"), ("ja", "kotoba-whisper-v2.0"),
    ])
    def test_the_users_own_language_may_be_picked(self, language, installed):
        chosen = device.auto_select_model(
            _cuda_probe(free_vram_mb=24576), device.DEVICE_CUDA,
            installed_ids=[installed], language=language,
        )
        assert chosen == installed


class TestResolveWhisperCppDevice:
    """whisper.cpp's own device rule: its graphics build, not CTranslate2's."""

    def test_the_processor_asked_for_is_the_processor(self):
        assert device.resolve_whisper_cpp_device(
            device.PREFERENCE_CPU, _cuda_probe(), True
        ) == (device.DEVICE_CPU, device.REASON_CPU_REQUESTED)

    def test_no_card_reads_as_resolve_device_says(self):
        for preference in (device.PREFERENCE_AUTO, device.PREFERENCE_CUDA):
            assert device.resolve_whisper_cpp_device(preference, _probe(), True) == (
                device.resolve_device(preference, _probe())
            )

    @pytest.mark.parametrize("capability", [(12, 0), (12, 1), None, (3, 7)])
    def test_a_card_the_build_cannot_run_on_is_the_processor_up_front(self, capability):
        # sm_120 (Blackwell) and an undescribed card: the pinned CUDA build
        # has no kernels for them, so the run must not even try.
        assert device.resolve_whisper_cpp_device(
            device.PREFERENCE_AUTO, _cuda_probe(capability=capability), True
        ) == (device.DEVICE_CPU, device.REASON_CUDA_BUILD_UNSUPPORTED)

    def test_a_supported_card_without_the_build_is_the_processor_and_says_why(self):
        assert device.resolve_whisper_cpp_device(
            device.PREFERENCE_CUDA, _cuda_probe(capability=(8, 6)), False
        ) == (device.DEVICE_CPU, device.REASON_CUDA_BUILD_MISSING)

    def test_a_supported_card_with_the_build_is_the_card(self):
        # Whatever CTranslate2's cuBLAS answer: the build carries its own.
        probe = _cuda_probe(capability=(8, 6), cuda_libraries_ok=False)
        assert device.resolve_whisper_cpp_device(device.PREFERENCE_AUTO, probe, True) == (
            device.DEVICE_CUDA, device.REASON_CUDA_SELECTED
        )

    @pytest.mark.parametrize("locale", LOCALES)
    def test_both_reasons_are_said_in_every_language(self, locale):
        table = load_strings(locale)
        for reason in (device.REASON_CUDA_BUILD_UNSUPPORTED, device.REASON_CUDA_BUILD_MISSING):
            assert table[device.device_reason_i18n_key(reason)], (locale, reason)


class TestProbeHardware:
    def test_probing_never_raises_and_answers_with_a_probe(self):
        # It loads a driver DLL and calls into the Windows memory API; on a
        # runner with neither, every field is simply unknown.
        probe = device.probe_hardware()
        assert isinstance(probe, device.HardwareProbe)
        assert isinstance(probe.cuda_available, bool)
        assert probe.cuda_device_count >= 0

    def test_a_failing_nvml_probe_still_returns_a_probe(self, monkeypatch):
        # A runner with no GPU never reaches _probe_nvml() at all, so the
        # failure has to be planted on both halves: a backend that reports a
        # device, and a driver probe that then blows up on it. That is the real
        # shape of the fault — the DLL loads and faults inside ctypes.
        monkeypatch.setitem(
            sys.modules, "ctranslate2",
            types.SimpleNamespace(get_cuda_device_count=lambda: 1),
        )

        def _boom():
            raise OSError("nvml exploded")

        monkeypatch.setattr(device, "_probe_nvml", _boom)
        # The library check is answered here rather than left to the machine:
        # this test is about NVML alone, and on a runner without the CUDA
        # runtime the real check would (correctly) veto the GPU below.
        monkeypatch.setattr(device, "probe_cuda_libraries", lambda: (True, (), None))
        probe = device.probe_hardware()
        assert isinstance(probe, device.HardwareProbe)
        assert probe.cuda_available is True
        assert probe.compute_capability is None
        # The GPU was counted and could not be described: this is the one case
        # that may be announced as a driver fault.
        assert "nvml exploded" in probe.cuda_probe_error
        assert device.resolve_device(device.PREFERENCE_CUDA, probe)[1] == (
            device.REASON_CUDA_SELECTED
        )

    def test_a_failing_ram_probe_still_returns_a_probe(self, monkeypatch):
        def _boom():
            raise OSError("GlobalMemoryStatusEx exploded")

        monkeypatch.setattr(device, "_probe_ram_mb", _boom)
        probe = device.probe_hardware()
        assert probe.total_ram_mb is None
        assert probe.available_ram_mb is None
        assert "GlobalMemoryStatusEx exploded" in probe.driver_error

    def test_a_missing_ctranslate2_still_returns_a_probe(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def _refuse(name, *args, **kwargs):
            if name == "ctranslate2":
                raise ImportError("no ctranslate2 here")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _refuse)
        probe = device.probe_hardware()
        assert probe.cuda_available is False
        # It lands in the aggregate, and nowhere near the driver-fault field.
        assert "ctranslate2" in probe.driver_error
        assert probe.cuda_probe_error is None

    def test_a_reported_nvml_failure_reaches_both_error_fields(self, monkeypatch):
        # The common real case: a card is present, nvml.dll is not. The helper
        # returns the reason rather than raising, and the reason has to land in
        # cuda_probe_error (which may be announced) AND in the aggregate (which
        # is what the log gets).
        monkeypatch.setitem(
            sys.modules, "ctranslate2",
            types.SimpleNamespace(get_cuda_device_count=lambda: 1),
        )
        monkeypatch.setattr(
            device, "_probe_nvml", lambda: (None, None, None, "nvml: not found")
        )
        probe = device.probe_hardware()
        assert probe.cuda_probe_error == "nvml: not found"
        assert "nvml: not found" in probe.driver_error
        assert probe.total_vram_mb is None and probe.free_vram_mb is None

    def test_loading_nvml_reports_every_path_it_tried(self, monkeypatch):
        # Without a card there is no DLL to load, so the failure path is the
        # only one testable here — and it is the one that reaches the log.
        def _refuse(path):
            raise OSError(f"cannot load {path}")

        monkeypatch.setattr(device.ctypes, "CDLL", _refuse)
        library, error = device._load_nvml()
        assert library is None
        # Keeping only the last failure left every card-less machine blaming
        # the legacy NVSMI path, the least informative of the three.
        assert "nvml.dll" in error
        assert error.count("cannot load") == len(device._NVML_LIBRARY_PATHS)

    def test_the_nvml_search_starts_with_the_bare_name(self):
        paths = device._NVML_LIBRARY_PATHS
        assert len(paths) == 3
        assert paths[0] == "nvml.dll"
        # Then the two absolute locations pynvml itself falls back to.
        assert paths[1].lower().endswith(r"system32\nvml.dll")
        assert "NVSMI" in paths[2]

    def test_a_probe_result_still_yields_a_usable_decision(self):
        probe = device.probe_hardware()
        chosen, reason = device.resolve_device(device.PREFERENCE_AUTO, probe)
        assert chosen in (device.DEVICE_CPU, device.DEVICE_CUDA)
        assert reason in device.DEVICE_REASON_I18N_KEYS
        assert device.select_compute_type(chosen, probe) in (
            device.COMPUTE_INT8, device.COMPUTE_FLOAT16, device.COMPUTE_FLOAT32,
        )


class TestCudaLibraries:
    """Whether CUDA can be *used*, which is not what counting devices answers.

    ctranslate2 4.8.2's Windows CUDA build names no CUDA library in its import
    table (the CUDA runtime is static) and opens cuBLAS with LoadLibrary the
    first time a model goes on the GPU. Nothing in requirements.txt ships it,
    so the failure lands at model load on a user's machine and never on a
    developer's, which has the Toolkit. The check has to be cheap enough to run
    before every transcription and must never take the app down with it.
    """

    @pytest.fixture(autouse=True)
    def _forget_the_memoized_answer(self, monkeypatch):
        """The answer is memoized per process, so it is per-test state here."""
        monkeypatch.setattr(device, "_cuda_library_answer", None)

    def test_cublas_is_the_library_that_matters(self):
        # Read off the shipped wheel, not off the documentation: the strings of
        # ctranslate2.dll carry "cublas64_12.dll" beside cublasCreate_v2 and
        # cublasGemmEx. cublasLt needs no entry of its own — cuBLAS imports it,
        # so the Windows loader fails this check for it too.
        assert device._CUDA_RUNTIME_LIBRARIES == ("cublas64_12.dll",)

    def test_cudnn_is_deliberately_not_required(self):
        """The wheel ships a cudnn64_9.dll and this build never calls it.

        That 266 KB file is the cuDNN 9 loader shim, forwarding to
        cudnn_graph/ops/cnn/adv64_9.dll, none of which the wheel ships — and
        ctranslate2/__init__.py loads it anyway with a blanket CDLL(*.dll).
        But ctranslate2.dll references cuDNN nowhere: not an import, not a
        string, in neither the DLL nor the extension module. Requiring it here
        would refuse the GPU to every machine over a library nothing calls.
        """
        assert not any("cudnn" in name for name in device._CUDA_RUNTIME_LIBRARIES)

    def test_the_bare_name_is_asked_the_way_the_run_asks_it(self):
        """`winmode=0`, and it cannot be observed from a test.

        ctypes forces LOAD_LIBRARY_SEARCH_DEFAULT_DIRS on every load of its
        own, while CTranslate2 opens cuBLAS with a raw LoadLibrary that follows
        the process search order. The two agree under python.exe and diverge in
        a frozen build, whose PyInstaller bootloader calls the legacy
        SetDllDirectory: measured on a real onedir build, a DLL reachable only
        through PATH — where the CUDA Toolkit installer puts it — loaded for
        the run and not for the check. That made the app announce "the CUDA
        libraries are not installed", falsely, and fall back to the CPU with
        nothing to retry, because the veto lands before the run. None of that
        is reproducible under pytest, so the intent is pinned on the source.
        """
        source = inspect.getsource(device._load_cuda_library)
        assert "winmode=0" in source
        # And only for the bare name: a registered directory keeps the default
        # flags, which is what lets cuBLASLt resolve out of the same folder.
        assert "(name, 0)" in source

    def test_the_check_answers_on_this_machine_without_raising(self):
        # This runner has no CUDA runtime, so the shape of the answer is known
        # rather than merely "one of the three": every library is missing, and
        # each is named — an assertion that accepted True as well would pass on
        # a machine where the check silently did nothing.
        ok, missing, error = device.probe_cuda_libraries(
            directories=(os.path.join("nowhere", "cuda"),)
        )
        assert ok is False
        assert missing == device._CUDA_RUNTIME_LIBRARIES
        assert "cublas64_12.dll" in error
        # The default path answers whatever this machine says, so there is
        # nothing to assert about it — only that it comes back at all, which
        # is the promise every caller in this module relies on.
        device.probe_cuda_libraries()

    def test_a_library_that_will_not_load_is_reported_as_missing(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")

        def _refuse(name, winmode=None):
            raise OSError(f"could not find {name}")

        monkeypatch.setattr(device.ctypes, "WinDLL", _refuse, raising=False)
        ok, missing, error = device.probe_cuda_libraries()
        assert ok is False
        assert missing == device._CUDA_RUNTIME_LIBRARIES
        assert "cublas64_12.dll" in error

    def test_a_library_that_loads_is_not_missing(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            device.ctypes, "WinDLL",
            lambda name, winmode=None: _FakeLibrary(name), raising=False,
        )
        monkeypatch.setattr(device, "_release_library", lambda library: None)
        ok, missing, error = device.probe_cuda_libraries()
        assert ok is True
        assert missing == ()
        assert error is None

    def test_the_load_is_released_again(self, monkeypatch):
        """The check runs before every transcription, so it may not accumulate.

        ctypes never unloads a library on its own; without the release, a probe
        per transcription piles up references to a library the decision may
        well not end up using.
        """
        released = []
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            device.ctypes, "WinDLL",
            lambda name, winmode=None: _FakeLibrary(name), raising=False,
        )
        monkeypatch.setattr(
            device.ctypes, "windll",
            types.SimpleNamespace(
                kernel32=types.SimpleNamespace(
                    FreeLibrary=lambda handle: released.append(handle)
                )
            ),
            raising=False,
        )
        assert device.probe_cuda_libraries()[0] is True
        assert len(released) == len(device._CUDA_RUNTIME_LIBRARIES)

    def test_a_release_that_fails_is_not_a_verdict(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            device.ctypes, "WinDLL",
            lambda name, winmode=None: _FakeLibrary(name), raising=False,
        )

        def _refuse_to_free(_handle):
            raise OSError("FreeLibrary exploded")

        monkeypatch.setattr(
            device.ctypes, "windll",
            types.SimpleNamespace(
                kernel32=types.SimpleNamespace(FreeLibrary=_refuse_to_free)
            ),
            raising=False,
        )
        assert device.probe_cuda_libraries()[0] is True

    def test_ctypes_blowing_up_is_unknown_rather_than_a_no(self, monkeypatch):
        # Not an answer about the machine, so it must not veto the GPU.
        monkeypatch.setattr(sys, "platform", "win32")

        def _explode(*_args):
            raise RuntimeError("ctypes is having a day")

        monkeypatch.setattr(device, "_load_cuda_library", _explode)
        monkeypatch.setattr(device, "_cuda_library_answer", None)
        ok, missing, error = device.probe_cuda_libraries()
        assert ok is None
        assert missing == ()
        assert "ctypes is having a day" in error

    def test_off_windows_the_question_is_not_asked_at_all(self, monkeypatch):
        # The library names are not these ones there, and answering "no" would
        # be inventing an obstacle nobody measured.
        monkeypatch.setattr(sys, "platform", "linux")
        ok, missing, error = device.probe_cuda_libraries()
        assert ok is None
        assert missing == ()
        # And no error text: it feeds driver_error, which is the bag of what
        # failed, and not having asked is not a failure.
        assert error is None

    def test_every_directory_that_failed_is_reported(self, monkeypatch):
        # Same rule as _load_nvml(): the last failure alone says nothing about
        # why the ones before it were not enough.
        monkeypatch.setattr(sys, "platform", "win32")

        def _refuse(name, winmode=None):
            raise OSError(f"cannot load {name}")

        monkeypatch.setattr(device.ctypes, "WinDLL", _refuse, raising=False)
        _ok, _missing, error = device.probe_cuda_libraries(
            directories=(os.path.join("somewhere", "cuda"),)
        )
        # The bare name, then the directory handed in.
        assert error.count("cannot load") == 2
        assert "somewhere" in error

    def test_a_registered_directory_is_searched_after_the_bare_name(self, monkeypatch):
        tried = []
        monkeypatch.setattr(sys, "platform", "win32")

        def _only_the_extra_directory(name, winmode=None):
            tried.append((name, winmode))
            if os.path.dirname(name):
                return _FakeLibrary(name)
            raise OSError("not on the default search path")

        monkeypatch.setattr(
            device.ctypes, "WinDLL", _only_the_extra_directory, raising=False
        )
        monkeypatch.setattr(device, "_release_library", lambda library: None)
        folder = os.path.join("elsewhere", "cuda")
        ok, missing, _error = device.probe_cuda_libraries(directories=(folder,))
        assert ok is True and missing == ()
        # The bare name asked the way the run asks (winmode=0), then the
        # registered directory with the default flags, which is what lets a
        # dependency like cuBLASLt resolve out of that same folder.
        assert tried[0] == ("cublas64_12.dll", 0)
        assert tried[1] == (os.path.join(folder, "cublas64_12.dll"), None)


class TestTheLibraryAnswerIsRemembered:
    """Asked once per process, not once per transcription.

    The rule against reusing a probe is about free memory, which moves while
    the app runs. Whether a library loads does not — and measuring it maps and
    unmaps cuBLAS and cuBLASLt, some 600 MB and two DllMain runs, immediately
    before CTranslate2 maps them again.
    """

    @pytest.fixture(autouse=True)
    def _forget_the_memoized_answer(self, monkeypatch):
        monkeypatch.setattr(device, "_cuda_library_answer", None)
        monkeypatch.setattr(device, "_cuda_library_generation", 0)
        monkeypatch.setattr(device, "_extra_cuda_library_dirs", {})

    def test_registering_a_directory_again_still_forgets_the_answer(
        self, monkeypatch, tmp_path
    ):
        """Part 4b registers the same directory twice, and the second time is
        the one that matters.

        The directory under global_dir() is registered while it is still empty,
        so the probe memoizes False. The download then lands in it and part 4b
        registers again — through the "already registered" shortcut. Without an
        invalidation there, that False stands for the rest of the session: the
        user paid for ~600 MB and stays on the CPU until they restart.
        """
        answers = [(False, ("cublas64_12.dll",), "not found"), (True, (), None)]
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            device, "_measure_cuda_libraries",
            lambda directories: answers.pop(0) if answers else (True, (), None),
        )
        folder = str(tmp_path / "cuda")
        os.makedirs(folder)

        assert device.register_cuda_library_directory(folder) is True
        assert device.probe_cuda_libraries()[0] is False   # still empty

        assert device.register_cuda_library_directory(folder) is True  # the download landed
        assert device.probe_cuda_libraries()[0] is True

    def test_taking_libraries_away_can_be_told_to_this_module(self, monkeypatch):
        """The invalidation a *removal* needs, which registering cannot give.

        Registering a directory invalidates on its own because it is the event
        that can turn a False into a True. Deleting the libraries is the same
        event pointing the other way and has no such call, so without this a
        True measured earlier in the session outlives the files: cuda_usable()
        keeps choosing the GPU and every transcription until the app restarts
        loads the model onto the card and dies on the missing library.
        """
        monkeypatch.setattr(device, "_cuda_library_answer", (True, (), None))

        device.forget_cuda_library_answer()

        assert device._cuda_library_answer is None
        # The generation moves too, so a measurement that started before the
        # removal cannot store its now-stale result afterwards.
        assert device._cuda_library_generation == 1

    def test_forgetting_twice_is_not_an_error(self, monkeypatch):
        device.forget_cuda_library_answer()
        device.forget_cuda_library_answer()
        assert device._cuda_library_answer is None

    def test_a_registration_during_the_measurement_is_not_overwritten(
        self, monkeypatch, tmp_path
    ):
        """The measurement runs outside the lock, because it maps ~600 MB.

        So a registration can land while it is in flight, and the answer that
        arrives afterwards describes the world from before it. Storing it
        anyway is the same permanent False as the shortcut above — and this is
        deterministic, not a race: the stub registers from inside the measure.
        """
        folder = str(tmp_path / "cuda")
        os.makedirs(folder)
        measured = []

        def _measure(directories):
            measured.append(directories)
            if len(measured) == 1:
                # "a registration arrived during the measurement"
                device.register_cuda_library_directory(folder)
                return False, ("cublas64_12.dll",), "not found"
            return True, (), None

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(device, "_measure_cuda_libraries", _measure)

        assert device.probe_cuda_libraries()[0] is False
        # Nothing was stored, so the next question is measured against the
        # world as it is now rather than answered from the stale one.
        assert device.probe_cuda_libraries()[0] is True
        assert len(measured) == 2

    def test_a_definite_answer_is_measured_once(self, monkeypatch):
        calls = []
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            device, "_measure_cuda_libraries",
            lambda directories: calls.append(directories) or (True, (), None),
        )
        first = device.probe_cuda_libraries()
        second = device.probe_cuda_libraries()
        assert first == second == (True, (), None)
        assert len(calls) == 1

    def test_a_missing_library_is_remembered_too(self, monkeypatch):
        calls = []
        answer = (False, ("cublas64_12.dll",), "cublas64_12.dll: not found")
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            device, "_measure_cuda_libraries",
            lambda directories: calls.append(directories) or answer,
        )
        assert device.probe_cuda_libraries() == answer
        assert device.probe_cuda_libraries() == answer
        assert len(calls) == 1

    def test_an_unknown_is_never_remembered(self, monkeypatch):
        # A transient ctypes fault must not become a permanent verdict.
        calls = []
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            device, "_measure_cuda_libraries",
            lambda directories: calls.append(directories) or (None, (), "odd"),
        )
        device.probe_cuda_libraries()
        device.probe_cuda_libraries()
        assert len(calls) == 2

    def test_an_explicit_question_neither_reads_nor_writes_the_cache(
        self, monkeypatch, tmp_path
    ):
        calls = []
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            device, "_measure_cuda_libraries",
            lambda directories: calls.append(directories) or (True, (), None),
        )
        device.probe_cuda_libraries(directories=(str(tmp_path),))
        device.probe_cuda_libraries(directories=(str(tmp_path),))
        assert len(calls) == 2
        assert device._cuda_library_answer is None

    def test_registering_a_directory_makes_the_next_call_measure_again(
        self, monkeypatch, tmp_path
    ):
        """Part 4b's download is the one event that can change the answer."""
        calls = []
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            device, "_measure_cuda_libraries",
            lambda directories: calls.append(directories) or (False, (), "not yet"),
        )
        monkeypatch.setattr(
            device.os, "add_dll_directory", lambda path: None, raising=False
        )
        device.probe_cuda_libraries()
        assert device.register_cuda_library_directory(str(tmp_path)) is True
        device.probe_cuda_libraries()
        assert len(calls) == 2
        # And the second measurement was told about the new directory.
        assert calls[1] == (str(tmp_path),)


class TestCudaLibraryDirectories:
    """Part 4b's hook: it downloads the runtime, this is told where it landed.

    Recording the path lets the transient probe and preparation load by
    absolute path. AddDllDirectory resolves their dependencies; preparation
    must additionally keep the mapping alive for CTranslate2's legacy lookup.
    """

    def test_registering_a_real_directory_reports_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(device, "_extra_cuda_library_dirs", {})
        assert device.register_cuda_library_directory(str(tmp_path)) is True
        assert device.cuda_library_directories() == (str(tmp_path),)

    def test_registering_twice_is_a_no_op(self, tmp_path, monkeypatch):
        monkeypatch.setattr(device, "_extra_cuda_library_dirs", {})
        assert device.register_cuda_library_directory(str(tmp_path)) is True
        assert device.register_cuda_library_directory(str(tmp_path)) is True
        assert len(device.cuda_library_directories()) == 1

    def test_the_directory_is_handed_to_the_windows_loader_too(
        self, tmp_path, monkeypatch
    ):
        added = []
        monkeypatch.setattr(device, "_extra_cuda_library_dirs", {})
        monkeypatch.setattr(
            device.os, "add_dll_directory", lambda path: added.append(path),
            raising=False,
        )
        device.register_cuda_library_directory(str(tmp_path))
        assert added == [str(tmp_path)]

    @pytest.mark.parametrize("path", [None, "", "   "])
    def test_a_path_that_is_not_a_directory_is_refused_quietly(self, path, monkeypatch):
        # An empty path in particular: abspath("") is the working directory,
        # and registering that would put the whole cwd on the DLL search path.
        monkeypatch.setattr(device, "_extra_cuda_library_dirs", {})
        assert device.register_cuda_library_directory(path) is False
        assert device.cuda_library_directories() == ()

    def test_a_missing_directory_is_refused_rather_than_raised(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(device, "_extra_cuda_library_dirs", {})
        assert device.register_cuda_library_directory(
            str(tmp_path / "was-never-downloaded")
        ) is False

    def test_a_registration_that_explodes_is_survived(self, tmp_path, monkeypatch):
        # It runs as a download finishes; a directory that misbehaves is a
        # reason to keep using the CPU, not to take the app down.
        monkeypatch.setattr(device, "_extra_cuda_library_dirs", {})

        def _boom(_path):
            raise OSError("the loader refused it")

        monkeypatch.setattr(device.os, "add_dll_directory", _boom, raising=False)
        assert device.register_cuda_library_directory(str(tmp_path)) is False


class TestTheRegistryIsThreadSafe:
    def test_registering_while_the_directories_are_read_does_not_raise(
        self, tmp_path, monkeypatch
    ):
        """The probe runs on the job thread; part 4b registers on the UI one.

        A dict grown mid-iteration raises RuntimeError, which would turn a
        download that had just succeeded into an "unknown" for that run.
        """
        monkeypatch.setattr(device, "_extra_cuda_library_dirs", {})
        monkeypatch.setattr(device, "_cuda_library_answer", None)
        monkeypatch.setattr(
            device.os, "add_dll_directory", lambda path: None, raising=False
        )
        folders = []
        for index in range(60):
            folder = tmp_path / f"cuda-{index}"
            folder.mkdir()
            folders.append(str(folder))

        failures = []

        def _register():
            try:
                for folder in folders:
                    device.register_cuda_library_directory(folder)
            except Exception as exc:  # pragma: no cover - the bug being pinned
                failures.append(exc)

        writer = threading.Thread(target=_register)
        writer.start()
        try:
            # Read for as long as the writer lives, rather than a fixed count:
            # measured against the unlocked version, 400 reads raced the 60
            # registrations 0 times in 20 runs — the test passed with and
            # without the lock, i.e. it pinned nothing. Bounded by the writer
            # instead, it reproduces the RuntimeError every time and scales
            # with whatever machine runs it.
            while writer.is_alive():
                device.cuda_library_directories()
        except Exception as exc:  # pragma: no cover - the bug being pinned
            failures.append(exc)
        writer.join(30)

        assert failures == []
        assert len(device.cuda_library_directories()) == len(folders)


class TestCudaRuntimePresence:
    """Part 4b's "is it already here?", which is not "does it work?".

    Registering answers only that a directory exists — an empty one registers
    exactly as happily as a complete one — so the question of whether to spend
    a download needs its own answer, and loadability stays
    `probe_cuda_libraries()`'s.
    """

    def test_a_folder_holding_every_library_is_present(self, tmp_path):
        for name in device._CUDA_RUNTIME_LIBRARIES:
            (tmp_path / name).write_bytes(b"not a real library")
        assert device.cuda_runtime_present_in(str(tmp_path)) is True

    def test_an_empty_folder_is_not(self, tmp_path):
        assert device.cuda_runtime_present_in(str(tmp_path)) is False

    def test_a_half_finished_download_is_not(self, tmp_path):
        # Only meaningful once more than one library is required; written so it
        # keeps meaning something when that day comes.
        for name in device._CUDA_RUNTIME_LIBRARIES[:-1]:
            (tmp_path / name).write_bytes(b"not a real library")
        assert device.cuda_runtime_present_in(str(tmp_path)) is False

    def test_a_directory_that_is_not_there_answers_no_rather_than_raising(
        self, tmp_path
    ):
        assert device.cuda_runtime_present_in(
            str(tmp_path / "was-never-downloaded")
        ) is False

    @pytest.mark.parametrize("path", [None, ""])
    def test_nothing_at_all_answers_no(self, path):
        assert device.cuda_runtime_present_in(path) is False


def test_the_library_names_belong_to_the_pinned_ctranslate2():
    """"cublas64_12" is CUDA 12 naming, and the pin is what makes it true.

    A bump to a CTranslate2 built against CUDA 13 renames the library. The
    check would then report it missing on *every* machine, every user would
    silently lose the GPU, and each would be told the CUDA libraries are not
    installed — which would be false. So the two are asserted together: moving
    the pin fails this test until the names move with it.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "requirements.txt"), "r", encoding="utf-8") as handle:
        requirements = handle.read()

    assert "ctranslate2==4.8.2" in requirements
    assert device._CUDA_RUNTIME_LIBRARIES == ("cublas64_12.dll",)


class TestProbeReportsTheLibraries:
    def test_a_missing_library_reaches_the_decision_and_the_log(self, monkeypatch):
        monkeypatch.setitem(
            sys.modules, "ctranslate2",
            types.SimpleNamespace(get_cuda_device_count=lambda: 1),
        )
        monkeypatch.setattr(device, "_probe_nvml", lambda: ((8, 6), 8192, 8192, None))
        monkeypatch.setattr(
            device, "probe_cuda_libraries",
            lambda: (False, ("cublas64_12.dll",), "cublas64_12.dll: not found"),
        )
        probe = device.probe_hardware()

        assert probe.cuda_available is True and probe.cuda_device_count == 1
        assert probe.cuda_libraries_ok is False
        assert probe.missing_cuda_libraries == ("cublas64_12.dll",)
        # The aggregate is log-facing and takes everything; the driver-fault
        # field must stay empty — the driver is fine, the libraries are not.
        assert "cublas64_12.dll" in probe.driver_error
        assert probe.cuda_probe_error is None
        assert device.resolve_device(device.PREFERENCE_AUTO, probe)[1] == (
            device.REASON_CUDA_LIBRARIES_MISSING
        )

    def test_the_check_is_not_run_when_no_device_was_counted(self, monkeypatch):
        # Loading cuBLAS on a machine with no NVIDIA card answers nothing and
        # is the one place this check would cost something for nothing.
        calls = []
        monkeypatch.setitem(
            sys.modules, "ctranslate2",
            types.SimpleNamespace(get_cuda_device_count=lambda: 0),
        )
        monkeypatch.setattr(
            device, "probe_cuda_libraries",
            lambda: calls.append(1) or (True, (), None),
        )
        probe = device.probe_hardware()
        assert calls == []
        assert probe.cuda_libraries_ok is None
        assert probe.missing_cuda_libraries == ()

    def test_a_library_check_that_raises_still_returns_a_probe(self, monkeypatch):
        monkeypatch.setitem(
            sys.modules, "ctranslate2",
            types.SimpleNamespace(get_cuda_device_count=lambda: 1),
        )
        monkeypatch.setattr(device, "_probe_nvml", lambda: (None, None, None, None))

        def _boom():
            raise OSError("the loader exploded")

        monkeypatch.setattr(device, "probe_cuda_libraries", _boom)
        probe = device.probe_hardware()
        assert isinstance(probe, device.HardwareProbe)
        assert probe.cuda_libraries_ok is None
        assert "the loader exploded" in probe.driver_error
        # Unknown, so the GPU is still allowed — and still announced as such.
        assert device.resolve_device(device.PREFERENCE_AUTO, probe)[0] == (
            device.DEVICE_CUDA
        )


class TestCpuRetry:
    """Running it again on the processor — when that is worth offering.

    An allowlist, because the offer costs the user the whole wait a second
    time: a 40-minute recording redone for a failure the CPU cannot cure is
    40 minutes spent learning to dismiss the offer.
    """

    @pytest.mark.parametrize(
        "code", [errors.INSUFFICIENT_VRAM, errors.CUDA_UNAVAILABLE,
                 # The card refused the chosen precision; the processor run
                 # resolves the same choice again, to one it can run.
                 errors.PRECISION_UNSUPPORTED]
    )
    def test_a_gpu_fault_the_cpu_would_not_have_is_worth_redoing(self, code):
        error = errors.TranscriptionError(code, "detail for the log")
        assert device.should_retry_on_cpu(error, device.DEVICE_CUDA) is True
        assert device.cpu_retry_i18n_key(error, device.DEVICE_CUDA)

    @pytest.mark.parametrize("code", [
        errors.CANCELLED,
        errors.MODEL_NOT_INSTALLED,
        errors.MODEL_CORRUPTED,
        errors.MEDIA_NOT_DOWNLOADED,
    ])
    def test_the_cases_the_issue_names_as_pointless_are_not_offered(self, code):
        error = errors.TranscriptionError(code)
        assert device.should_retry_on_cpu(error, device.DEVICE_CUDA) is False
        assert device.cpu_retry_i18n_key(error, device.DEVICE_CUDA) is None

    @pytest.mark.parametrize("code", sorted(errors.ERROR_CODES))
    def test_every_code_has_an_answer_and_only_these_are_yes(self, code):
        # The set is written out rather than read back off the implementation:
        # a code added to the allowlist has to come here and re-argue itself
        # against the comment above it, which is where the reasoning is.
        # PRECISION_UNSUPPORTED did (part 11): the processor run picks a
        # precision the processor can run.
        worth_redoing = {errors.INSUFFICIENT_VRAM, errors.CUDA_UNAVAILABLE,
                         errors.PRECISION_UNSUPPORTED}
        error = errors.TranscriptionError(code)
        offered = device.should_retry_on_cpu(error, device.DEVICE_CUDA)
        assert offered is (code in worth_redoing)

    def test_insufficient_ram_is_never_the_cpus_problem_to_solve(self):
        # It is the CPU's own memory that ran out; redoing it there is the one
        # thing guaranteed to fail the same way.
        error = errors.TranscriptionError(errors.INSUFFICIENT_RAM)
        assert device.should_retry_on_cpu(error, device.DEVICE_CUDA) is False

    def test_a_backend_error_is_not_offered(self):
        # The GPU-only backend fault seen on real hardware — int8 on sm_120 —
        # is already prevented by select_compute_type(), so what is left under
        # this code is the unknown, and an offer that usually fails again is
        # worse than none.
        error = errors.TranscriptionError(errors.BACKEND_ERROR, "something new")
        assert device.should_retry_on_cpu(error, device.DEVICE_CUDA) is False

    def test_a_run_that_was_already_on_the_cpu_is_never_offered_the_cpu(self):
        error = errors.TranscriptionError(errors.INSUFFICIENT_VRAM)
        assert device.should_retry_on_cpu(error, device.DEVICE_CPU) is False
        assert device.cpu_retry_i18n_key(error, device.DEVICE_CPU) is None

    def test_a_device_that_was_never_decided_is_not_offered_either(self):
        # job.device is still None when the run fell before the device was
        # chosen — a backend that would not resolve, for instance.
        error = errors.TranscriptionError(errors.CUDA_UNAVAILABLE)
        assert device.should_retry_on_cpu(error, None) is False

    def test_something_that_is_not_an_error_answers_no_rather_than_raising(self):
        assert device.should_retry_on_cpu(None, device.DEVICE_CUDA) is False
        assert device.cpu_retry_i18n_key("insufficient_vram") is None

    def test_the_two_offers_do_not_say_the_same_thing(self):
        # Declining sends the user to different settings, so the two failures
        # cannot share one sentence.
        assert len(set(device.CPU_RETRY_I18N_KEYS.values())) == len(
            device.CPU_RETRY_I18N_KEYS
        )


class TestErrors:
    def test_the_exception_carries_the_code(self):
        exc = errors.TranscriptionError(errors.FFMPEG_FAILED, "ffmpeg exited with 1")
        assert exc.code == errors.FFMPEG_FAILED
        assert exc.i18n_key == "transcription_error_ffmpeg_failed"

    def test_the_technical_detail_is_optional(self):
        exc = errors.TranscriptionError(errors.CANCELLED)
        assert exc.detail is None
        assert str(exc) == errors.CANCELLED

    def test_the_detail_reaches_the_log_line(self):
        exc = errors.TranscriptionError(errors.BACKEND_ERROR, "CUDA out of memory")
        assert exc.log_line == f"{errors.BACKEND_ERROR}: CUDA out of memory"

    def test_neither_str_nor_args_leaks_the_detail(self):
        # wx.MessageBox(str(exc), ...) is an idiom this repository already uses
        # (client/ui/media_viewer.py), so whatever __str__ returns is one
        # careless handler away from a screen reader spelling out a file path.
        exc = errors.TranscriptionError(errors.BACKEND_ERROR, r"C:\Users\name\file.ogg")
        assert str(exc) == errors.BACKEND_ERROR
        assert "file.ogg" not in str(exc)
        assert exc.args == (errors.BACKEND_ERROR,)
        assert "file.ogg" not in "".join(str(arg) for arg in exc.args)

    def test_the_log_line_is_just_the_code_without_a_detail(self):
        assert errors.TranscriptionError(errors.CANCELLED).log_line == errors.CANCELLED

    def test_every_error_answers_moved_even_when_no_move_was_involved(self):
        # move_models() attaches the ids it managed to move before failing, but
        # only on the path that got that far. A caller reading exc.moved after a
        # move would hit AttributeError on every other failure of the same call
        # — the busy-folder one above all, which raises its own error.
        assert errors.TranscriptionError(errors.MODELS_BUSY).moved == ()
        assert errors.TranscriptionError(errors.BACKEND_ERROR, "detail").moved == ()

    def test_every_case_the_issue_names_has_a_code(self):
        for code in (
            errors.MODEL_NOT_INSTALLED, errors.MODEL_CORRUPTED, errors.NO_DISK_SPACE,
            errors.CUDA_UNAVAILABLE, errors.INSUFFICIENT_VRAM, errors.INSUFFICIENT_RAM,
            errors.UNSUPPORTED_AUDIO_FORMAT, errors.FFMPEG_FAILED,
            errors.AUDIO_INCOMPLETE, errors.MEDIA_NOT_DOWNLOADED, errors.CANCELLED,
            errors.SAVE_FAILED, errors.BACKEND_MISSING, errors.MODEL_DOWNLOAD_FAILED,
            errors.BACKEND_ERROR,
        ):
            assert code in errors.ERROR_CODES

    def test_codes_are_unique(self):
        assert len(set(errors.ERROR_CODES)) == len(errors.ERROR_CODES)

    @pytest.mark.parametrize("code", errors.ERROR_CODES)
    def test_every_code_maps_to_an_i18n_key(self, code):
        # A %TEMP% with a drive letter, pinned: TEMP_NO_DISK_SPACE picks its
        # sentence by that, and the machine running this may have none.
        key = errors.error_i18n_key(code, r"C:\Temp")
        assert key == f"transcription_error_{code}"
        assert errors.ERROR_I18N_KEYS[code] == key

    def test_an_unknown_code_resolves_to_the_internal_error_message(self):
        # I18n.t() falls back to the raw key name, so a made-up key would have
        # the screen reader read "transcription_error_whatever" aloud.
        assert errors.error_i18n_key("whatever") == errors.ERROR_I18N_KEYS[errors.BACKEND_ERROR]
        assert errors.TranscriptionError(None).i18n_key in errors.ERROR_I18N_KEYS.values()


class TestTheTempDiskIsNamed:
    """TEMP_NO_DISK_SPACE names the drive %TEMP% is on — never its folder,
    which carries the Windows user name."""

    def test_the_drive_letter_alone(self):
        assert errors.temp_drive(r"C:\Users\Ana Souza\AppData\Local\Temp") == "C:"
        assert errors.temp_drive(r"d:\tmp") == "D:"

    def test_a_network_temp_names_no_share(self):
        assert errors.temp_drive(r"\\servidor\ana.souza\temp") == ""

    def test_a_network_temp_gets_the_sentence_that_names_no_drive(self):
        """ "There is no free space on drive  to prepare..." is what one key
        with an empty field read out for a %TEMP% on a share."""
        share = r"\\servidor\ana.souza\temp"
        key = errors.error_i18n_key(errors.TEMP_NO_DISK_SPACE, share)
        assert key == errors.TEMP_NO_DISK_SPACE_UNNAMED_I18N_KEY
        assert errors.error_i18n_values(errors.TEMP_NO_DISK_SPACE, share) == {}
        assert errors.error_i18n_key(errors.TEMP_NO_DISK_SPACE, r"D:\tmp") == (
            errors.ERROR_I18N_KEYS[errors.TEMP_NO_DISK_SPACE])

    @pytest.mark.parametrize("locale", LOCALES)
    def test_both_temp_sentences_read_whole_in_every_language(self, locale):
        table = load_strings(locale)
        for temp_dir in (r"E:\Temp", r"\\srv\temp"):
            key = errors.error_i18n_key(errors.TEMP_NO_DISK_SPACE, temp_dir)
            sentence = table[key].format(
                **errors.error_i18n_values(errors.TEMP_NO_DISK_SPACE, temp_dir))
            assert sentence and "{" not in sentence and "  " not in sentence, locale
        assert "{" not in table[errors.TEMP_NO_DISK_SPACE_UNNAMED_I18N_KEY]

    def test_only_that_code_asks_for_it(self):
        values = errors.error_i18n_values(
            errors.TEMP_NO_DISK_SPACE, r"C:\Users\Ana Souza\AppData\Local\Temp")
        assert values == {"drive": "C:"}
        for code in errors.ERROR_CODES:
            if code != errors.TEMP_NO_DISK_SPACE:
                assert errors.error_i18n_values(code) == {}

    @pytest.mark.parametrize("locale", LOCALES)
    def test_every_error_sentence_formats_with_what_it_is_given(self, locale):
        """A field the sentence asks for and error_i18n_values() does not give
        is a KeyError in the middle of announcing the failure."""
        table = load_strings(locale)
        for code in errors.ERROR_CODES:
            text = table[errors.error_i18n_key(code, r"E:\Temp")]
            sentence = text.format(**errors.error_i18n_values(code, r"E:\Temp"))
            if code == errors.TEMP_NO_DISK_SPACE:
                assert "E:" in sentence, locale


class TestTranslations:
    """Same rule as tests/test_language_files_in_sync.py, aimed at these keys.

    The sync test would only notice a key missing from *some* locales; a key
    missing from all five agrees with itself perfectly and still reaches the
    user as its own name.
    """

    @pytest.mark.parametrize("locale", LOCALES)
    def test_every_error_code_is_translated_everywhere(self, locale):
        table = load_strings(locale)
        keys = [errors.error_i18n_key(code, r"C:\Temp") for code in errors.ERROR_CODES]
        keys.append(errors.TEMP_NO_DISK_SPACE_UNNAMED_I18N_KEY)
        missing = sorted(key for key in keys if key not in table)
        assert missing == [], f"{locale} catalog would speak these key names aloud: {missing}"

    @pytest.mark.parametrize("locale", LOCALES)
    def test_every_device_reason_is_translated_everywhere(self, locale):
        table = load_strings(locale)
        missing = sorted(
            key for key in device.DEVICE_REASON_I18N_KEYS.values() if key not in table
        )
        assert missing == [], f"{locale} catalog is missing device reasons: {missing}"

    @pytest.mark.parametrize("locale", LOCALES)
    def test_every_cpu_retry_offer_is_translated_everywhere(self, locale):
        table = load_strings(locale)
        missing = sorted(
            key for key in device.CPU_RETRY_I18N_KEYS.values() if key not in table
        )
        assert missing == [], f"{locale} catalog is missing retry offers: {missing}"

    @pytest.mark.parametrize("locale", LOCALES)
    def test_every_size_class_is_translated_everywhere(self, locale):
        table = load_strings(locale)
        missing = sorted(
            key for key in model_catalog.SIZE_CLASS_I18N_KEYS.values() if key not in table
        )
        assert missing == [], f"{locale} catalog is missing size labels: {missing}"

    @pytest.mark.parametrize("locale", LOCALES)
    def test_no_transcription_string_is_blank(self, locale):
        table = load_strings(locale)
        blank = sorted(
            key for key, text in table.items()
            if key.startswith("transcription_") and not text.strip()
        )
        assert blank == []

    def test_size_class_keys_cover_every_class_in_the_catalogue(self):
        for model in model_catalog.MODELS:
            assert model_catalog.size_class_i18n_key(model.size_class)

    def test_an_unknown_size_class_has_no_key(self):
        assert model_catalog.size_class_i18n_key("enormous") is None


def test_the_backend_is_never_imported_at_module_level():
    """The app has to start on a copy whose faster-whisper is missing or broken.

    The backend ships in requirements.txt, but a copy where it will not import
    must still open. If it is missing, the run falls back to whisper.cpp when
    its program is installed, or answers BACKEND_MISSING
    (backend.available_backend_ids(), which is_available()'s find_spec()
    feeds). If it is present but its DLLs will not load, is_available() cannot
    tell, and the first load answers BACKEND_MISSING
    (faster_whisper_backend._whisper_model_class()). Either way, a top-level
    import would take the conversation window down with it instead. Checked
    against the source rather than sys.modules, because probe_hardware() legally
    imports ctranslate2 inside a function and would mask the difference.
    """
    from core.transcription import __file__ as package_file

    package_dir = os.path.dirname(package_file)
    offenders = []
    for name in sorted(os.listdir(package_dir)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(package_dir, name), "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in tree.body:  # top level only — nested imports are the rule
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for imported in names:
                if imported.split(".")[0] in ("faster_whisper", "ctranslate2", "wx"):
                    offenders.append(f"{name}: {imported}")
    assert offenders == [], f"imported at module level: {offenders}"
