"""WinZapp's npm keeps its own cache and its own configuration.

Every npm and npx WinZapp runs (the API install, the missing-package repair,
the browser download, the build, start.js's own npx fallback) used to run with
the user's environment untouched, so it shared the user's npm cache
(%LOCALAPPDATA%\\npm-cache) and read their ~/.npmrc. Reported from a Windows
machine: after a WinZapp update, WinZapp worked and the user's own npm, for
their own projects, did not. The update had relaunched WinZapp as
Administrator (updater.py's elevated installer), so every npm under it wrote
into that shared cache as Administrator, leaving files the user's own,
non-elevated npm could no longer replace. The cache being shared at all was
the bug; the elevation only made it visible.

So every call site builds its environment through npm_environment():

* npm_config_cache points to <global data>/npm/cache — outside the API
  directory, which an update replaces, so it survives exactly like
  NODE_COMPILE_CACHE (core/node_compile_cache.py). Always overridden, even
  when the user set a cache of their own: that cache is the one this exists
  to stay out of.
* npm_config_userconfig points to <global data>/npm/npmrc, a file WinZapp
  never creates — npm reads a missing userconfig as empty, exactly as it
  does for everyone without a ~/.npmrc (@npmcli/config only logs a load
  error that is not ENOENT). The user's settings reach npm as variables
  instead (below), minus the ones that say where npm writes. npm creates
  the cache directory itself, so building this environment writes nothing
  to disk.
* npm_config_update_notifier=false: the notice is useless in a hidden
  window, and checking it is one more write into the cache.
* npm_config_logs_dir points to that cache's _logs directory, even when
  the parent environment or a user's npmrc redirects npm logs elsewhere.

The user's npmrc is not simply dropped: it is where someone behind a proxy
or a registry mirror tells npm how to reach the internet, and npm hands
every one of its keys to install scripts as npm_config_* (that is how a
puppeteer download mirror or node-gyp's python reach them — @npmcli/config's
set-envs.js). So its settings are forwarded as npm_config_* variables
instead, and only where that is exactly equivalent to npm reading the file:

* Only keys made of a-z, 0-9 and - are forwarded. npm reads a file key
  literally, but an npm_config_* name back with _ as - and lower-cased
  (loadEnv), so `strict_ssl=false` or `Registry=x` in the file did nothing
  to npm, and forwarded they would become strict-ssl and registry and take
  effect. Skipping such a key would not be equivalent either (set-envs.js
  still hands it to install scripts), so a file with one is kept as npm's
  userconfig instead.
* A key the API's own .npmrc also sets (project_dir; wppconnect-server
  ships legacy-peer-deps=true) is not forwarded: the project file ranks
  above the user's, but below the environment.
* The four settings that are WinZapp's to decide (OWN_SETTINGS) are never
  taken from the user.
* A value already in the environment wins, as it did before (npm ranks the
  environment above every npmrc).

Never written to a file, since a proxy URL can carry a password.

The user's file is still read as npm's userconfig, as it always was, when
it holds anything that cannot be forwarded that way: credentials or
anything credential-like, per-registry (//host/:...) or @scoped settings,
private _keys, a top-level cert/key, an array (key[]=), an [ini section],
an empty value, a JSON object, a key outside a-z0-9-, a control character,
or bytes that are not clean UTF-8 (a UTF-16 file, which is what Windows
PowerShell 5.1's `echo x > ~/.npmrc` writes; npm reads it as UTF-8 too, and
gets nothing usable from it, so keeping it changes nothing). The cache is
WinZapp's own either way.

parse_npmrc() follows the ini package npm itself uses, line for line: ini
5.0.0 as bundled with npm 10.9.7 in Node 22.22.2 (client/node_download_config.py),
lib/ini.js decode() and unsafe(), with JavaScript's trim() and \\s (which
include U+FEFF, so a UTF-8 byte-order mark disappears from the first key
exactly as it does for npm). That means a ; or # anywhere in an unquoted
value starts a comment unless escaped as \\; or \\#, a quoted value is
JSON-decoded (single quotes stripped first), a key with no = is true, and
the last of two plain keys wins.
"""

