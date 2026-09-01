import os
import re
import sys
import copy
import json
import base64
import unicodedata
import requests
from cryptography.fernet import Fernet


# How much Unicode folding searching applies, in the order the Settings radio
# group offers them. Stored in settings as one of these strings.
SEARCH_NORMALIZATION_MODES = ("off", "nfd", "nfkd")


#: Where a duration WinZapp measured from the media file itself is kept, apart
#: from the "seconds" the message arrived with. Two reasons for its own key:
#: a resync overwrites "seconds" with the server's copy and must not touch
#: this, and only a measured value can legitimately be 0 (see video_seconds).
MEASURED_SECONDS_KEY = "_measured_seconds"


def video_seconds(video: dict):
    """A video's length in whole seconds, or None when it isn't known.

    Two sources, in order:

    1. ``_measured_seconds`` — what WinZapp read off the media file itself
       (see ui/conversations.probe_media_duration). This is the only place a
       0 is believed: the file said so, and a clip under a second really does
       round to 0 there. WhatsApp shows "0:00" for such a thing, so we do too.

    2. ``seconds`` — what the message arrived with, and only when above zero.
       WhatsApp Web hands over duration 0 whenever the sending client left the
       field out of the message (confirmed against /get-messages for such a
       video: `duration` is the string "0", and no other field on the payload
       or its mediaData carries the real length). No video lasts no time, so
       announcing "duração: 0 segundos" from that states a length that is
       certainly wrong — reported as exactly that on a video that plays for
       minutes.

    So a stated 0 means "not stated", a measured 0 means "really that short",
    and the two are no longer the same answer. Audio never needed the
    distinction: a voice note under a second genuinely reports its own 0, and
    that path keeps treating it as a real value.

    Either value may arrive as an int or as a string depending on which layer
    normalized it (WebSocketClient._media_seconds() casts, a record restored
    straight from a REST sync may not) — "0" is truthy, so this cannot be a
    plain falsiness check at the call site.
    """
    if not isinstance(video, dict):
        return None
    measured = _whole_seconds(video.get(MEASURED_SECONDS_KEY))
    if measured is not None and measured >= 0:
        return measured
    stated = _whole_seconds(video.get("seconds"))
    return stated if stated is not None and stated > 0 else None


def _whole_seconds(raw):
    """*raw* as a whole number of seconds, or None if it isn't a number."""
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def carry_over_video_durations(new_msgs, old_msgs) -> int:
    """Copy a measured video duration from *old_msgs* onto the matching
    message in *new_msgs* whenever the new copy states none. Returns how many
    were carried over.

    A resync replaces the in-memory records of a chat with the server's copy,
    and the server never learns a duration WinZapp measured from the file
    itself (see ui/conversations.probe_media_duration) — so without this the
    length vanished from the list the moment any sync ran, and only came back
    after the video was played again. The database side of the same rule lives
    in DatabaseManager._with_known_video_duration(); this one keeps what is on
    screen right now in step with it.

    A message's video is immutable — WhatsApp has no "edit the media of a sent
    message" — so a duration measured once stays valid for that message id
    forever.
    Only the measured value travels: "seconds" is the server's own field and
    the incoming copy's is authoritative for it, whatever it says.
    """
    known = {}
    for m in old_msgs or ():
        if not isinstance(m, dict):
            continue
        mid = (m.get("key") or {}).get("id")
        video = (m.get("message") or {}).get("videoMessage")
        if not isinstance(video, dict):
            continue
        measured = _whole_seconds(video.get(MEASURED_SECONDS_KEY))
        if mid and measured is not None and measured >= 0:
            known[mid] = measured
    if not known:
        return 0
    carried = 0
    for m in new_msgs or ():
        if not isinstance(m, dict):
            continue
        video = (m.get("message") or {}).get("videoMessage")
        if not isinstance(video, dict):
            continue
        if _whole_seconds(video.get(MEASURED_SECONDS_KEY)) is not None:
            continue
        measured = known.get((m.get("key") or {}).get("id"))
        if measured is not None:
            video[MEASURED_SECONDS_KEY] = measured
            carried += 1
    return carried


def search_normalization_mode(value) -> str:
    """Canonicalize whatever settings.json holds into one of the three modes.

    Anything unrecognised — a missing key, a typo, a hand-edited file, a value
    from a newer version — reads as "off", which is the mode that cannot
    surprise anyone: searching then behaves exactly as it always did.
    """
    mode = str(value).strip().lower() if value is not None else ""
    return mode if mode in SEARCH_NORMALIZATION_MODES else "off"


def normalize_for_search(text: str, mode="off") -> str:
    """Prepare *text* for a case-insensitive substring search.

    ``off`` is plain ``.lower()`` — byte for byte the behaviour every search
    in the app had before this setting existed, so the default changes
    nothing at all.

    ``nfd`` additionally drops diacritics (decompose, then discard combining
    marks), so "reuniao" finds "reunião" and "acao" finds "ação" — what a
    user typing without accents actually wants.

    ``nfkd`` folds the accents too, and on top of that the *compatibility*
    forms: the ligature "ﬁ" matches "fi", "½" matches "1⁄2", superscripts
    match plain digits, and full-width characters match their ASCII
    equivalents. Useful against text pasted from PDFs, spreadsheets and CJK
    input methods, where those forms turn up without the author ever having
    typed them deliberately.

    Folding is offered rather than imposed because it is not free: in
    languages where an accent changes the word (Spanish "año"/"ano", French
    "sur"/"sûr") the exact one becomes unsearchable, and NFKD goes further
    still by erasing distinctions that are sometimes meaningful. Which trade
    is worth making is the user's call, not this function's.

    Known limit of both folding modes, worth knowing before promising too
    much: only marks that decomposition actually separates from their base
    letter are folded (á, ç, ñ, ś, ż...). Letters written with a stroke or
    slash are single indivisible codepoints with no decomposition — Polish
    "ł", Danish/Norwegian "ø", Croatian "đ" — so "lodka" still will not find
    "łódka" (the ó folds, the ł does not). Handling those needs a
    transliteration table, a much larger promise than "ignore the accents I
    did not type".
    """
    text = (text or "").lower()
    mode = search_normalization_mode(mode)
    if mode == "off":
        return text
    form = "NFD" if mode == "nfd" else "NFKD"
    return "".join(
        ch for ch in unicodedata.normalize(form, text)
        if not unicodedata.combining(ch)
    )


_UNICODE_LINE_SEPARATORS = {
    "\u2028",  # LINE SEPARATOR
    "\u2029",  # PARAGRAPH SEPARATOR
    "\u0085",  # NEXT LINE (NEL)
    "\x0b",    # VERTICAL TAB
    "\x0c",    # FORM FEED
    "\r",      # lone CR (CRLF handled below, before this set applies)
}


def normalize_line_separators(text) -> str:
    """Collapse every Unicode line/paragraph separator into plain ``\\n``.

    Rich clipboard sources — Google Docs, Word, websites, Apple apps — copy
    U+2028 LINE SEPARATOR / U+2029 PARAGRAPH SEPARATOR (and the rarer NEL,
    VT, FF) where a plain editor stores ``\\n``. A ``wx.TextCtrl`` keeps them
    verbatim: the native control does not render them as breaks (a paste
    looks like a single line, and NVDA reads it as one), yet WhatsApp
    renders U+2029 as a paragraph break on the receiving side. The result is
    the classic "it looks fine here but arrives full of weird breaks"
    report. Normalizing to ``\\n`` makes the field, the screen reader and
    the recipient all agree on the same line structure.
    """
    text = (text or "").replace("\r\n", "\n")
    for sep in _UNICODE_LINE_SEPARATORS:
        text = text.replace(sep, "\n")
    return text


