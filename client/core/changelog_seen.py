"""The release notes as the user last saw them, for Help > "What's new since
you last looked".

One file for the whole install, in the global data folder: news is about
WinZapp, not about an account, so whichever account looks, every account has
seen it. It keeps the text of every changelog that ships at that moment, not
only the language being read, so that changing the language later still
compares like with like instead of reporting the whole file as new.

Written whole, through a temporary file and os.replace(): two account windows
looking at once leave one complete snapshot, never a torn one.

No wx: the window is main_window/whats_new_since.py.
"""

import json
import logging
import os
import tempfile
import time

from core import changelog_diff

FILE_NAME = "changelog_seen.json"


def read(path: str):
    """{"texts": {locale: text}, "seen_at": float, "version": str}, or None
    when there is none or it cannot be read (then this is a first look)."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logging.warning("[whats-new] the last-seen notes could not be read: %s",
                        type(exc).__name__)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("texts"), dict):
        return None
    return data


def write(path: str, texts: dict, version: str, now=None) -> bool:
    """Keep `texts` as what the user has now seen. Whether it was written."""
    data = {"texts": {k: v for k, v in texts.items() if isinstance(v, str)},
            "seen_at": float(time.time() if now is None else now),
            "version": str(version)}
    folder = os.path.dirname(path) or "."
    try:
        os.makedirs(folder, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".changelog_seen-", dir=folder)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(data, out, ensure_ascii=False)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return True
    except OSError as exc:
        logging.warning("[whats-new] the last-seen notes could not be written: %s",
                        type(exc).__name__)
        return False


def text_for(previous, current: str, seen_on: str, version: str, t) -> str:
    """What the window says. `previous` is the text last seen (None for a
    first look), `seen_on` that moment written for the user, `version` the
    running one, `t` I18n.t.

    A first look has nothing to compare with: it shows the version in use,
    which is what "new" means to someone who never looked."""
    if previous is None:
        newest = changelog_diff.first_version(current)
        body = newest.text if newest is not None else current.strip()
        return t("whats_new_since_first").format(version=version) + "\n\n" + body
    described = changelog_diff.describe(*changelog_diff.compare(previous, current), t)
    if not described:
        return t("whats_new_since_nothing").format(date=seen_on)
    return t("whats_new_since_header").format(date=seen_on) + "\n\n" + described


def previous_text(snapshot, locale: str):
    """The text of `locale` the user last saw, or None (never looked, or not
    in that language: a language added since)."""
    if not snapshot:
        return None
    text = snapshot["texts"].get(locale)
    return text if isinstance(text, str) else None