import json
import logging
import os
import re
import sys

#: npm settings WinZapp sets itself (or that only say where npm writes);
#: never forwarded from the user's npmrc.
OWN_SETTINGS = ("cache", "userconfig", "update-notifier", "logs-dir")

#: Top-level keys that carry or point to credentials.
_CREDENTIAL_KEYS = {"cert", "key", "certfile", "keyfile", "username", "email"}
_CREDENTIAL_WORDS = ("auth", "token", "password", "passwd", "secret")

#: JavaScript's whitespace (WhiteSpace + LineTerminator): what String.trim()
#: removes and what \s matches in npm's ini. Python's str.strip() differs —
#: it keeps U+FEFF and removes \x1c-\x1f.
_JS_SPACE = ("\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
             "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff")
_S = "[" + re.escape(_JS_SPACE) + "]"
_INI_LINE = re.compile(r"^\[([^\]]*)\]" + _S + r"*$|^([^=]+)(=([^\u2028\u2029]*))?$")
_FORWARDABLE_KEY = re.compile(r"[a-z0-9-]+")


def _get(environment, key):
    """*key* from *environment*, ignoring case: Windows (and npm) treat
    environment names case-insensitively, and os.environ upper-cases them."""
    for name, value in environment.items():
        if name.lower() == key.lower():
            return value
    return None


def _set(environment, key, value):
    """Set *key*, removing every other spelling of it first. A copy of
    os.environ on Windows already holds NPM_CONFIG_CACHE in upper case, and
    two spellings of one name in a child's environment block leave which
    one npm sees to chance."""
    for name in [n for n in environment if n.lower() == key.lower()]:
        del environment[name]
    environment[key] = value


def user_npmrc_path(environment) -> str:
    """The npmrc npm itself would read for this user."""
    explicit = _get(environment, "npm_config_userconfig")
    if explicit:
        return explicit
    home = _get(environment, "USERPROFILE" if sys.platform == "win32" else "HOME")
    return os.path.join(home or os.path.expanduser("~"), ".npmrc")


def _ini_is_quoted(value: str) -> bool:
    return ((value.startswith('"') and value.endswith('"'))
            or (value.startswith("'") and value.endswith("'")))


def _reject_constant(name):
    raise ValueError(name)  # JSON.parse has no NaN/Infinity


def _ini_unsafe(value: str):
    """ini's unsafe(): trim, then JSON-decode a quoted value, or cut an
    unquoted one at its first unescaped ; or #."""
    value = (value or "").strip(_JS_SPACE)
    if _ini_is_quoted(value):
        if value[0] == "'":
            value = value[1:-1]
        try:
            return json.loads(value, parse_constant=_reject_constant)
        except ValueError:
            return value
    out, escaped = [], False
    for ch in value:
        if escaped:
            out.append(ch if ch in "\\;#" else "\\" + ch)
            escaped = False
        elif ch in ";#":
            break
        elif ch == "\\":
            escaped = True
        else:
            out.append(ch)
    if escaped:
        out.append("\\")
    return "".join(out).strip(_JS_SPACE)


def parse_npmrc(text: str) -> list:
    """``(section, key, value, is_array)`` for each setting of an npmrc, in
    file order, decoded as npm's ini package decodes it (module docstring).
    section is None at the top level; value is what JSON.parse or the
    string itself gives (True for a key with no =)."""
    entries = []
    section = None
    for line in re.split(r"[\r\n]+", text or ""):
        if not line or re.match("^" + _S + "*[;#]", line) or re.match("^" + _S + "*$", line):
            continue
        match = _INI_LINE.match(line)
        if not match:
            continue
        if match.group(1) is not None:
            section = _ini_unsafe(match.group(1))
            continue
        key_raw = _ini_unsafe(match.group(2))
        if not isinstance(key_raw, str):
            key_raw = _js_string(key_raw)
        is_array = len(key_raw) > 2 and key_raw.endswith("[]")
        key = key_raw[:-2] if is_array else key_raw
        if key == "__proto__":
            continue
        value = _ini_unsafe(match.group(4)) if match.group(3) else True
        if value in ("true", "false", "null"):
            value = json.loads(value)
        entries.append((section, key, value, is_array))
    return entries