_FORWARDABLE_SUB_KEYS = (
    "extendedTextMessage", "audioMessage", "imageMessage",
    "videoMessage", "documentMessage", "stickerMessage",
    "locationMessage", "contactMessage", "buttonsMessage",
    "listMessage",
)


def is_message_forwarded(msg) -> bool:
    """True when contextInfo.isForwarded is set — a real WhatsApp protocol
    field present on any forwarded message, from anyone, not only ones this
    app itself forwarded (WebSocketClient._normalize_wpp_message threads it
    through from WPPConnect's own Message.isForwarded).

    Shared by ui/conversations.py (to skip offering forward-related actions
    it doesn't apply to) and main.py's on_new_message() (to make sure a
    forwarded copy's own contextInfo/key fields — which can carry residual
    provenance about whoever originally sent the message being forwarded —
    are never mistaken for identifying WHO SENT THIS COPY)."""
    if not isinstance(msg, dict):
        return False
    top_ctx = msg.get("contextInfo")
    if isinstance(top_ctx, dict) and top_ctx.get("isForwarded"):
        return True
    msg_obj = msg.get("message") or {}
    if not isinstance(msg_obj, dict):
        return False
    for sub_key in _FORWARDABLE_SUB_KEYS:
        sub = msg_obj.get(sub_key)
        if isinstance(sub, dict) and isinstance(sub.get("contextInfo"), dict):
            if sub["contextInfo"].get("isForwarded"):
                return True
    return False


def is_voice_message(msg) -> bool:
    """Return True if msg is a voice note (PTT / mensagem de voz), not a generic audio file."""
    if not isinstance(msg, dict):
        return False
    if msg.get("_is_voice_recording") or msg.get("type") == "ptt":
        return True
    if msg.get("isPtt") or msg.get("ptt"):
        return True
    msg_type = msg.get("messageType") or msg.get("type")
    if msg_type not in ("audioMessage", "audio", "ptt"):
        return False
    if msg_type == "ptt":
        # "ptt" IS the voice-note type — the name says so. It used to fall
        # through to the ptt-flag check below, which a record carrying only
        # the type (no inner audioMessage) failed, reporting a voice note as a
        # plain audio file. Harmless while both were one category; not once
        # they are two.
        return True
    msg_obj = msg.get("message")
    inner = (msg_obj.get("audioMessage") or {}) if isinstance(msg_obj, dict) else {}
    if not inner and isinstance(msg.get("audioMessage"), dict):
        inner = msg.get("audioMessage") or {}
    media_data = msg.get("mediaData") if isinstance(msg.get("mediaData"), dict) else {}
    return bool(
        inner.get("ptt", False)
        or inner.get("isPtt", False)
        or media_data.get("ptt", False)
        or media_data.get("isPtt", False)
    )


# The media categories the group data dialog's Media tab filters by, and that
# Settings > Interface do usuario lets the user pre-select. Order is the order
# the checkboxes are shown in, and the keys are what gets persisted in
# user_interface.group_media_default_types - so renaming one silently drops a
# user's saved choice, exactly like SOUND_EVENTS' keys.
#
# "audios" and "voice_messages" are two categories, not one. They used to be
# one — deliberately, on the reasoning that both are "the audio someone sent"
# — but users asked for the split: they want the Media tab to show only voice
# notes (or only music/audio files), and they want to choose whether the
# automatic download fetches one, the other or both. is_voice_message() is the
# single test that separates them, here as everywhere else.
#
# The risk the old grouping was avoiding is real and is handled by a migration
# rather than by the grouping: a settings.json written before this split has
# "audios" saved and cannot have "voice_messages", so read literally it would
# leave every existing user's voice notes unchecked — invisible in the Media
# tab and never auto-downloaded — silently, on the first launch after the
# update. migrate_voice_messages_media_types() below inherits the "audios"
# state into the new key once, at settings-load time.
GROUP_MEDIA_TYPES = ("photos", "videos", "audios", "voice_messages",
                     "documents", "links")

_GROUP_MEDIA_MESSAGE_TYPES = {
    "photos":    ("imageMessage", "image", "stickerMessage", "sticker"),
    "videos":    ("videoMessage", "video"),
    # No "ptt" here: group_media_category() tests is_voice_message() before
    # this table, so a PTT never reaches it. Leaving it would only look like
    # the two categories disagree.
    "audios":    ("audioMessage", "audio"),
    "documents": ("documentMessage", "document"),
}

# The message types a voice note can possibly arrive under. is_voice_message()
# answers "is this audio a voice note" for records already known to be audio,
# so it honours a top-level ptt/isPtt flag BEFORE looking at the type at all —
# right at its own call sites, wrong as a category test: a photo record
# carrying a stray truthy "ptt" would be filed under voice notes and vanish
# from "Fotos" (and be judged against the wrong auto-download checkbox).
# group_media_category() gates on the type first.
_VOICE_CAPABLE_MESSAGE_TYPES = ("audioMessage", "audio", "ptt")


# Same shape conversations._URL_RE matches, kept here rather than imported so
# this module stays wx-free and importable by tests on its own.
_MEDIA_URL_RE = re.compile(r"https?://\S+|www\.\S+")


def message_has_link(msg) -> bool:
    """True when a message body carries a URL.

    Reads the WIRE text (conversation / extendedTextMessage.text), never a
    rendered line: a link preview's title or a resolved @mention would
    otherwise decide whether a message counts as a link.
    """
    if not isinstance(msg, dict):
        return False
    body = msg.get("message")
    if not isinstance(body, dict):
        return False
    text = body.get("conversation") or ""
    if not text:
        ext = body.get("extendedTextMessage")
        if isinstance(ext, dict):
            text = ext.get("text") or ""
    return bool(text) and bool(_MEDIA_URL_RE.search(text))


def group_media_category(msg) -> str:
    """Which Media-tab category *msg* belongs to, or "" when it belongs to none.

    A message's own media type decides first, so a photo whose caption happens
    to contain a URL is still a photo — a user looking for "the link someone
    sent" is looking for the text message, and counting the photo twice would
    make the checkboxes overlap.

    Pure and message-shaped rather than a method on the dialog, so the filter
    can be tested without wx - the tab itself is a wx.Panel.
    """
    if not isinstance(msg, dict):
        return ""
    msg_type = msg.get("messageType") or msg.get("type") or ""
    # Before the type table, not inside it: a voice note's messageType is
    # "audioMessage" too, so the table would claim it as "audios" first and
    # the new category would never match anything. Gated on the type as well —
    # see _VOICE_CAPABLE_MESSAGE_TYPES for why that gate has to be here even
    # though is_voice_message() has a type check of its own.
    if msg_type in _VOICE_CAPABLE_MESSAGE_TYPES and is_voice_message(msg):
        return "voice_messages"
    for category, types in _GROUP_MEDIA_MESSAGE_TYPES.items():
        if msg_type in types:
            return category
    if msg_type in ("conversation", "extendedTextMessage", "text") and \
            message_has_link(msg):
        return "links"
    return ""


