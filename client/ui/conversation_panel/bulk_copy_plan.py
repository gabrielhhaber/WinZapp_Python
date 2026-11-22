"""Pure rules for copying a selection that may hold files (no wx).

Ctrl+C / "Copy selected" on a selection used to take text messages only. A
selection can now also hold documents, photos, videos and audio, which go to
the clipboard as files (CF_HDROP) so one paste in Windows Explorer drops them
all. These helpers decide which message goes where and what the staged copies
are called; the clipboard itself is `bulk_copy_files.py`.
"""

from ui.conversation_panel.copy_target import FILE_COPY_TYPES

TEXT_COPY_TYPES = ("conversation", "extendedTextMessage")


def split_copy_selection(messages) -> "tuple[list, list]":
    """(text messages, file messages) from *messages*, each in the order given.

    Anything else (location, contact card, poll, ...) is in neither list: a
    mass copy has never carried those, same as the old text-only copy.
    """
    texts, files = [], []
    for msg in messages:
        msg_type = (msg or {}).get("messageType", "")
        if msg_type in TEXT_COPY_TYPES:
            texts.append(msg)
        elif msg_type in FILE_COPY_TYPES:
            files.append(msg)
    return texts, files


def unique_copy_name(name: str, used: set) -> str:
    """*name*, or "<base>_<n><ext>" when it collides (case-insensitively, as on
    Windows) with one already in *used*; the result is added to *used*.

    All staged files share one folder, so two documents both called
    "report.pdf" must not overwrite each other.
    """
    name = safe_file_name(name)
    base, ext = _split_ext(name)
    candidate = name
    n = 1
    while candidate.lower() in used:
        candidate = f"{base}_{n}{ext}"
        n += 1
    used.add(candidate.lower())
    return candidate


def safe_file_name(name: str) -> str:
    """*name* reduced to a plain file name: no folder part and none of the
    characters Windows refuses, so a media name from a message can never make
    the staged file land outside the temp folder."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join("_" if c in '<>:"|?*' or ord(c) < 32 else c for c in name)
    name = name.strip(" .")
    return name or "file"


def _split_ext(name: str) -> "tuple[str, str]":
    dot = name.rfind(".")
    if dot <= 0:
        return name, ""
    return name[:dot], name[dot:]