def _js_string(value):
    """What String(value) gives in JavaScript, for the scalar types JSON
    can produce; None for an object or array, which cannot be forwarded."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, (int, str)):
        return str(value)
    return None


def _needs_the_file(key: str) -> bool:
    lowered = key.lower()
    return (lowered.startswith(("//", "@", "_"))
            or lowered in _CREDENTIAL_KEYS
            or any(word in lowered for word in _CREDENTIAL_WORDS))


def _has_control_character(text: str) -> bool:
    """Any C0 control character or DEL besides tab and the line breaks: no
    environment variable may carry one (a NUL makes Popen raise)."""
    return any((ord(ch) < 0x20 and ch not in "\t\r\n") or ord(ch) == 0x7f
               for ch in text)


def _top_level_keys(text: str) -> set:
    return {key for section, key, _, _ in parse_npmrc(text) if section is None}


def forwarded_settings(text: str, project_text: str = ""):
    """``(settings, needs_user_config)`` for the user's npmrc *text*.

    settings maps each npm_config_* variable name to its value.
    needs_user_config is True when the file holds something forwarding
    cannot reproduce exactly (module docstring) — then it is kept as npm's
    userconfig instead. *project_text* is the API's own .npmrc.
    """
    if _has_control_character(text):
        return {}, True
    project_keys = _top_level_keys(project_text)
    settings = {}
    for section, key, value, is_array in parse_npmrc(text):
        if section is not None or is_array or _needs_the_file(key):
            return {}, True
        if not _FORWARDABLE_KEY.fullmatch(key):
            return {}, True
        rendered = _js_string(value)
        if rendered is None or rendered == "" or _has_control_character(rendered + key):
            # npm ignores an empty npm_config_* variable, and an object has
            # no string form: only the file can say these.
            return {}, True
        if key.lower() in OWN_SETTINGS or key in project_keys:
            continue
        settings["npm_config_" + key.replace("-", "_")] = rendered
    return settings, False


def _read_utf8(path: str):
    """The text of *path*, "" when it does not exist, None when it exists
    but is not clean UTF-8 (or cannot be read)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return ""
    except OSError:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def npm_environment(environment, npm_dir: str, project_dir: str = "") -> dict:
    """A copy of *environment* for an npm/npx run of WinZapp's.

    *npm_dir* is WinZapp's own npm directory (``global_dir("npm")``);
    *project_dir* is the directory npm runs in (the API's), whose .npmrc
    keeps outranking the user's settings. The caller's mapping is never
    modified.
    """
    child = dict(environment)
    _set(child, "npm_config_update_notifier", "false")
    _set(child, "npm_config_cache", os.path.abspath(os.path.join(npm_dir, "cache")))
    _set(child, "npm_config_logs_dir", os.path.abspath(os.path.join(npm_dir, "cache", "_logs")))

    # No npmrc is the common case. An unreadable or undecodable one is left
    # to npm, exactly as before.
    user_text = _read_utf8(user_npmrc_path(environment))
    project_text = _read_utf8(os.path.join(project_dir, ".npmrc")) if project_dir else ""
    if user_text is None or project_text is None:
        settings, needs_user_config = {}, True
    else:
        settings, needs_user_config = forwarded_settings(user_text, project_text)
    if needs_user_config:
        # Not logged with the path or contents: an npmrc is where tokens live.
        logging.info("[npm-env] the user's npmrc holds registry credentials or "
                     "settings that cannot be forwarded; npm still reads it")
        return child
    _set(child, "npm_config_userconfig", os.path.abspath(os.path.join(npm_dir, "npmrc")))
    for env_key, value in settings.items():
        if _get(child, env_key) is None:
            child[env_key] = value
    return child
