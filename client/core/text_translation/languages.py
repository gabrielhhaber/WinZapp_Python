"""The languages a translation engine is offered, and each engine's code for them.

Named by their **endonym**, the name of the language in that language, like
the transcription's language picker (core/transcription/preferences.py):
translating 130 language names into every locale is strings nobody here can
review, and a user looking for their own language recognises "polski" in any
interface. The English name is kept beside it only so a target typed for an
AI provider ("Portuguese") still resolves when the engine changes.

The saved target stays the text the settings show (an endonym, or whatever was
typed for an AI provider); ``language_code()`` turns it into the code below,
and each engine maps that code to its own spelling:

* NLLB-200 uses ISO 639-3 plus script (``por_Latn``), checked against the
  model's own special tokens (special_tokens_map.json of
  OpenNMT/nllb-200-3.3B-ct2-int8, read 2026-11-17);
* M2M-100 and SMaLL-100 use their own 100 two-letter tokens (``__pt__``);
* mBART-50 uses 52 locale-like tokens (``pt_XX``);
* MADLAD-400 uses ``<2xx>`` tokens from its vocabulary (Nextcloud-AI's
  shared_vocabulary.json, same date);
* the services take ISO codes with a few spellings of their own.
"""

#: (code, endonym, English name, NLLB-200 code or None). Order is irrelevant:
#: choices() sorts by endonym and puts the interface language first.
LANGUAGES = (
    ("af", "Afrikaans", "Afrikaans", "afr_Latn"),
    ("ak", "Akan", "Akan", "aka_Latn"),
    ("am", "አማርኛ", "Amharic", "amh_Ethi"),
    ("ar", "العربية", "Arabic", "arb_Arab"),
    ("as", "অসমীয়া", "Assamese", "asm_Beng"),
    ("ast", "asturianu", "Asturian", "ast_Latn"),
    ("ay", "aymar aru", "Aymara", "ayr_Latn"),
    ("az", "azərbaycan dili", "Azerbaijani", "azj_Latn"),
    ("ba", "башҡорт теле", "Bashkir", "bak_Cyrl"),
    ("be", "беларуская", "Belarusian", "bel_Cyrl"),
    ("bg", "български", "Bulgarian", "bul_Cyrl"),
    ("bn", "বাংলা", "Bengali", "ben_Beng"),
    ("bo", "བོད་སྐད་", "Tibetan", "bod_Tibt"),
    ("br", "brezhoneg", "Breton", None),
    ("bs", "bosanski", "Bosnian", "bos_Latn"),
    ("ca", "català", "Catalan", "cat_Latn"),
    ("ceb", "Cebuano", "Cebuano", "ceb_Latn"),
    ("ckb", "کوردی", "Central Kurdish", "ckb_Arab"),
    ("co", "corsu", "Corsican", None),
    ("cs", "čeština", "Czech", "ces_Latn"),
    ("cy", "Cymraeg", "Welsh", "cym_Latn"),
    ("da", "dansk", "Danish", "dan_Latn"),
    ("de", "Deutsch", "German", "deu_Latn"),
    ("dv", "ދިވެހި", "Dhivehi", None),
    ("ee", "eʋegbe", "Ewe", "ewe_Latn"),
    ("el", "Ελληνικά", "Greek", "ell_Grek"),
    ("en", "English", "English", "eng_Latn"),
    ("eo", "Esperanto", "Esperanto", "epo_Latn"),
    ("es", "español", "Spanish", "spa_Latn"),
    ("et", "eesti", "Estonian", "est_Latn"),
    ("eu", "euskara", "Basque", "eus_Latn"),
    ("fa", "فارسی", "Persian", "pes_Arab"),
    ("ff", "Fulfulde", "Fulah", "fuv_Latn"),
    ("fi", "suomi", "Finnish", "fin_Latn"),
    ("fo", "føroyskt", "Faroese", "fao_Latn"),
    ("fr", "français", "French", "fra_Latn"),
    ("fy", "Frysk", "Western Frisian", None),
    ("ga", "Gaeilge", "Irish", "gle_Latn"),
    ("gd", "Gàidhlig", "Scottish Gaelic", "gla_Latn"),
    ("gl", "galego", "Galician", "glg_Latn"),
    ("gn", "avañeʼẽ", "Guarani", "grn_Latn"),
    ("gu", "ગુજરાતી", "Gujarati", "guj_Gujr"),
    ("ha", "Hausa", "Hausa", "hau_Latn"),
    ("haw", "ʻŌlelo Hawaiʻi", "Hawaiian", None),
    ("he", "עברית", "Hebrew", "heb_Hebr"),
    ("hi", "हिन्दी", "Hindi", "hin_Deva"),
    ("hr", "hrvatski", "Croatian", "hrv_Latn"),
    ("ht", "kreyòl ayisyen", "Haitian Creole", "hat_Latn"),
    ("hu", "magyar", "Hungarian", "hun_Latn"),
    ("hy", "հայերեն", "Armenian", "hye_Armn"),
    ("id", "Bahasa Indonesia", "Indonesian", "ind_Latn"),
    ("ig", "Igbo", "Igbo", "ibo_Latn"),
    ("ilo", "Ilokano", "Ilocano", "ilo_Latn"),
    ("is", "íslenska", "Icelandic", "isl_Latn"),
    ("it", "italiano", "Italian", "ita_Latn"),
    ("ja", "日本語", "Japanese", "jpn_Jpan"),
    ("jv", "basa Jawa", "Javanese", "jav_Latn"),
    ("ka", "ქართული", "Georgian", "kat_Geor"),
    ("kk", "қазақ тілі", "Kazakh", "kaz_Cyrl"),
    ("km", "ភាសាខ្មែរ", "Khmer", "khm_Khmr"),
    ("kn", "ಕನ್ನಡ", "Kannada", "kan_Knda"),
    ("ko", "한국어", "Korean", "kor_Hang"),
    ("ku", "kurdî", "Kurdish (Kurmanji)", "kmr_Latn"),
    ("ky", "кыргызча", "Kyrgyz", "kir_Cyrl"),
    ("la", "Latina", "Latin", None),
    ("lb", "Lëtzebuergesch", "Luxembourgish", "ltz_Latn"),
    ("lg", "Luganda", "Ganda", "lug_Latn"),
    ("ln", "lingála", "Lingala", "lin_Latn"),
    ("lo", "ລາວ", "Lao", "lao_Laoo"),
    ("lt", "lietuvių", "Lithuanian", "lit_Latn"),
    ("lv", "latviešu", "Latvian", "lvs_Latn"),
    ("mg", "Malagasy", "Malagasy", "plt_Latn"),
    ("mi", "te reo Māori", "Maori", "mri_Latn"),
    ("mk", "македонски", "Macedonian", "mkd_Cyrl"),
    ("ml", "മലയാളം", "Malayalam", "mal_Mlym"),
    ("mn", "монгол", "Mongolian", "khk_Cyrl"),
    ("mr", "मराठी", "Marathi", "mar_Deva"),
    ("ms", "Bahasa Melayu", "Malay", "zsm_Latn"),
    ("mt", "Malti", "Maltese", "mlt_Latn"),
    ("my", "မြန်မာဘာသာ", "Burmese", "mya_Mymr"),
    ("nb", "norsk bokmål", "Norwegian", "nob_Latn"),
    ("ne", "नेपाली", "Nepali", "npi_Deva"),
    ("nl", "Nederlands", "Dutch", "nld_Latn"),
    ("nn", "nynorsk", "Norwegian Nynorsk", "nno_Latn"),
    ("ny", "Chichewa", "Chichewa", "nya_Latn"),
    ("oc", "occitan", "Occitan", "oci_Latn"),
    ("om", "Afaan Oromoo", "Oromo", "gaz_Latn"),
    ("or", "ଓଡ଼ିଆ", "Odia", "ory_Orya"),
    ("pa", "ਪੰਜਾਬੀ", "Punjabi", "pan_Guru"),
    ("pl", "polski", "Polish", "pol_Latn"),
    ("ps", "پښتو", "Pashto", "pbt_Arab"),
    ("pt", "português (Brasil)", "Portuguese", "por_Latn"),
    ("pt-PT", "português (Portugal)", "European Portuguese", "por_Latn"),
    ("qu", "Runa Simi", "Quechua", "quy_Latn"),
    ("ro", "română", "Romanian", "ron_Latn"),
    ("ru", "русский", "Russian", "rus_Cyrl"),
    ("rw", "Kinyarwanda", "Kinyarwanda", "kin_Latn"),
    ("sa", "संस्कृतम्", "Sanskrit", "san_Deva"),
    ("sd", "سنڌي", "Sindhi", "snd_Arab"),
    ("si", "සිංහල", "Sinhala", "sin_Sinh"),
    ("sk", "slovenčina", "Slovak", "slk_Latn"),
    ("sl", "slovenščina", "Slovenian", "slv_Latn"),
    ("sm", "Gagana Samoa", "Samoan", "smo_Latn"),
    ("sn", "chiShona", "Shona", "sna_Latn"),
    ("so", "Soomaali", "Somali", "som_Latn"),
    ("sq", "shqip", "Albanian", "als_Latn"),
    ("sr", "српски", "Serbian", "srp_Cyrl"),
    ("st", "Sesotho", "Southern Sotho", "sot_Latn"),
    ("su", "basa Sunda", "Sundanese", "sun_Latn"),
    ("sv", "svenska", "Swedish", "swe_Latn"),
    ("sw", "Kiswahili", "Swahili", "swh_Latn"),
    ("ta", "தமிழ்", "Tamil", "tam_Taml"),
    ("te", "తెలుగు", "Telugu", "tel_Telu"),
    ("tg", "тоҷикӣ", "Tajik", "tgk_Cyrl"),
    ("th", "ไทย", "Thai", "tha_Thai"),
    ("ti", "ትግርኛ", "Tigrinya", "tir_Ethi"),
    ("tk", "türkmen dili", "Turkmen", "tuk_Latn"),
    ("tl", "Tagalog", "Tagalog", "tgl_Latn"),
    ("tn", "Setswana", "Tswana", "tsn_Latn"),
    ("tr", "Türkçe", "Turkish", "tur_Latn"),
    ("ts", "Xitsonga", "Tsonga", "tso_Latn"),
    ("tt", "татар теле", "Tatar", "tat_Cyrl"),
    ("ug", "ئۇيغۇرچە", "Uyghur", "uig_Arab"),
    ("uk", "українська", "Ukrainian", "ukr_Cyrl"),
    ("ur", "اردو", "Urdu", "urd_Arab"),
    ("uz", "oʻzbek", "Uzbek", "uzn_Latn"),
    ("vi", "Tiếng Việt", "Vietnamese", "vie_Latn"),
    ("wo", "Wolof", "Wolof", "wol_Latn"),
    ("xh", "isiXhosa", "Xhosa", "xho_Latn"),
    ("yi", "ייִדיש", "Yiddish", "ydd_Hebr"),
    ("yo", "Yorùbá", "Yoruba", "yor_Latn"),
    ("yue", "粵語", "Cantonese", "yue_Hant"),
    ("zh", "中文（简体）", "Chinese", "zho_Hans"),
    ("zh-TW", "中文（繁體）", "Traditional Chinese", "zho_Hant"),
    ("zu", "isiZulu", "Zulu", "zul_Latn"),
)