# Categories the background media auto-download can be limited to
# (Configuracoes > Armazenamento). Links are deliberately absent: a link is a
# text message, there is no file behind it, and it never reaches the download
# path at all — offering it as something to "not download" would be a checkbox
# that does nothing.
#
# Derived from GROUP_MEDIA_TYPES rather than written out, so a category added
# there appears here too instead of silently becoming undownloadable.
AUTO_DOWNLOAD_MEDIA_TYPES = tuple(k for k in GROUP_MEDIA_TYPES if k != "links")


# Marks that the one-shot "audios" -> "audios" + "voice_messages" migration has
# already run for this install. A flag, not a shape check, because there is no
# shape to check: after the migration, a list holding "audios" without
# "voice_messages" is exactly what a user who unchecked "Mensagens de voz"
# leaves behind, and it is byte-for-byte identical to a pre-split list. Without
# something recording that the migration ran, every launch would re-tick the
# box the user just unticked. Lives in "general" because it describes the file,
# not a single section, and it is deliberately absent from DEFAULT_SETTINGS: a
# fresh install runs the migration once over lists that already contain the new
# key (a no-op) and writes the flag itself, which is one write and no special
# case.
VOICE_MEDIA_TYPE_MIGRATION_FLAG = "voice_messages_media_type_migrated"

# The persisted lists this migration has to fix up, section by section. Both
# hold GROUP_MEDIA_TYPES keys, both were written before "voice_messages"
# existed.
_MIGRATED_MEDIA_TYPE_SETTINGS = (
    ("user_interface", "group_media_default_types"),
    ("storage", "auto_download_media_types"),
)


def migrate_voice_messages_media_types(settings) -> bool:
    """Teach a pre-split settings.json about the "voice_messages" category.

    In a list written before the category existed, a checked "audios" meant
    "audio files AND voice notes" — that is what the single box did — so the
    new key inherits that state, and only that state. Nothing else is touched:

    * an explicitly empty list stays empty, because unchecking everything is a
      legitimate choice (see auto_download_allows()) and "empty" never meant
      "voice notes too";
    * a list without "audios" stays without "voice_messages", for the same
      reason — the user had audio off, and off is what the split should keep;
    * a missing or corrupt value is left alone, because every reader already
      treats that as "all categories" and inserting a list here would turn an
      un-chosen default into a saved choice.

    Returns True whenever *settings* changed, which includes merely writing the
    flag: the flag has to reach disk or the migration runs again next launch
    and re-checks a box the user has since unchecked.
    """
    if not isinstance(settings, dict):
        return False
    general = settings.get("general")
    if not isinstance(general, dict):
        general = {}
        settings["general"] = general
    if general.get(VOICE_MEDIA_TYPE_MIGRATION_FLAG):
        return False
    for section_name, key in _MIGRATED_MEDIA_TYPE_SETTINGS:
        section = settings.get(section_name)
        if not isinstance(section, dict):
            continue
        saved = section.get(key)
        if not isinstance(saved, (list, tuple)):
            continue
        if "audios" not in saved or "voice_messages" in saved:
            continue
        # Rebuilt in GROUP_MEDIA_TYPES order, the order both dialogs write
        # these lists in, so a migrated file looks like one the user saved.
        section[key] = [k for k in GROUP_MEDIA_TYPES
                        if k in saved or k == "voice_messages"]
    general[VOICE_MEDIA_TYPE_MIGRATION_FLAG] = True
    return True


# Marks that the one-shot voice_message_mode "audio" -> "voice_message" default
# change has already run for this install. Its own flag, deliberately not the
# media-types one: the two migrations are independent and an install that ran
# only the first one must still get this one.
VOICE_MESSAGE_MODE_MIGRATION_FLAG = "voice_message_mode_default_migrated"


def migrate_voice_message_mode_default(settings) -> bool:
    """Move an existing install onto the new voice_message_mode default.

    The default became "voice_message" (announce a voice note as "mensagem de
    voz", not as "audio") — but every settings.json in existence was seeded
    from settings_default.json and therefore already has the literal string
    "audio" saved, so changing the default alone would reach nobody. Hence the
    conversion, which is the deliberate cost of this change: a user who chose
    "Audio" on purpose cannot be told apart from one who never opened the
    setting, so both are converted and the first has to re-tick the radio in
    Configuracoes > Interface do usuario.

    Only the exact string "audio" is converted. A missing value is left absent
    (backfill_missing_defaults() puts the new default there straight after),
    and an unrecognized value is left untouched — rewriting a value we cannot
    interpret would be guessing at what the user meant.

    The flag is what makes this a one-shot change rather than a permanent
    override: without it, the user who does go back and re-tick "Audio" would
    find it silently reverted on the next launch, and that is exactly the user
    who cares. Returns True whenever *settings* changed, the flag included —
    an unwritten flag is the same as no flag.
    """
    if not isinstance(settings, dict):
        return False
    general = settings.get("general")
    if not isinstance(general, dict):
        general = {}
        settings["general"] = general
    if general.get(VOICE_MESSAGE_MODE_MIGRATION_FLAG):
        return False
    section = settings.get("user_interface")
    if isinstance(section, dict) and section.get("voice_message_mode") == "audio":
        section["voice_message_mode"] = "voice_message"
    general[VOICE_MESSAGE_MODE_MIGRATION_FLAG] = True
    return True


def auto_download_allows(settings, msg) -> bool:
    """Whether the background auto-download may fetch *msg*'s media.

    A missing or non-list setting allows everything: that is the default, and
    it is also what a settings.json written before this option looks like —
    reading it as "nothing selected" would silently stop all media downloads
    for every existing install. An explicitly empty list is honoured, because
    unchecking everything is a choice the user can legitimately make.

    Note that stickers count as photos, because group_media_category() puts
    them there — the same grouping the Media tab shows, so unchecking "Fotos"
    means the same thing in both places.
    """
    section = settings.get("storage") if isinstance(settings, dict) else None
    allowed = section.get("auto_download_media_types") if isinstance(section, dict) else None
    if not isinstance(allowed, (list, tuple)):
        return True
    category = group_media_category(msg)
    if not category or category == "links":
        # Not one of the categories this setting governs. Whatever else may
        # skip it, this check is not the one to do it.
        return True
    return category in allowed


# The Media tab's "Filtrar midias" radio, mirroring the conversation list's own
# filter. Order is the order the radio shows them in.
GROUP_MEDIA_FILTER_ALL = "all"
GROUP_MEDIA_FILTER_DOWNLOADED = "downloaded"
GROUP_MEDIA_FILTER_NOT_DOWNLOADED = "not_downloaded"
GROUP_MEDIA_FILTERS = (
    GROUP_MEDIA_FILTER_ALL,
    GROUP_MEDIA_FILTER_DOWNLOADED,
    GROUP_MEDIA_FILTER_NOT_DOWNLOADED,
)


def media_cache_id(msg) -> str:
    """The id a message's cached media file is stored under.

    WhatsApp ids for media sometimes arrive as "<a>_<b>_<real>" and the cache
    is keyed by the last (or third) part — the same unpacking the message list
    and the old media count both did inline. Here once, so the Media tab's
    "is it downloaded" check cannot disagree with the code that wrote the file.
    """
    if not isinstance(msg, dict):
        return ""
    mid = (msg.get("key") or {}).get("id", "") or ""
    if "_" in mid:
        parts = mid.split("_")
        return parts[2] if len(parts) > 2 else parts[-1]
    return mid


