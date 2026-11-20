"""The offline translation models WinZapp offers, as data.

Same contract as the Whisper catalogue (core/transcription/model_catalog.py):
a model is not in the installer, it is fetched from Hugging Face at a pinned
revision, every file at its exact size, and every LFS file (the weights and
the tokenizer) checked against its SHA-256 as it streams in. The download,
resume, lock and free-space gate are the transcription model store's
(``local_store``), not a second copy. Revisions, sizes and digests were read
from the Hugging Face API on 2026-11-17.

All of them are CTranslate2 conversions, so they run on the ctranslate2 that
already ships for faster-whisper. Five families, each tokenised its own way
(``local_engine``):

* **NLLB-200** (Meta, CC BY-NC 4.0, 200 languages): ``tokenizer.json`` through
  the ``tokenizers`` package; needs the source language.
* **M2M-100** (Meta, MIT, 100 languages): SentencePiece; needs the source
  language.
* **SMaLL-100** (MIT, the same 100 languages, a distilled M2M-100): only the
  target language, at the head of the source.
* **mBART-50** (Meta, MIT, 52 languages): SentencePiece (``tokenizer.model``);
  needs the source language, like NLLB-200.
* **MADLAD-400** (Google, Apache 2.0, about 400 languages): only the target
  language, a ``<2xx>`` token; ``tokenizer.json``.

The largest (M2M-100 12B, MADLAD-400 10B) need about as much memory as their
download; the list says the size so the user can judge.

Only the files listed are fetched: some repositories also hold a README, a
second tokenizer or a safetensors index nothing here reads.

Publishers' and models' names are proper names, shown as they are; the licence
is shown beside the name because NLLB-200's forbids commercial use.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FAMILY_NLLB = "nllb"
FAMILY_M2M100 = "m2m100"
FAMILY_SMALL100 = "small100"
FAMILY_MADLAD = "madlad"
FAMILY_MBART50 = "mbart50"

_NLLB_TOKENIZER = ("tokenizer.json", 17_331_176)
_NLLB_TOKENIZER_SHA = "e316b82de11d0f951f370943b3c438311629547285129b0b81dadabd01bca665"
_M2M_SPM = ("sentencepiece.bpe.model", 2_423_393)
_M2M_SPM_SHA = "d8f7c76ed2a5e0822be39f0a4f95a55eb19c78f4593ce609e2edbc2aea4d380a"
_MADLAD_TOKENIZER = ("tokenizer.json", 16_629_031)
_MADLAD_TOKENIZER_SHA = "a2799ccc696b752ba00c34f58726bfe253a04921ceb6cfc620400f560474790b"


@dataclass(frozen=True)
class TranslationModel:
    """One downloadable CTranslate2 translation model.

    Duck-types what core/transcription/model_store reads of a catalogue
    entry: ``id``, ``repo``, ``revision``, ``files``, ``download_bytes`` and
    ``sha256_of()``.
    """

    id: str
    name: str
    family: str
    publisher: str
    license: str
    languages: int
    repo: str
    revision: str
    files: tuple[tuple[str, int], ...]
    digests: dict = field(default_factory=dict, hash=False, compare=False)

    @property
    def download_bytes(self) -> int:
        return sum(size for _name, size in self.files)

    def sha256_of(self, name):
        return self.digests.get(name)

    @property
    def tokenizer_file(self):
        return next(name for name, _size in self.files
                    if name in ("tokenizer.json", "sentencepiece.bpe.model", "tokenizer.model"))

    @property
    def needs_source(self) -> bool:
        """Whether the model is told the source language (detected or chosen)."""
        return self.family in (FAMILY_NLLB, FAMILY_M2M100, FAMILY_MBART50)


MODELS = (
    TranslationModel(
        id="small100-int8", name="SMaLL-100", family=FAMILY_SMALL100,
        publisher="solavr", license="MIT", languages=100,
        repo="solavr/small100-ctranslate2-int8",
        revision="3c5af8e17d5c4f35bda849b7ffceaa1dfc62eb84",
        files=(("config.json", 233), ("model.bin", 338_727_246),
               ("shared_vocabulary.json", 2_924_622), _M2M_SPM),
        digests={"model.bin": "24e8b51720b7267cfd8faacedce50128bf11c4327f039632a494deb5e1c421f6",
                 _M2M_SPM[0]: _M2M_SPM_SHA},
    ),
    TranslationModel(
        id="m2m100-418m-int8", name="M2M-100 418M", family=FAMILY_M2M100,
        publisher="Meta", license="MIT", languages=100,
        repo="jncraton/m2m100_418M-ct2-int8",
        revision="7c1b2620a4e58dacecbd8bf89cfd6da7eb9eb7b0",
        files=(("config.json", 189), ("model.bin", 490_667_713),
               ("shared_vocabulary.json", 2_796_509), _M2M_SPM),
        digests={"model.bin": "d6703dd9f920ff896e45c3d97b490761bed5944937b90bbe6a7245f5652542d4",
                 _M2M_SPM[0]: _M2M_SPM_SHA},
    ),
    TranslationModel(
        id="mbart-50-int8", name="mBART-50", family=FAMILY_MBART50,
        publisher="Meta", license="MIT", languages=52,
        repo="keisuke-miyako/mbart-large-50-many-to-many-mmt-ct2-int8",
        revision="df3be3189592d0fd6946befc8f27210d2f5f33fb",
        files=(("config.json", 1651), ("model.bin", 620_482_694),
               ("shared_vocabulary.json", 6_740_284), ("tokenizer.model", 5_069_051)),
        digests={"model.bin": "7f239bf7cec2dcf41bbf7e54baf997f21af16f62c658e14dd8e3b9d35275039f",
                 "tokenizer.model":
                     "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865"},
    ),
    TranslationModel(
        id="nllb-200-distilled-600m-int8", name="NLLB-200 distilled 600M", family=FAMILY_NLLB,
        publisher="Meta", license="CC BY-NC 4.0", languages=200,
        repo="JustFrederik/nllb-200-distilled-600M-ct2-int8",
        revision="302d78f00e6fdb50a1064059df7c392b735e9d05",
        files=(("config.json", 159), ("model.bin", 622_595_991),
               ("shared_vocabulary.txt", 2_568_098), _NLLB_TOKENIZER),
        digests={"model.bin": "ed1beaf75134de7505315a5223162f56acff397eff6b50638a500d3936fe707b",
                 _NLLB_TOKENIZER[0]: _NLLB_TOKENIZER_SHA},
    ),
    TranslationModel(
        id="m2m100-1.2b-int8", name="M2M-100 1.2B", family=FAMILY_M2M100,
        publisher="Meta", license="MIT", languages=100,
        repo="jncraton/m2m100_1.2B-ct2-int8",
        revision="e50078df6be13a88592b70ea42f4d74c2082e448",
        files=(("config.json", 189), ("model.bin", 1_249_655_149),
               ("shared_vocabulary.json", 2_796_509), _M2M_SPM),
        digests={"model.bin": "c97df052a558895317312470e1ff7cb8eae5416f7ae16214a2983c6853dd3ce5",
                 _M2M_SPM[0]: _M2M_SPM_SHA},
    ),
    TranslationModel(
        id="nllb-200-distilled-1.3b-int8", name="NLLB-200 distilled 1.3B", family=FAMILY_NLLB,
        publisher="Meta", license="CC BY-NC 4.0", languages=200,
        repo="OpenNMT/nllb-200-distilled-1.3B-ct2-int8",
        revision="70f572adafa4794890ce7826156a4209717855af",
        files=(("config.json", 1071), ("model.bin", 1_377_755_738),
               ("shared_vocabulary.json", 5_921_176), _NLLB_TOKENIZER),
        digests={"model.bin": "89dc4b9eb7f4dcd3ff36023c9b1c55fcecb47ddc0df97ab31b068ef9cf194940",
                 _NLLB_TOKENIZER[0]: _NLLB_TOKENIZER_SHA},
    ),
    TranslationModel(
        id="nllb-200-1.3b-int8", name="NLLB-200 1.3B", family=FAMILY_NLLB,
        publisher="Meta", license="CC BY-NC 4.0", languages=200,
        repo="JustFrederik/nllb-200-1.3B-ct2-int8",
        revision="1bc38c391686836b5f0e4a7baaecafd58659a881",
        files=(("config.json", 159), ("model.bin", 1_381_827_087),
               ("shared_vocabulary.txt", 2_568_098), _NLLB_TOKENIZER),
        digests={"model.bin": "45f1c00ec0c4ffc7f0342e0b49524424674f28b1acdb5d5ff098f9af10498e6e",
                 _NLLB_TOKENIZER[0]: _NLLB_TOKENIZER_SHA},
    ),
    TranslationModel(
        id="madlad400-3b-int8", name="MADLAD-400 3B", family=FAMILY_MADLAD,
        publisher="Google", license="Apache 2.0", languages=400,
        repo="Nextcloud-AI/madlad400-3b-mt-ct2-int8",
        revision="aa32bbdeba7880eff2096ec044cb155a340a9400",
        files=(("config.json", 224), ("model.bin", 2_950_208_329),
               ("shared_vocabulary.json", 5_477_099), _MADLAD_TOKENIZER),
        digests={"model.bin": "77b9fd9ab97c1259d07089b5f854393dad81bc5fb5647d3f9a5d101c94f40daa",
                 _MADLAD_TOKENIZER[0]: _MADLAD_TOKENIZER_SHA},
    ),
    TranslationModel(
        id="nllb-200-3.3b-int8", name="NLLB-200 3.3B", family=FAMILY_NLLB,
        publisher="Meta", license="CC BY-NC 4.0", languages=200,
        repo="OpenNMT/nllb-200-3.3B-ct2-int8",
        revision="28d998cc8548ad62f4f98cf8860928c3af99b97b",
        files=(("config.json", 1071), ("model.bin", 3_356_047_962),
               ("shared_vocabulary.json", 5_921_176), _NLLB_TOKENIZER),
        digests={"model.bin": "b7540c4e19aa2a5079c89241ffccb49711705ca8b1d255cea00c3216dc7d5165",
                 _NLLB_TOKENIZER[0]: _NLLB_TOKENIZER_SHA},
    ),
    TranslationModel(
        id="madlad400-7b-bt-int8", name="MADLAD-400 7B", family=FAMILY_MADLAD,
        publisher="Google", license="Apache 2.0", languages=400,
        repo="Nextcloud-AI/madlad400-7b-mt-bt-ct2-int8",
        revision="35c8ee23db1906ef28970fe7e6e9c4dfbe8a39d7",
        files=(("config.json", 224), ("model.bin", 8_312_279_065),
               ("shared_vocabulary.json", 5_477_099), _MADLAD_TOKENIZER),
        digests={"model.bin": "028675a47ec8a287161d24c01a62796595124d48e9972eacba4b08150ef09ba5",
                 _MADLAD_TOKENIZER[0]: _MADLAD_TOKENIZER_SHA},
    ),
    TranslationModel(
        id="madlad400-10b-int8", name="MADLAD-400 10B", family=FAMILY_MADLAD,
        publisher="Google", license="Apache 2.0", languages=400,
        repo="rei-ma/madlad400-10b-mt-ct2-int8",
        revision="9373418af3e3689df55aee4fcc18dfce48b7f3b8",
        files=(("config.json", 224), ("model.bin", 10_729_462_596),
               ("shared_vocabulary.json", 5_477_099), _MADLAD_TOKENIZER),
        digests={"model.bin": "76d5f8f324e62b523e70221f1d129a86b9d24c979aba5d30c499f700d47df060",
                 _MADLAD_TOKENIZER[0]: _MADLAD_TOKENIZER_SHA},
    ),
    TranslationModel(
        id="m2m100-12b-int8", name="M2M-100 12B", family=FAMILY_M2M100,
        publisher="Meta", license="MIT", languages=100,
        repo="Abu-Dju/m2m100-12B-last-ckpt-ct2-int8_float16",
        revision="d08dfb474f4bdde73b92ef756f0fef1e859fb039",
        files=(("config.json", 223), ("model.bin", 11_822_465_450),
               ("shared_vocabulary.json", 2_796_509), _M2M_SPM),
        digests={"model.bin": "6c643332965306e1cbf40bf659ed5552ddb170b327852125f9d16ee9ff2a49e8",
                 _M2M_SPM[0]: _M2M_SPM_SHA},
    ),
)

#: The model offered first: the smallest NLLB-200, the best quality for its
#: size in the catalogue.
DEFAULT_MODEL_ID = "nllb-200-distilled-600m-int8"

_BY_ID = {model.id: model for model in MODELS}


def get_model(model_id):
    return _BY_ID.get(model_id)
