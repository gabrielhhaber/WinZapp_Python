"""Language codes accepted by Gemini Live Translate, displayed as autonyms."""

# https://ai.google.dev/gemini-api/docs/live-api/live-translate#supported-languages
LANGUAGES = {
    "af": "Afrikaans", "ak": "Akan", "sq": "Shqip", "am": "አማርኛ", "ar": "العربية",
    "hy": "Հայերեն", "az": "Azərbaycanca", "eu": "Euskara", "be": "Беларуская",
    "bn": "বাংলা", "bg": "Български", "my": "မြန်မာ", "ca": "Català",
    "zh-Hans": "简体中文", "zh-Hant": "繁體中文", "hr": "Hrvatski", "cs": "Čeština",
    "da": "Dansk", "nl": "Nederlands", "en": "English", "et": "Eesti",
    "fil": "Filipino", "fi": "Suomi", "fr": "Français", "gl": "Galego", "ka": "ქართული",
    "de": "Deutsch", "el": "Ελληνικά", "gu": "ગુજરાતી", "ha": "Hausa", "he": "עברית",
    "hi": "हिन्दी", "hu": "Magyar", "is": "Íslenska", "id": "Bahasa Indonesia",
    "it": "Italiano", "ja": "日本語", "jv": "Basa Jawa", "kn": "ಕನ್ನಡ", "kk": "Қазақша",
    "km": "ខ្មែរ", "rw": "Ikinyarwanda", "ko": "한국어", "lo": "ລາວ", "lv": "Latviešu",
    "lt": "Lietuvių", "mk": "Македонски", "ms": "Bahasa Melayu", "ml": "മലയാളം",
    "mr": "मराठी", "mn": "Монгол", "ne": "नेपाली", "no": "Norsk", "fa": "فارسی",
    "pl": "Polski", "pt-BR": "Português (Brasil)", "pt-PT": "Português (Portugal)",
    "pa": "ਪੰਜਾਬੀ", "ro": "Română", "ru": "Русский", "sr": "Српски", "sd": "سنڌي",
    "si": "සිංහල", "sk": "Slovenčina", "sl": "Slovenščina", "es": "Español",
    "su": "Basa Sunda", "sw": "Kiswahili", "sv": "Svenska", "ta": "தமிழ்",
    "te": "తెలుగు", "th": "ไทย", "tr": "Türkçe", "uk": "Українська", "ur": "اردو",
    "uz": "Oʻzbekcha", "vi": "Tiếng Việt", "zu": "IsiZulu",
}


def language_defaults(saved, locale):
    saved = saved if isinstance(saved, dict) else {}
    default = locale if locale in LANGUAGES else locale.split("-", 1)[0]
    mine = saved.get("mine")
    peer = saved.get("peer")
    return (mine if isinstance(mine, str) and mine in LANGUAGES
            else default if default in LANGUAGES else "en",
            peer if isinstance(peer, str) and peer in LANGUAGES else "",
            saved.get("duck") is True)