def filter_group_media_by_download(records, media_filter, downloaded_ids) -> list:
    """Apply the "Filtrar midias" radio.

    *downloaded_ids* is a precomputed set of media_cache_id()s known to be on
    disk. It is passed in rather than stat-ed here on purpose: this runs on the
    UI thread on every filter change, and one os.path.isfile() per message is
    exactly what froze this dialog before (issue #52).

    A link message has no file to download, so it belongs with the
    not-downloaded side — which is what the third option asks for by name
    ("nao baixadas / links").
    """
    if media_filter == GROUP_MEDIA_FILTER_ALL:
        return list(records or [])
    downloaded = set(downloaded_ids or ())
    out = []
    for m in (records or []):
        is_link = group_media_category(m) == "links"
        on_disk = (not is_link) and media_cache_id(m) in downloaded
        if media_filter == GROUP_MEDIA_FILTER_DOWNLOADED:
            if on_disk:
                out.append(m)
        elif not on_disk:
            out.append(m)
    return out


def filter_group_media(records, enabled_types) -> list:
    """The Media tab's list contents: every media record whose category is
    enabled, in the order the message list itself uses them, so a row reads the
    same in both places.

    Anything that is not media is excluded outright, whatever the checkboxes
    say: the tab is a media browser, not a filtered conversation.
    """
    enabled = set(enabled_types or ())
    return [m for m in (records or []) if group_media_category(m) in enabled]


def append_selected_marker(text: str, word: str, position: str, is_selected: bool) -> str:
    """Add the localized "selected" marker word to a list-row string when
    *is_selected*, at the configured *position* ("start" or anything else,
    treated as "end"). Used by both the messages list and the conversations
    list so a screen-reader user with sound events disabled still gets a
    persistent, textual cue for which rows are part of the bulk selection."""
    if not is_selected:
        return text
    return f"{word} {text}" if position == "start" else f"{text} {word}"


def link_preview_text(ext: dict, text: str, main_window=None) -> str:
    """Prepend a link's WhatsApp-generated preview to the message *text*.

    An ``extendedTextMessage`` that carries a link preview holds the title and
    description WhatsApp itself resolved for the URL (see
    websocket_client._has_link_preview). Rendered as
    ``"<title>. <description>. <text>"`` — preview first, then the link — which
    is the same order WhatsApp's own card conveys visually, expressed as plain
    text because neither surface here has room for a thumbnail.

    Both surfaces that read a link out loud go through this one function: the
    message list (ui/conversations._get_message_content) and the notification
    toast (core/notification_manager.format_notification_body). They used to
    each carry their own copy of this switch — and for a long time the toast's
    copy simply didn't exist, so a link with a preview read as its raw URL
    there while the list read the title. Same reasoning as status_panel's
    _status_content_label(): a near-duplicate of a small switch is exactly the
    shape that silently drifts.

    Gated on Settings > Interface do usuário > "Mostrar prévias de links"
    (``user_interface.show_link_previews``, on by default). *main_window* is
    optional so a caller with no window in reach still gets the default
    behaviour instead of an AttributeError.
    """
    if main_window is not None:
        show_previews = main_window.settings.get("user_interface", {}).get(
            "show_link_previews", True
        )
        if not show_previews:
            return text
    if not isinstance(ext, dict):
        return text
    bits = [
        part
        for part in (
            (ext.get("title") or "").strip(),
            (ext.get("description") or "").strip(),
        )
        if part
    ]
    if not bits:
        return text
    preview = ". ".join(bits)
    return f"{preview}. {text}" if text else preview


def get_downloads_folder() -> str:
    """Return the current user's Downloads folder.

    Resolves Windows' FOLDERID_Downloads shell API (SHGetKnownFolderPath)
    rather than assuming ``~/Downloads`` — that plain join is wrong for any
    user who has redirected their Downloads folder elsewhere (e.g. to a
    OneDrive-synced location, or a different drive), which the shell API
    correctly follows. Falls back to ``~/Downloads`` if the API call fails
    for any reason, or on a non-Windows platform.
    """
    fallback = os.path.join(os.path.expanduser("~"), "Downloads")
    if sys.platform != "win32":
        return fallback
    try:
        import ctypes
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_byte * 8),
            ]

        # FOLDERID_Downloads = {374DE290-123F-4565-9164-39C4925E467B}
        folder_id = _GUID(
            0x374DE290, 0x123F, 0x4565,
            (ctypes.c_byte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B),
        )
        path_ptr = ctypes.c_wchar_p()
        result = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(path_ptr)
        )
        if result == 0 and path_ptr.value:
            path = path_ptr.value
            ctypes.windll.ole32.CoTaskMemFree(path_ptr)
            if os.path.isdir(path):
                return path
    except Exception:
        pass
    return fallback

# Single source of truth for settings.json's shape, used both to bootstrap a
# missing/corrupt settings.json (MainWindow.load_settings()) and to backfill
# settings_default.json when the dialog needs it (settings_dialog.py). Keeping
# one copy avoids the two call sites drifting apart when a new settings key
# is added to only one of them.
DEFAULT_SETTINGS = {
    "connection": {
        "wpp_server": "http://127.0.0.1",
        "wpp_port": 6300,
        "wpp_ws_server": "ws://127.0.0.1",
        "wpp_api_key": "70733f08be1ed195bda1c31b6e135f5ebeb9fb8c6c28530a3a46e4093357b037",
        "wpp_custom_api": False
    },
    "general": {
        "language": "",
        "notifications_enabled": True,
        "keep_muted_chats_silent_when_open": True,
        "updates_enabled": True,
        # Alpha channel (one build per commit on main) — opt-in, see
        # client/updater.py's select_release().
        "alpha_updates_enabled": False,
        "noise_reduction_enabled": False,
        "first_run": True,
        "api_type_first_run_asked": False,
        "hotkey_first_run_asked": False,
        "autostart": False,
        "show_tray_icon": True,
        "terms_alert_displayed": False,
        "quick_tip_shown": False,
        "global_hotkey": None,
        "switch_behavior": "single",
        # Master mute (Settings > Geral) for the spoken+sound announcements of
        # sync progress/completion, media downloads and the automatic offline
        # transition. On by default; unchecked = those warnings stay silent.
        "announce_sync_events": True,
        "search_normalization": "off"
    },
    "status": {
        "messages_set_completed": False
    },
    "status_panel": {
        "liked_status_ids": [],
        "viewed_status_ids": []
    },
    "calls": {
        "alerts_enabled": True,
        "popup_enabled": True
    },
    "user_interface": {
        "messages_page_size": 200,
        "page_jump_size": 15,
        "focus_on_open": "message_field",
        "voice_record_focus": "send_button",
        "message_list_mode": "classic",
        "show_listbox_item_count": False,
        "page_up_down_step": 10,
        "self_reference_mode": "eu",
        "self_reference_custom_word": "",
        "show_delivery_status_in_chat_list": True,
        "preserve_typed_text_as_attachment_caption": True,
        "bulk_action_shortcuts": True,
        "auto_focus_next_audio": True,
        "selected_announcement_position": "end",
        "show_yesterday_label": True,
        "show_link_previews": True,
        "forwarded_prefix_enabled": False,
        "conversation_video_media_viewer_dialog": True,
        "status_media_viewer_dialog": True,
        "voice_message_mode": "voice_message",
        # Which media categories start checked in a group's Media tab.
        # A list, not four booleans, so GROUP_MEDIA_TYPES stays the single
        # place a category is declared.
        "group_media_default_types": list(GROUP_MEDIA_TYPES)
    },
    "audio_playback": {
        "audio_default_speed": 1.0,
        # On by default: this is the existing behaviour. Off means a received
        # voice note is never flagged "played" in the message list when its
        # playback ends — which also means no row rewrite, and so no screen
        # reader announcement about a message the user has already moved off.
        "mark_audio_played_in_list": True
    },
    "audio_devices": {
        "output_device_name": "",
        "effects_output_device_name": "",
        "input_device_name": ""
    },
    "accessibility": {
        "extended_sr_compat_enabled": True,
        "sapi_fallback_enabled": True
    },
    # See core/save_location.py — which folder a Save As dialog opens on.
    # "last" is the default and is a deliberate change from the old
    # unconditional Downloads: it degrades to Downloads on the first save of a
    # fresh install, so nobody has to open the settings to get sensible
    # behaviour.
    "files": {
        "save_dialog_folder_mode": "last",
        "save_dialog_custom_folder": "",
        "save_dialog_last_folder": "",
    },
    "speech_content": {
        "announce_typing": True,
        "announce_recording": True,
        "announce_conversations_update_start": True,
        "announce_conversations_update_complete": True,
        "speak_active_conv_messages": True,
        "speak_other_conv_messages": True,
        "silence_while_recording": False
    },
    "active_sound_pack": "default",
    "sound_events": {},
    "alert_tones": {
        "private": "default",
        "private_custom_path": "",
        "group": "default",
        "group_custom_path": ""
    },
    "conversation_sounds": {},
    "cleared_chats": {},
    "storage": {
        "auto_download_media": True,
        # Which categories the auto-download covers. All of them by default —
        # see auto_download_allows(). Links are not a category here.
        "auto_download_media_types": list(AUTO_DOWNLOAD_MEDIA_TYPES),
        "media_max_days": 30,
        "media_max_mb": 100,
        "probe_video_duration_on_download": False
    }
}

