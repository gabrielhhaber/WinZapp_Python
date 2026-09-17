"""Carrying a user's settings to another install, without carrying the session.

settings.json cannot simply be copied: alongside the preferences it holds the
state of *this* install and *this* account — the WhatsApp login itself
(`privateinfo`: WA_token and paired), what this account has already synced,
which statuses it liked, per-chat clear cutoffs and per-chat sound overrides
keyed by JID, the Node port this account was given, and the one-shot flags that
say which first-run questions were already answered. Dropped into another
install, those either do nothing or actively break it: a stale token against a
different session, cutoffs for chats that are not there, a port another account
already owns.

So the export is a filtered copy, and the import applies only what it
recognises:

- EXCLUDED_SECTIONS and EXCLUDED_KEYS never leave and are never read back;
- both halves work from the same whitelist: a section this build does not
  declare in DEFAULT_SETTINGS is neither exported nor imported, so the next
  piece of state written straight into self.settings — which is how
  privateinfo, cleared_chats and status_panel all arrived — cannot leak into a
  file or arrive from one unnoticed;
- a value whose type does not match the shipped default is ignored too, so a
  string where a number belongs cannot reach the code that reads it;
- and a value can be refused for what it NAMES rather than its type: a locale
  this build does not ship would leave every label reading its own key name.

The whole module is pure: MainWindow owns the file dialogs, the writing and
making the imported settings take effect.
"""

import copy
from urllib.parse import urlsplit

from core.utils import DEFAULT_SETTINGS

#: What the first line of an exported file says it is. Checked on import so a
#: JSON file that is not a WinZapp export is refused with a clear message
#: rather than half-applied.
EXPORT_FORMAT = "winzapp-settings"
EXPORT_VERSION = 1

#: Sections that belong to this install or this account, never to the person.
EXCLUDED_SECTIONS = frozenset({
    # The WhatsApp login of this install (WA_token, paired). Never written to
    # a file the user may mail to themselves — see core/token_vault.py for why
    # it is not even kept in plain text inside settings.json.
    "privateinfo",
    "status",                 # messages_set_completed: this account's sync state
    "status_panel",           # which statuses this account liked or viewed
    "cleared_chats",          # per-JID clear cutoffs
    "cleared_starred_chats",
    "conversation_sounds",    # per-JID sound overrides
})

#: Keys inside a section that is otherwise worth carrying.
EXCLUDED_KEYS = frozenset({
    ("general", "autostart"),          # a Windows registry entry of that machine
    ("general", "first_run"),          # the one-shot questions this install
    ("general", "api_type_first_run_asked"),   # has already answered
    ("general", "hotkey_first_run_asked"),
    ("general", "terms_alert_displayed"),
    ("general", "quick_tip_shown"),
    ("files", "save_dialog_last_folder"),      # the last folder used HERE
    # Paths on THIS machine. Useless anywhere else, and worse than useless from
    # a file someone sent: a sound or folder path is opened as it stands, so a
    # network path (\\host\share\x.ogg) would make Windows sign in to that
    # host on every launch. The per-event sound paths in `sound_events` are
    # stripped for the same reason (_sound_events_without_paths()).
    ("files", "save_dialog_custom_folder"),
    ("alert_tones", "private_custom_path"),
    ("alert_tones", "group_custom_path"),
})

#: RegisterHotKey modifier bits, as settings_dialog._HotkeyCapture records them.
_MOD_ALT, _MOD_CONTROL, _MOD_SHIFT = 0x0001, 0x0002, 0x0004

#: The API this account talks to, which travels only for a custom one.
#:
#: With WinZapp’s own bundled API these are this install’s alone: the port
#: is allocated per account and re-resolved at every launch
#: (MainWindow._resolve_wpp_port), so another machine’s port is at best
#: overwritten and at worst claimed by a second account here; and the key is the
#: local REST credential. With a custom API they are the opposite — the address,
#: port and key of a server the person runs, which is exactly what they want on
#: the other computer too, so the whole block travels together.
#:
#: The addresses are in this list too, not only port and key. Every request
#: WinZapp makes carries the session token to `wpp_server`, so a file that
#: could set it while claiming the bundled API would point this install at any
#: host with nothing on screen to say so. Changing the API at all is also
#: confirmed separately, naming the server (api_change(), and
#: MainWindow._on_import_settings).
CUSTOM_API_KEYS = ("wpp_server", "wpp_ws_server", "wpp_port", "wpp_api_key")

_CONNECTION_KEYS = ("wpp_custom_api",) + CUSTOM_API_KEYS


#: One-shot migration flags (core/utils.py). Carrying one would stop the
#: migration running on the other install, leaving it on the old value.
_MIGRATION_FLAG_SUFFIX = "_migrated"