CODES = tuple(row[0] for row in LANGUAGES)
ENDONYMS = {code: endonym for code, endonym, _english, _nllb in LANGUAGES}
NLLB = {code: nllb for code, _endonym, _english, nllb in LANGUAGES if nllb}

#: The 100 language tokens of M2M-100 (and SMaLL-100, which shares its
#: vocabulary), from special_tokens_map.json of jncraton/m2m100_418M-ct2-int8.
M2M100_TOKENS = frozenset((
    "af am ar ast az ba be bg bn br bs ca ceb cs cy da de el en es et fa ff fi fr fy ga gd "
    "gl gu ha he hi hr ht hu hy id ig ilo is it ja jv ka kk km kn ko lb lg ln lo lt lv mg "
    "mk ml mn mr ms my ne nl no ns oc or pa pl ps pt ro ru sd si sk sl so sq sr ss su sv "
    "sw ta th tl tn tr uk ur uz vi wo xh yi yo zh zu").split())
#: Where an M2M-100 token is not the code above.
M2M100_OVERRIDES = {"nb": "no", "pt-PT": "pt"}

#: The MADLAD-400 ``<2xx>`` tokens for the languages above (its vocabulary has
#: some 490; only these can be chosen here).
MADLAD_TOKENS = frozenset((
    "af ak am ar as ay az ba be bg bn bo br bs ca ceb ckb co cs cy da de dv ee el en eo "
    "es et eu fa ff fi fil fo fr fy ga gd gl gn gu ha haw he hi hr ht hu hy id ig ilo is it "
    "ja jv ka kk km kn ko ku ky la lb lg ln lo lt lv mg mi mk ml mn mr ms mt my ne nl nn no "
    "ny oc om or pa pl ps pt qu ro ru rw sa sd si sk sl sm sn so sq sr st su sv sw ta te tg "
    "th ti tk tn tr ts tt ug uk ur uz vi wo xh yi yo zh zh_Hant zu").split())