def generate_and_save_key(filepath):
    key = Fernet.generate_key()
    with open(filepath, 'wb') as key_file:
        key_file.write(key)
    return key

def retrieve_key(filepath):
    with open(filepath, 'rb') as key_file:
        key = key_file.read()
    return key

def encrypt(data, key):
    fernet = Fernet(key)
    #Encode only if data is string
    if isinstance(data, str):
        data = data.encode()
    encrypted_data = fernet.encrypt(data)
    return encrypted_data

def decrypt(encrypted_data, key):
    fernet = Fernet(key)
    decrypted_data = fernet.decrypt(encrypted_data)
    return decrypted_data.decode()

def decrypt_bytes(encrypted_data, key):
    fernet = Fernet(key)
    return fernet.decrypt(encrypted_data)

def _sanitize_for_json(obj):
    """Recursively convert bytes values to base64 strings so json.dumps never raises."""
    if isinstance(obj, bytes):
        return base64.b64encode(obj).decode("ascii")
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in list(obj.items())}
    if isinstance(obj, list):
        return [_sanitize_for_json(item) for item in list(obj)]
    return obj

def encrypt_json(data, key):
    fernet = Fernet(key)
    json_data = json.dumps(_sanitize_for_json(data)).encode()
    encrypted_data = fernet.encrypt(json_data)
    return encrypted_data

def decrypt_json(encrypted_data, key):
    fernet = Fernet(key)
    decrypted_data = fernet.decrypt(encrypted_data)
    data = json.loads(decrypted_data.decode())
    return data

def is_phone_like(name: str) -> bool:
    """Return True if name looks like a phone number rather than a display name.

    Also rejects purely-numeric strings of any length (e.g. "0") — those are
    WPPConnect API fallbacks from contact.id.split('@')[0] when no real name is
    available, not actual display names.
    """
    if not name:
        return False
    stripped = name.strip()
    if stripped.isdigit():
        return True  # "0", "123", "5511999999999" — never a real name
    digit_count = sum(1 for c in stripped if c.isdigit())
    return digit_count >= 7 and digit_count >= len(stripped) * 0.7

def looks_like_binary_blob(value) -> bool:
    """Return True if value looks like base64 image/thumbnail data, not a name.

    Business accounts and some vCards leak a ``jpegThumbnail`` (or other binary
    blob) into name fields, which then surfaced in the chat list as garbage like
    ``+0 /9j/4AAQSkZJRg...``. Such values must never be treated as display names.
    """
    if not value or not isinstance(value, str):
        return False
    s = value.strip()
    if not s:
        return False
    # Common base64 image/data signatures (JPEG, PNG, GIF, SVG, data URIs).
    if s.startswith(("/9j/", "iVBORw0", "R0lGOD", "data:image", "PHN2Zy", "JVBER")):
        return True
    # A long, spaceless string drawn entirely from the base64 alphabet is
    # overwhelmingly binary data rather than a human name.
    if len(s) > 64 and " " not in s and re.fullmatch(r"[A-Za-z0-9+/=_-]+", s):
        return True
    return False

_JID_RE = re.compile(
    r"^\d+(?::\d+)?@(s\.whatsapp\.net|c\.us|g\.us|lid|broadcast|newsletter)$"
)

def looks_like_jid(value) -> bool:
    """Return True if value is nothing but a bare WhatsApp JID.

    Real chat text is never just a phone-number/lid digit string plus a
    '@...' suffix — this is only ever seen when WPPConnect's raw
    notification-type payload (internal WhatsApp events with no real text
    content, e.g. a security-code/E2E-identity-change notice) stuffs the
    JID of whoever triggered it into the same "body"/"text" field normal
    messages use, and generic text-fallback code mistakes it for one.
    """
    if not value or not isinstance(value, str):
        return False
    return bool(_JID_RE.match(value.strip()))

def _clean_mentioned_jid(jid_val):
    """Normalize one mentionedJid entry to a plain '...@s.whatsapp.net'/'@lid'
    string. Mirrors WebSocketClient._clean_jid() without needing an instance —
    entries can arrive as a raw WPPConnect Wid dict instead of a string."""
    if not jid_val:
        return ""
    if isinstance(jid_val, dict):
        jid_val = jid_val.get("_serialized") or jid_val.get("id") or ""
    if not isinstance(jid_val, str):
        jid_val = str(jid_val)
    return jid_val.replace("@c.us", "@s.whatsapp.net")


def _extract_mentioned_jids(quoted):
    """Pull the quoted message's OWN mentionedJid list out of *quoted*,
    wherever WPPConnect/Baileys put it — a WPPConnect-style raw quote carries
    it flat as ``mentionedJidList``; a Baileys-style quotedMessage proto
    carries it nested under (extendedTextMessage.)contextInfo.mentionedJid."""
    for src in (
        quoted,
        quoted.get("contextInfo") or {},
        (quoted.get("extendedTextMessage") or {}).get("contextInfo") or {},
    ):
        raw = src.get("mentionedJidList") or src.get("mentionedJid")
        if raw:
            return [_clean_mentioned_jid(m) for m in raw if m]
    return []