#: Values that are only usable if this build has the thing they name. A type
#: check cannot catch these: every locale code is a str.
def known_languages():
    """The locale codes this build ships, or None when they cannot be read.

    None means "do not judge": refusing every language because the map could
    not be read would quietly drop the one setting people most want to carry.
    """
    try:
        from core.i18n import LANGUAGE_NAMES
        codes = list(LANGUAGE_NAMES)
    except Exception:
        return None
    return set(codes) or None


def _language_is_usable(code, languages) -> bool:
    return languages is None or code in languages


def connection_runtime(settings, fallback=None):
    """The five connection values MainWindow keeps as attributes.

    Pulled out of MainWindow because this is where an import has to land: the
    app builds every WPPConnect URL from `wpp_server` and `wpp_port` together
    and authenticates with `wpp_api_key`, so applying some of them and not the
    rest points the app at a host with the wrong port and the wrong key.
    """
    fallback = fallback if isinstance(fallback, dict) else {}
    section = (settings or {}).get("connection") if isinstance(settings, dict) else None
    section = section if isinstance(section, dict) else {}
    return {key: section.get(key, fallback.get(key))
            for key in ("wpp_custom_api", "wpp_server", "wpp_ws_server",
                        "wpp_port", "wpp_api_key")}


def uses_custom_api(settings) -> bool:
    """Whether these settings describe a WPPConnect the user runs themselves."""
    section = (settings or {}).get("connection") if isinstance(settings, dict) else None
    # `is True`, not truthiness: merge_settings() only ever saves a real bool
    # here, so a file saying "yes" must not be asked about (api_change) as if
    # it moved the API and then be saved with the flag still False.
    return section.get("wpp_custom_api") is True if isinstance(section, dict) else False


def is_excluded(section, key, settings=None) -> bool:
    """Whether settings[section][key] stays behind. `key` None asks about the
    whole section. `settings` is the dict being exported or imported: the
    custom-API keys are judged against what it says about the API (see
    CUSTOM_API_KEYS)."""
    if section in EXCLUDED_SECTIONS:
        return True
    if key is None:
        return False
    if (section, key) in EXCLUDED_KEYS:
        return True
    if section == "connection" and key in CUSTOM_API_KEYS:
        return not uses_custom_api(settings)
    return str(key).endswith(_MIGRATION_FLAG_SUFFIX)


def _sound_events_without_paths(value):
    """(clean sound_events, ignored names) — {pack: {event: {...}}} with every
    per-event `path` removed, and anything not of that shape dropped.

    load_sounds() reads it as exactly that nesting and calls .get() at every
    level, so a string one level too deep saved here crashes every launch before
    the window exists. The path is removed because it names a file on the
    machine that wrote it (see EXCLUDED_KEYS).
    """
    clean, ignored = {}, []
    if not isinstance(value, dict):
        return clean, ["sound_events"]
    for pack, events in value.items():
        if not isinstance(events, dict):
            ignored.append(f"sound_events.{pack}")
            continue
        kept = {}
        for event, cfg in events.items():
            if not isinstance(cfg, dict):
                ignored.append(f"sound_events.{pack}.{event}")
                continue
            entry = {k: copy.deepcopy(v) for k, v in cfg.items() if k != "path"}
            if "enabled" in entry and not isinstance(entry["enabled"], bool):
                entry.pop("enabled")
                ignored.append(f"sound_events.{pack}.{event}.enabled")
            if "path" in cfg:
                ignored.append(f"sound_events.{pack}.{event}.path")
            kept[event] = entry
        clean[pack] = kept
    return clean, ignored


def global_hotkey_is_valid(value) -> bool:
    """The same rule the Settings dialog's capture enforces: no hotkey at all,
    or a key with Ctrl or Alt (plus optionally Shift). A system-wide hotkey on a
    bare key would swallow that key in every other program."""
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    vk, mod = value.get("vk"), value.get("mod")
    if isinstance(vk, bool) or isinstance(mod, bool):
        return False
    if not isinstance(vk, int) or not isinstance(mod, int):
        return False
    if not 0 < vk < 256:
        return False
    if mod & ~(_MOD_ALT | _MOD_CONTROL | _MOD_SHIFT):
        return False
    return bool(mod & (_MOD_CONTROL | _MOD_ALT))


def _default_for(section, key=None):
    """The shipped default of a setting, or None when this build has no such
    setting. `sound_events` and friends ship as an empty dict, which means
    free-form (any key), not "no keys allowed"."""
    if section not in DEFAULT_SETTINGS:
        return None
    default = DEFAULT_SETTINGS[section]
    if key is None or not isinstance(default, dict):
        return default
    return default.get(key)