MADLAD_OVERRIDES = {"nb": "no", "pt-PT": "pt", "tl": "fil", "zh-TW": "zh_Hant"}

#: The 52 language tokens of mBART-50 (special_tokens_map.json of
#: keisuke-miyako/mbart-large-50-many-to-many-mmt-ct2-int8), by code above.
MBART50_TOKENS = {token.split("_")[0]: token for token in (
    "ar_AR cs_CZ de_DE en_XX es_XX et_EE fi_FI fr_XX gu_IN hi_IN it_IT ja_XX kk_KZ ko_KR lt_LT "
    "lv_LV my_MM ne_NP nl_XX ro_RO ru_RU si_LK tr_TR vi_VN zh_CN af_ZA az_AZ bn_IN fa_IR he_IL "
    "hr_HR id_ID ka_GE km_KH mk_MK ml_IN mn_MN mr_IN pl_PL ps_AF pt_XX sv_SE sw_KE ta_IN te_IN "
    "th_TH tl_XX uk_UA ur_PK xh_ZA gl_ES sl_SI").split()}
MBART50_TOKENS["pt-PT"] = MBART50_TOKENS["pt"]

#: The interface locales (client/languages/language_map.json) whose code is
#: not a code above.
_LOCALE_CODES = {"pt-BR": "pt", "en-US": "en", "es-ES": "es", "tr-TR": "tr"}