def _slim_quoted_message(quoted):
    """Reduce a quoted-message dict to only what the reply preview needs.

    WPPConnect embeds the *entire* quoted message under
    ``contextInfo.quotedMessage`` — including the base64 thumbnail, mediaKey,
    directPath, deprecatedMms3Url, file hashes, etc. None of that is read by the
    UI (the preview only shows a short text or a type label), yet it dominated
    messages.dat and slowed every conversation that had replies. This keeps just
    a capped text preview plus a type marker.

    mentionedJid is the one field kept beyond that: without it, a quoted
    message's own @mentions render as raw @<phone-or-lid-digits> forever,
    because the placeholder text survives slimming but the JID list needed to
    resolve it into a contact name does not — see
    ConversationsPanel._resolve_mentions_in_text(), the only consumer.
    """
    if not isinstance(quoted, dict):
        return quoted
    text = (
        quoted.get("conversation")
        or quoted.get("caption")
        or quoted.get("body")
        or (quoted.get("extendedTextMessage") or {}).get("text")
        or ""
    )
    if not isinstance(text, str) or looks_like_binary_blob(text):
        text = ""
    text = text[:300]  # a long pasted message must not be duplicated into replies

    qtype = quoted.get("type")
    slim: dict = {}
    if qtype and qtype not in ("chat", "text"):
        slim["type"] = qtype
        if text:
            slim["caption"] = text
    elif text:
        slim["conversation"] = text
    else:
        # No usable text — preserve a media type marker so the preview can still
        # render a localized label ("Photo", "Audio", …).
        for k in ("imageMessage", "videoMessage", "audioMessage",
                  "documentMessage", "stickerMessage", "contactMessage"):
            if k in quoted:
                slim[k] = {}
                break
    mentioned = _extract_mentioned_jids(quoted)
    if mentioned:
        slim["mentionedJid"] = mentioned
    return slim


# Heavy media fields that arrive from the API but are never read by the client:
# media is (re)downloaded by message id via /get-media-by-message, and voice
# notes live in the voice_messages folder — so urls, encryption keys, hashes and
# waveforms are pure dead weight inside messages.dat. jpegThumbnail is kept
# because the inline-thumbnail view uses it.
_HEAVY_MEDIA_FIELDS = frozenset({
    "url", "directPath", "mediaKey", "mediaKeyTimestamp", "deprecatedMms3Url",
    "fileSha256", "fileEncSha256", "filehash", "encFilehash",
    "thumbnailDirectPath", "thumbnailSha256", "thumbnailEncSha256",
    "streamingSidecar", "waveform", "midQualityFileSha256",
    "midQualityFileEncSha256", "scansSidecar", "scanLengths",
    "firstScanSidecar", "firstScanLength", "rawMediaData", "body",
})


def prune_message_record(msg):
    """Strip stored bloat from a single message record (mutates and returns it).

    - Slims ``contextInfo.quotedMessage`` wherever it appears.
    - Removes heavy, never-read media fields (urls, mediaKey, hashes, waveform…)
      from each message sub-type so audio/image/video records stay tiny.

    Returns True if anything was changed.
    """
    if not isinstance(msg, dict):
        return False
    changed = False

    def _prune_ctx(ctx):
        nonlocal changed
        if isinstance(ctx, dict):
            q = ctx.get("quotedMessage")
            if isinstance(q, dict):
                slim = _slim_quoted_message(q)
                if slim != q:
                    ctx["quotedMessage"] = slim
                    changed = True

    _prune_ctx(msg.get("contextInfo"))
    m = msg.get("message")
    if isinstance(m, dict):
        for sub in m.values():
            if isinstance(sub, dict):
                _prune_ctx(sub.get("contextInfo"))
                for k in _HEAVY_MEDIA_FIELDS & sub.keys():
                    del sub[k]
                    changed = True
    return changed


def prune_chats_messages(chats) -> bool:
    """Prune every stored message in a chats dict in place. Returns True if any
    record changed (so the caller can persist the slimmed data once)."""
    changed = False
    if not isinstance(chats, dict):
        return False
    for chat in chats.values():
        try:
            records = (
                chat.get("messages", {}).get("messages", {}).get("records", [])
            )
        except AttributeError:
            continue
        for rec in records:
            if prune_message_record(rec):
                changed = True
    return changed


def effective_unread_count(chat) -> int:
    """A chat's unread count, suppressed only when it would open empty.

    The problem this guards against: a chat-list sync can report e.g.
    unreadCount=2 for a chat whose per-chat message fetch previously failed (or
    hasn't run yet), leaving 0 messages in the local store — showing "2 unread"
    on a conversation that opens completely empty.

    This used to return ``min(reported, local_count)``, which fixed that case
    but silently capped every count at however many messages the last fetch
    happened to bring back. WhatsApp Web's message store only holds a handful
    of messages per chat right after a pairing (measured: ~15), so a group with
    5747 unread announced "15" — badly wrong for a screen-reader user deciding
    what to open, and indistinguishable from a group with exactly 15.

    Having *some* history is enough to know the conversation is real and opens
    with content; beyond that, the server's own count is the honest number, and
    the one the app should report. Callers that need a count bounded by what is
    actually on screen (the unread separator in conversations.py) already check
    that themselves.
    """
    if not isinstance(chat, dict):
        return 0
    reported = int(chat.get("unreadCount") or 0)
    if reported <= 0:
        return 0
    try:
        local_count = len(
            chat.get("messages", {}).get("messages", {}).get("records", []) or []
        )
    except AttributeError:
        local_count = 0
    if local_count == 0:
        return 0
    return reported


_CC_SORTED: list[str] | None = None

def _known_country_codes() -> list[str]:
    """Return country codes sorted longest-first (cached). Thread-safe for reads."""
    global _CC_SORTED
    if _CC_SORTED is None:
        try:
            try:
                from client.countries import COUNTRIES
            except ImportError:
                from countries import COUNTRIES
            _CC_SORTED = sorted({code for _, code in COUNTRIES}, key=len, reverse=True)
        except Exception:
            _CC_SORTED = []
    return _CC_SORTED

def format_number(string_number):
    """Format a raw digit string (or JID) as a human-readable phone number.

    Brazil (+55): +55 DD XXXXX-XXXX or +55 DD XXXX-XXXX
    All other countries: +CC local  (no area-code assumptions)
    Falls back to '+<digits>' if no known country code matches.
    """
    digits = "".join(c for c in string_number.split('@')[0] if c.isdigit())
    if not digits:
        return string_number

    # Do not format LID JIDs (which are 15-digit internal identifiers) as phone numbers
    if "@lid" in string_number or len(digits) >= 14:
        return digits

    cc = None
    for candidate in _known_country_codes():
        if digits.startswith(candidate):
            cc = candidate
            break

    if cc is None:
        return f"+{digits}"

    local = digits[len(cc):]

    # A Brazilian mobile/landline number is always DDD(2) + 8 or 9 digits —
    # 10 or 11 digits total after the country code. E.164 codes are
    # prefix-free (no other country's code starts with "55"), so a genuine
    # phone number matching "55" here really is Brazilian — but a
    # non-standard-length id (e.g. a WhatsApp internal identifier that
    # slipped past the @lid/length guard above, or a malformed contact
    # entry) can still coincidentally start with "55" digits without being
    # a real Brazilian number. Forcing the DDD/dash split on it produced a
    # string that *looked* like a valid Brazilian number even though it
    # wasn't one — reported live for shared contacts whose actual country
    # was something else entirely (issue #35). Only apply the Brazil-
    # specific shape when the length actually fits; anything else falls
    # through to the plain "+55 <digits>" international format below,
    # which at least never fabricates a fake area code/dash grouping.
    if cc == "55" and len(local) in (10, 11):
        ddd = local[:2]
        rest = local[2:]
        if len(rest) == 9:
            return f"+{cc} {ddd} {rest[:5]}-{rest[5:]}"
        return f"+{cc} {ddd} {rest[:4]}-{rest[4:]}"

    # Generic international (also covers "55" matches of the wrong length)
    return f"+{cc} {local}" if local else f"+{cc}"


