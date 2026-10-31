"""The registered locale set and authoritative PO readers for tests.

Not a test module — a helper the locale-parametrized tests derive from, so the
derivation and the check that it produced anything live in one place.

Why derive at all: the set of locales is data, not code. A locale is added by
adding a PO catalog plus an entry in that map, with no rebuild
(CLAUDE.md, "Paths, config, i18n"), so a list written out inside a test goes
stale the moment one is added and silently stops checking it. That already
happened — `pl` was missing from the hand-written lists in test_mute.py and
test_self_reference_label.py, so Polish was never checked by either.

Why the check belongs here rather than in each caller: deriving the list
introduces a second failure mode in exchange. A hand-written list is at least
never empty; a derived one is empty if the map is unreadable in a way json
tolerates (`{}`, or a reshape into a list of objects). Every caller uses it to
drive a loop or a `parametrize`, and an empty sequence makes those pass with
zero iterations — a suite that checks nothing and still goes green, which is
the exact failure the derivation was meant to prevent, moved one level down.

Raising here means a caller cannot obtain the list without the check having
run: it surfaces as a collection error naming this file, before any test in
any of those modules reports success.
"""

import functools
import json
from pathlib import Path

import polib

LANGUAGES_DIR = Path(__file__).resolve().parents[1] / "client" / "languages"
LANGUAGE_MAP = LANGUAGES_DIR / "language_map.json"
CATALOGS_DIR = Path(__file__).resolve().parents[1] / "translations"

#: The locale WinZapp falls back to, and the one guaranteed to be complete.
#: Its presence doubles as a shape check: a map that parsed but lost its
#: `{code: display name}` form (a list of objects, say) yields keys that are
#: not locale codes at all, and this is what notices.
DEFAULT_LOCALE = "pt-BR"


def registered_locale_codes() -> tuple[str, ...]:
    """Every locale code in language_map.json, sorted.

    Raises rather than returning something empty — see the module docstring.
    """
    codes = tuple(sorted(json.loads(LANGUAGE_MAP.read_text(encoding="utf-8"))))
    if not codes:
        raise AssertionError(
            f"{LANGUAGE_MAP} registers no locales, so every test deriving its "
            f"locale list from it would run zero cases and still pass."
        )
    if DEFAULT_LOCALE not in codes:
        raise AssertionError(
            f"{LANGUAGE_MAP} does not register {DEFAULT_LOCALE!r}, the locale the "
            f"app defaults to. Either it was removed — which breaks the fallback "
            f"i18n.t() relies on — or the file is no longer a {{code: name}} map. "
            f"Got: {list(codes)}"
        )
    return codes


def load_strings(locale: str) -> dict[str, str]:
    """Active stable-key translations from the authoritative PO catalog.

    A copy of one parse per locale: parsing a catalog takes a noticeable part
    of a second, and many tests ask for one in every stub they build.
    """
    return dict(_parsed_strings(locale))


@functools.lru_cache(maxsize=None)
def _parsed_strings(locale: str) -> dict[str, str]:
    path = CATALOGS_DIR / locale / "LC_MESSAGES" / "winzapp.po"
    return {
        entry.msgctxt: entry.msgstr
        for entry in polib.pofile(str(path), encoding="utf-8", check_for_duplicates=True)
        if entry.msgctxt and not entry.obsolete
    }