def _is_free_form(section) -> bool:
    default = DEFAULT_SETTINGS.get(section)
    return isinstance(default, dict) and not default


def _same_kind(value, default) -> bool:
    """Whether `value` is the same sort of thing as the shipped default.

    bool before int on purpose (True is an int in Python), and an int is
    accepted where a float is expected — a speed saved as 1 must still load.
    The one default that is None is global_hotkey, which is a pair or nothing.
    """
    if default is None:
        # global_hotkey: {"vk": ..., "mod": ...}, or None for "no hotkey".
        return value is None or isinstance(value, dict)
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, list)
    if isinstance(default, dict):
        return isinstance(value, dict)
    return False


def exportable_settings(settings) -> dict:
    """A deep copy of `settings` with everything this install owns removed."""
    out = {}
    for section, value in (settings or {}).items():
        # The same whitelist the import applies. Without it the export is a
        # blacklist: `privateinfo` is caught only because someone named it, and
        # the next section written straight into self.settings — which is how
        # privateinfo, cleared_chats and status_panel all arrived — would land
        # in a file the user mails to themselves with no test failing.
        if section not in DEFAULT_SETTINGS or is_excluded(section, None):
            continue
        if section == "sound_events":
            out[section], _ignored = _sound_events_without_paths(value)
        elif isinstance(value, dict):
            kept = {k: copy.deepcopy(v) for k, v in value.items()
                    if not is_excluded(section, k, settings)}
            if kept or not value:
                out[section] = kept
        else:
            out[section] = copy.deepcopy(value)
    return out


def build_export(settings, app_version: str = "") -> dict:
    """The document written to the file the user picks."""
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "app_version": str(app_version or ""),
        "settings": exportable_settings(settings),
    }


def read_export(payload):
    """(settings, error key) for a parsed export file.

    The error is an i18n key, so every refusal the user can hit is a sentence
    rather than a traceback: a file that is not an export at all, and one from
    a newer format this build cannot read.
    """
    if not isinstance(payload, dict) or payload.get("format") != EXPORT_FORMAT:
        return None, "settings_import_not_an_export"
    try:
        version = int(payload.get("version", 0))
    except (TypeError, ValueError):
        return None, "settings_import_not_an_export"
    if version > EXPORT_VERSION:
        return None, "settings_import_newer_version"
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        return None, "settings_import_not_an_export"
    return settings, ""


def api_endpoint(base, port, schemes):
    """`base` and `port` as the single address the app will really connect
    to — `scheme://host:port` — or None when that is not what they say.

    The app builds every URL as f"{base}:{port}/..." (REST from wpp_server,
    Socket.IO from wpp_ws_server). So the only form that means what it looks
    like is exactly `scheme://host`: userinfo turns `http://127.0.0.1@evil`
    into a request to `evil`, a path or port inside `base` puts the real port
    somewhere else, and whitespace or control characters can hide either. None
    of those is a working configuration anyway, so refusing them loses nothing.
    """
    if not isinstance(base, str) or not base or any(
            ch.isspace() or ord(ch) < 32 for ch in base):
        return None
    # A real int only, the one type merge_settings() saves for wpp_port: "8443"
    # would be shown here as :8443 while the port actually kept stays the old
    # one.
    if isinstance(port, bool) or not isinstance(port, int):
        return None
    try:
        parts = urlsplit(base)
        host = parts.hostname
    except (TypeError, ValueError):
        return None
    scheme = parts.scheme.lower()
    if scheme not in schemes or not host or not 0 < port < 65536:
        return None
    netloc = parts.netloc.lower()
    if netloc not in (host, f"[{host}]") or parts.path or parts.query or parts.fragment:
        return None
    return f"{scheme}://{netloc}:{port}"


_REST_SCHEMES = ("http", "https")
_WS_SCHEMES = ("ws", "wss", "http", "https")


def _connection_after_import(current, incoming):
    """The connection block as it would be if the file's were applied."""
    section = incoming.get("connection") if isinstance(incoming, dict) else None
    here = (current or {}).get("connection") if isinstance(current, dict) else None
    section = section if isinstance(section, dict) else {}
    here = here if isinstance(here, dict) else {}
    return {key: section.get(key, here.get(key)) for key in _CONNECTION_KEYS}, here


def custom_api_is_valid(current, incoming) -> bool:
    """Whether a file describing a custom API names addresses that mean what
    they look like (api_endpoint). A file on the bundled API is always valid:
    its addresses never travel (CUSTOM_API_KEYS)."""
    if not uses_custom_api(incoming):
        return True
    after, _here = _connection_after_import(current, incoming)
    return (api_endpoint(after["wpp_server"], after["wpp_port"], _REST_SCHEMES) is not None
            and api_endpoint(after["wpp_ws_server"], after["wpp_port"], _WS_SCHEMES) is not None)