#: What py3langid answers that is spelt differently above.
_DETECTED = {"no": "nb"}


def _key(text):
    return " ".join(str(text or "").replace("_", "-").split()).casefold()


def _lookup():
    table = {}
    for code, endonym, english, _nllb in LANGUAGES:
        for name in (english, endonym, code):
            table[_key(name)] = code
    return table


_BY_NAME = _lookup()


def language_code(value, extra_names=None):
    """The code of a saved target or source, or None.

    ``value`` is an endonym, a code, an English name or one of the interface
    language names (``extra_names``: core.i18n.LANGUAGE_NAMES, code -> name).
    """
    key = _key(value)
    if not key:
        return None
    if key in _BY_NAME:
        return _BY_NAME[key]
    for locale, name in (extra_names or {}).items():
        if key in (_key(name), _key(locale)):
            return interface_code(locale)
    return None


def interface_code(locale):
    """The code above for an interface locale ("pt-BR" -> "pt"), or None."""
    if not locale:
        return None
    locale = str(locale)
    if locale in _LOCALE_CODES:
        return _LOCALE_CODES[locale]
    if locale in ENDONYMS:
        return locale
    base = locale.replace("_", "-").split("-")[0].lower()
    return base if base in ENDONYMS else None


def detected_code(code):
    """A py3langid answer as a code above, or None."""
    code = _DETECTED.get(code, code)
    return code if code in ENDONYMS else None


def nllb_code(code):
    return NLLB.get(code)


def m2m100_code(code):
    token = M2M100_OVERRIDES.get(code, code)
    return token if token in M2M100_TOKENS else None


def mbart50_code(code):
    return MBART50_TOKENS.get(code)


def madlad_code(code):
    token = MADLAD_OVERRIDES.get(code, code)
    return token if token in MADLAD_TOKENS else None


def choices(supported=None, ui_language=""):
    """((code, endonym), ...) for a picker: the codes ``supported`` keeps
    (all of them when None), sorted by endonym, the interface language first."""
    rows = sorted(((code, name) for code, name in ENDONYMS.items()
                   if supported is None or supported(code)),
                  key=lambda row: row[1].casefold())
    own = interface_code(ui_language)
    first = [row for row in rows if row[0] == own]
    return tuple(first + [row for row in rows if row[0] != own])