def contact_dedup_key(main_window, jid: str) -> str:
    """Canonical key identifying *the same person* across JID formats.

    The same contact can be stored in main_window.contacts under @lid, @c.us
    or @s.whatsapp.net (and with the Brazilian 8- vs 9-digit mobile variant),
    so keying a dedup on the raw JID string lets the same person appear more
    than once — reported live in the "attach a contact" picker as every
    contact showing up twice, once in international format and once in
    Brazilian formatting (issue #70). Collapse all the formats the app
    already knows how to unify: device suffix stripped, @c.us →
    @s.whatsapp.net, @lid bridged to its phone number, and the 55-prefixed
    9th digit dropped. Groups keep their full unique JID.
    """
    normalize_jid = getattr(main_window, "_normalize_jid", None)
    norm = normalize_jid(jid) if normalize_jid else jid
    if norm.endswith("@g.us"):
        return norm
    if norm.endswith("@lid"):
        lid_map = getattr(main_window, "_lid_to_phone", {}) or {}
        phone = lid_map.get(norm, "")
        if phone:
            norm = phone
    local = norm.split("@", 1)[0]
    # Brazilian mobile 8/9-digit interchangeability: 5511999999999 ↔ 551199999999
    if local.startswith("55") and len(local) == 13 and local[4] == "9":
        local = local[:4] + local[5:]
    return local