def api_change(current, incoming):
    """Where an import would move this install's API, or None.

    Not None only when the file describes a valid custom API and applying it
    would change anything about the API this install talks to. That is the one
    import that decides where the session token is sent from now on — to BOTH
    addresses: every REST call carries it to wpp_server, and the Socket.IO
    connection sends it as `apikey` to wpp_ws_server — so both are named, as
    the host and port the app will really connect to, never as the raw string
    the file typed. Moving back to the bundled API is not asked about: that
    sends nothing anywhere new. An invalid custom API is not asked about
    either: merge_settings() refuses it outright.

    Returns {"server": ..., "ws_server": ...}.
    """
    if not uses_custom_api(incoming) or not custom_api_is_valid(current, incoming):
        return None
    after, here = _connection_after_import(current, incoming)
    if all(after[key] == here.get(key) for key in _CONNECTION_KEYS):
        return None
    return {
        "server": api_endpoint(after["wpp_server"], after["wpp_port"], _REST_SCHEMES),
        "ws_server": api_endpoint(after["wpp_ws_server"], after["wpp_port"], _WS_SCHEMES),
    }


def merge_settings(current, incoming, include_connection=True):
    """(the settings to save, how many values were applied, what was ignored).

    Applied onto a copy of `current`, so anything the file does not mention —
    including everything the export leaves behind — keeps the value this
    install already had. `include_connection` False leaves the API this
    install talks to exactly as it is (the person declined api_change()).
    """
    merged = copy.deepcopy(current or {})
    applied = 0
    ignored = []
    for section, value in (incoming or {}).items():
        if is_excluded(section, None) or section not in DEFAULT_SETTINGS:
            ignored.append(str(section))
            continue
        if section == "connection" and (
                not include_connection or not custom_api_is_valid(current, incoming)):
            ignored.append(str(section))
            continue
        if section == "sound_events":
            if isinstance(value, dict):
                clean, dropped = _sound_events_without_paths(value)
                # Merged per pack and event, so a path this install already has
                # for an event survives the import of that event's switch.
                target = merged.setdefault("sound_events", {})
                for pack, events in clean.items():
                    pack_target = target.setdefault(pack, {})
                    if not isinstance(pack_target, dict):
                        pack_target = target[pack] = {}
                    for event, entry in events.items():
                        existing = pack_target.get(event)
                        base = dict(existing) if isinstance(existing, dict) else {}
                        base.update(entry)
                        pack_target[event] = base
                applied += 1
                ignored.extend(dropped)
            else:
                ignored.append(str(section))
            continue
        if _is_free_form(section):
            # Free-form in its KEYS (a sound event name, a JID), never in its
            # values: a `sound_events` holding a number instead of a dict is
            # saved happily here and then crashes load_sounds() on the next
            # launch, before the window exists — with hand-editing
            # settings.json the only way back, which is the very thing this
            # feature exists to spare people.
            if isinstance(value, dict):
                kept = {k: copy.deepcopy(v) for k, v in value.items()
                        if isinstance(v, dict)}
                merged[section] = kept
                applied += 1
                ignored.extend(f"{section}.{k}" for k, v in value.items()
                               if not isinstance(v, dict))
            else:
                ignored.append(str(section))
            continue
        if not isinstance(DEFAULT_SETTINGS[section], dict):
            if _same_kind(value, DEFAULT_SETTINGS[section]):
                merged[section] = copy.deepcopy(value)
                applied += 1
            else:
                ignored.append(str(section))
            continue
        if not isinstance(value, dict):
            ignored.append(str(section))
            continue
        target = merged.setdefault(section, {})
        languages = known_languages() if section == "general" else None
        for key, item in value.items():
            # Judged by what the FILE says about the API, not by this install:
            # importing a custom-API setup is exactly how the other computer
            # comes to use that same server.
            if is_excluded(section, key, incoming) or key not in DEFAULT_SETTINGS[section]:
                ignored.append(f"{section}.{key}")
                continue
            if not _same_kind(item, _default_for(section, key)):
                ignored.append(f"{section}.{key}")
                continue
            if section == "general" and key == "global_hotkey" and not global_hotkey_is_valid(item):
                ignored.append(f"{section}.{key}")
                continue
            if section == "general" and key == "language" and not _language_is_usable(
                    item, languages):
                # A locale this build does not ship turns every label and every
                # spoken announcement into its own key name — including the box
                # reporting this import, and the Settings dialog needed to undo
                # it. Locales are data (CLAUDE.md), so an export from a newer
                # install really can name one.
                ignored.append(f"{section}.{key}")
                continue
            target[key] = copy.deepcopy(item)
            applied += 1
    return merged, applied, ignored