def parse_bool_flag(value):
    """Interpret a WPPConnect boolean-ish field, or None when it says nothing.

    Chat flags (``archive``, ``pin``) come back as real booleans, as the
    strings "true"/"false", as 0/1, or missing entirely depending on the
    endpoint and API version.  A plain ``bool(value)`` is wrong for the string
    form — ``bool("false")`` is True — and that is exactly how conversations
    the user never archived ended up in the Archived tab.  Returning None for
    "not stated" lets callers leave the local state untouched.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes"):
            return True
        if v in ("false", "0", "no", "", "none", "null"):
            return False
    return None


def group_setting_notif_value(notif):
    if not isinstance(notif, dict):
        return None
    raw = notif.get("value")
    if raw is None:
        raw = (notif.get("body") or "").strip()
    if isinstance(raw, str):
        if not raw.strip():
            return None
        low = raw.strip().lower()
        if low in ("on", "announcement", "locked"):
            return True
        if low in ("off", "unlocked"):
            return False
    return parse_bool_flag(raw)


def check_internet_connection(test_url="https://www.google.com", timeout=10):
    try:
        response = requests.get(test_url, timeout=timeout)
        return True
    except (requests.ConnectionError, requests.Timeout):
        return False


def mute_response_accepted(http_ok: bool, body: str, is_unmute: bool) -> bool:
    """Decide whether a WPPConnect /send-mute reply means "state applied".

    Any 2xx is a success. Beyond that, an older/unpatched WPPConnect routes
    /send-mute through the legacy ``WAPI.sendMute`` shim, which answers HTTP
    500 with ``{"erro": true, "text": "This chat is already mute"}`` (or "is
    not mute to remove" when unmuting) for *any* internal non-200 — including
    the perfectly ordinary case of the chat already being in the state we
    asked for. Treating those as failures rolled the optimistic local change
    back and showed the user an error for a no-op, which is why muting looked
    completely broken. The real fix is the patched sendMute controller (which
    drives ``WPP.chat.mute``); this keeps the client correct against an API
    build that predates it.
    """
    if http_ok:
        return True
    text = (body or "").lower()
    if is_unmute:
        return "is not mute to remove" in text
    return "already mute" in text


def first_unread_index(displayable, unread_count: int) -> int:
    """Index of the first unread message in *displayable*, or -1.

    WhatsApp's ``unreadCount`` only ever counts messages you *received*. The
    naive ``len(displayable) - unread_count`` therefore lands in the wrong place
    whenever any of your own messages sit at the tail of the conversation —
    typically because you replied from your phone or another linked device. The
    unread separator was then drawn above your own messages, announcing them as
    unread, which is what "minhas próprias mensagens contam como não lidas"
    describes.

    Walk backwards instead, counting only incoming (``key.fromMe`` falsy)
    messages, and stop on the *unread_count*-th one. Returns -1 when the loaded
    history doesn't hold that many incoming messages (nothing sensible to
    anchor the separator to).
    """
    if unread_count <= 0:
        return -1
    seen = 0
    for idx in range(len(displayable) - 1, -1, -1):
        msg = displayable[idx]
        if not isinstance(msg, dict):
            continue
        if (msg.get("key") or {}).get("fromMe"):
            continue
        seen += 1
        if seen == unread_count:
            return idx
    return -1


def display_page_fetch_limit(configured_limit: int, cap: int = 2000, buffer: int = 50) -> int:
    """Raw rows needed to reliably fill a page of visible messages."""
    return min(configured_limit + buffer, cap)


def db_fetch_limit(configured_limit: int, unread_count: int, cap: int = 2000, buffer: int = 50) -> int:
    """How many messages navigate_to_conversation() should pull from the DB.

    The configured "messages per conversation" page size is meant to bound a
    *display* window, not to cap how much unread history gets loaded. When a
    conversation has more unread messages than that page size (e.g. 350
    unread against the default 200), fetching only ``configured_limit``
    messages leaves the unread separator (and every message after it)
    entirely outside what's loaded — paginated_window()'s widening logic
    never gets a chance to run because first_unread_index() can't find enough
    incoming messages in the truncated list to begin with. Alt+3 then reports
    "no unread" and initial focus falls back to the last message instead of
    the separator.

    Widen the fetch to cover the unread backlog (plus a small buffer of
    already-read context above it), capped so a corrupt/absurd unread count
    can't pull an unbounded amount of history into memory at once.
    """
    visible_page = display_page_fetch_limit(configured_limit, cap, buffer)
    if unread_count > configured_limit:
        return min(max(visible_page, unread_count + buffer), cap)
    return visible_page


def paginated_window(total_len: int, limit: int, unread_sep_idx: int,
                     min_visible: int = 0) -> tuple:
    """Where populate_messages()'s pagination window should start.

    Returns ``(offset, adjusted_sep_idx)``: ``offset`` is how many leading
    messages to skip (0 when everything fits), and ``adjusted_sep_idx`` is
    ``unread_sep_idx`` re-based onto the paginated slice (-1 once the
    separator falls before ``offset``).

    Plain ``max(0, total_len - limit)`` cuts strictly at the configured page
    size regardless of what it cuts through. When a conversation has more
    unread messages than the page size allows (e.g. 230 unread against the
    default 200-message limit), that cut lands ABOVE the unread separator —
    the separator (and everything after it, i.e. every genuinely unread
    message) falls entirely outside the window and is silently dropped: no
    separator to land on, no Alt+3 target, no way to reach messages that are
    demonstrably still unread. So the window widens — but only while there is
    something unread to protect (``unread_sep_idx >= 0``); a fully-read
    conversation still respects the configured limit exactly as before.

    ``min_visible`` widens the window for the same kind of reason, but for
    history the user asked for by hand: Home/scroll-up loads older messages
    and grows the rendered list past the page size, and a background rebuild
    (the 60s resync, or the one every new message triggers) recomputing the
    window from the end alone would throw all of it away and snap back to the
    configured limit. It is a count of rows already materialized, not an
    offset, so it stays anchored to the same old point as new messages arrive,
    and it only grows when the user actually pulls more history in.
    """
    effective_limit = max(limit, min_visible)
    if unread_sep_idx >= 0:
        effective_limit = max(effective_limit, total_len - unread_sep_idx)
    if total_len <= effective_limit:
        return 0, unread_sep_idx
    offset = total_len - effective_limit
    adjusted = unread_sep_idx - offset if unread_sep_idx >= 0 else -1
    if adjusted < 0:
        adjusted = -1
    return offset, adjusted


def expanded_min_visible(displayable: list, anchor_id: str, fallback_count: int,
                         cap: int = 0) -> int:
    """How wide populate_messages() must keep the window it rebuilds.

    ``fallback_count`` is how many rows the list had after the user last pulled
    older history in. On its own it is not enough: every message that arrives
    afterwards grows ``displayable``, so a window sized purely by that count
    slides one row forward per arrival and eats back exactly the history that
    was loaded. ``anchor_id`` — the oldest message displayed at that moment —
    pins it instead, and the count stays as the floor for when that message is
    no longer there (deleted remotely), so a missing anchor widens the window
    less rather than collapsing it back to the page size.

    ``cap`` is off by default (0, or any non-positive value) and exists only
    for a caller that has a reason to bound the window. A standing cap must
    NOT be reintroduced here: it reproduces the original bug with a higher
    floor. Expanded to 4200 rows, the next arriving message recomputes the
    window at the cap and 2200 rows vanish under the reader mid-read;
    ``_expanded_visible_count`` is not rewritten, so every later rebuild
    performs the same cut, and the Home that follows only reaches
    ``_load_more_messages()`` and is undone again — above the cap the user can
    never reach older history at all. Rendering cost was the argument for one,
    but the multi-second stalls that argument cited were measured to be caused
    by *frequency*, not window size (see main.py's _schedule_refresh_active_
    messages(), where an oversized pagination window is recorded as a tested
    and discarded theory) and are fixed by its 1s debounce. Whether a very
    large window costs anything on its own has not been measured.
    """
    try:
        floor = max(0, int(fallback_count))
    except (TypeError, ValueError):
        floor = 0
    widened = floor
    if anchor_id:
        for idx, msg in enumerate(displayable):
            if isinstance(msg, dict) and msg.get("key", {}).get("id") == anchor_id:
                widened = max(floor, len(displayable) - idx)
                break
    try:
        ceiling = int(cap)
    except (TypeError, ValueError):
        ceiling = 0
    return min(widened, ceiling) if ceiling > 0 else widened


def history_window(displayable: list, anchor_id: str, expanded_count: int,
                   limit: int, unread_sep_idx: int, cap: int = 0) -> tuple:
    """The pagination window populate_messages() should rebuild with.

    Pulled out of populate_messages() whole because it is the entire fix for
    "the conversation drops the history I loaded": the panel state, the
    anchoring and the widening only matter together, and testing the two
    halves separately left the wiring between them — the one line that carries
    the expanded window into paginated_window() — covered by nothing.

    Returns ``paginated_window()``'s own ``(offset, adjusted_sep_idx)``.
    """
    min_visible = expanded_min_visible(displayable, anchor_id, expanded_count, cap)
    return paginated_window(len(displayable), limit, unread_sep_idx,
                            min_visible=min_visible)


def reaction_targets_status(msg: dict) -> bool:
    """True when this reaction is aimed at a status (story), not a chat message.

    A reaction carries the key of the message it reacts to, and for a status
    that key's remoteJid is status@broadcast. The distinction matters because a
    status has no counterpart in any conversation: it is kept in
    _status_updates for the Status tab, so a reaction to one has nothing to
    attach itself to and disappears the moment the conversation is rebuilt.
    """
    if msg.get("messageType") != "reactionMessage":
        return False
    target = ((msg.get("message") or {}).get("reactionMessage") or {}).get("key") or {}
    return str(target.get("remoteJid") or "").endswith("@broadcast")


def plan_row_updates(old_rows: list, new_rows: list, max_ops: "int | None" = None):
    """Plan how to turn the list-control rows *old_rows* into *new_rows* using
    only per-row deletes and inserts, instead of clearing the whole control.

    Both arguments are lists of row identities (WinZapp passes chat JIDs, which
    are unique within a list). Returns a list of ``("delete", index)`` /
    ``("insert", index)`` operations to apply **in order**, each index being
    valid against the control as it is being mutated. Returns None when the
    change isn't worth doing incrementally — the caller then rebuilds.

    Why this exists: a new message (or a reaction, or a read receipt) reorders
    the chat list, usually moving exactly one chat up. Rebuilding the whole
    wx.ListCtrl for that is O(rows) native calls plus a DeleteAllItems, which
    on an account with hundreds of chats is slow enough to see, and it hands
    screen readers a completely new list every time. One chat moving becomes
    two operations here regardless of how long the list is.

    The plan is greedy rather than provably minimal: rows that vanished are
    deleted first, then the remainder is walked against *new_rows* and any row
    that isn't already in place is moved (delete + insert) or inserted. For the
    shapes that actually occur — one chat moving, one chat appearing, one chat
    disappearing — that is already the minimum.

    *max_ops* caps how much churn is accepted before None is returned; it
    defaults to a third of the new length (minimum 4), past which a rebuild is
    both simpler and no slower.
    """
    if old_rows == new_rows:
        return []
    if max_ops is None:
        max_ops = max(4, len(new_rows) // 3)

    new_set = set(new_rows)
    # Duplicate identities would make index() below ambiguous and the plan
    # wrong; the caller's rows are unique, so bail rather than corrupt the list.
    if len(new_set) != len(new_rows) or len(set(old_rows)) != len(old_rows):
        return None

    work = list(old_rows)
    ops: list = []

    # 1. Drop rows that are gone. Walking forward and popping in place keeps
    #    every recorded index valid at the moment it is applied.
    i = 0
    while i < len(work):
        if work[i] not in new_set:
            ops.append(("delete", i))
            work.pop(i)
        else:
            i += 1

    # 2. Align what's left against the target order. Everything before
    #    target_idx already matches, and identities are unique, so a row still
    #    present in `work` can only be at or after target_idx.
    for target_idx, row in enumerate(new_rows):
        if target_idx < len(work) and work[target_idx] == row:
            continue
        try:
            src = work.index(row, target_idx)
        except ValueError:
            src = None
        if src is not None:
            ops.append(("delete", src))
            work.pop(src)
        ops.append(("insert", target_idx))
        work.insert(target_idx, row)
        if len(ops) > max_ops:
            return None

    if len(ops) > max_ops:
        return None
    return ops


def backfill_missing_defaults(settings: dict, defaults: dict) -> bool:
    """Insert every key/section of *defaults* that *settings* does not have.

    Returns True when anything was inserted, so the caller can decide whether
    a save is warranted. Recurses into nested dicts, and never overwrites a
    value the user already has — only genuinely absent keys are filled.

    Module-level and pure so it can be tested directly: it used to be a
    closure inside MainWindow.load_settings(), reachable only by constructing
    a wx.Frame, and the ordering bug it shipped with (running BEFORE
    _migrate_settings(), so the "ui" -> "user_interface" rename never fired
    because the new section already existed) is exactly the kind of thing one
    plain assertion catches.
    """
    modified = False
    for key, value in defaults.items():
        if key not in settings:
            settings[key] = copy.deepcopy(value)
            modified = True
        elif isinstance(value, dict) and isinstance(settings[key], dict):
            if backfill_missing_defaults(settings[key], value):
                modified = True
    return modified
