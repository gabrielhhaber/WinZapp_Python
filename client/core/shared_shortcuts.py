"""Keyboard shortcuts are a preference of the install, not of an account.

Each account runs in a process of its own with its own settings.json, and the
shortcut overrides used to live there: a combination changed in one account
did nothing in the others. A user who set Ctrl+Shift+3 and Ctrl+Shift+4 for
"switch to account 1/2" in account 1 could reach account 2 with Ctrl+Shift+4,
but from account 2 only the shipped Ctrl+Alt+1 brought them back -- and the
key they had chosen to go back to all conversations from the archived list
did nothing there either (user report, 2026-11-19).

The overrides now live in global/app.json (app_settings, key SECTION) and
every account keeps a mirror in settings[SECTION], which the readers of
shortcuts already use. MainWindow._persist_global_settings() keeps the mirror
in step like the other install-wide values: a change made here is written,
one made in another account is pulled and applied.
"""
from core.keyboard_shortcuts import SECTION, conflicts, sanitize_overrides

#: One-shot, per account (settings["general"]). Ends in "_migrated" so a
#: settings export never carries it (core/settings_transfer.py).
MIGRATED_FLAG = "keyboard_shortcuts_shared_migrated"


def merge_account_overrides(shared, local, catalog, translate):
    """The install's overrides plus those of this account it does not set.

    The install wins on a command both set. An account's own override whose
    combination another command already answers to in the result is left out:
    keeping it would give one key two commands. Repeated until nothing more
    fits, so an override that only becomes free once another one has moved a
    command off its shipped key is not lost to the order of the dict.
    """
    merged = sanitize_overrides(shared, catalog)
    pending = {key: binding for key, binding in sanitize_overrides(local, catalog).items()
               if key not in merged}
    first_row = {}
    for shortcut in catalog:
        first_row.setdefault(shortcut.id, shortcut)
    added = True
    while pending and added:
        added = False
        for key, binding in list(pending.items()):
            if binding is not None and conflicts(first_row[key], binding, merged,
                                                 catalog, translate):
                continue
            merged[key] = binding
            del pending[key]
            added = True
    return merged


def catalog_translate(language):
    """A translate() for `language` before the window's I18n exists.

    The one-shot merge runs from load_settings(), ahead of MainWindow.i18n,
    and the shipped key of a mnemonic command ("@messages") is the letter of
    its translated label. No language yet: nothing to resolve, and on that
    first run an account has no overrides of its own anyway.
    """
    catalog = {}
    if language:
        from core.translation_catalog import load_catalog
        catalog = load_catalog(language)
    return lambda key: catalog.get(key, key)


def share_account_overrides(app, settings, catalog, translate_factory):
    """Make settings[SECTION] the install's overrides; return them.

    The first time an account starts on this version, its own overrides are
    handed to the install first (merge_account_overrides()), so whatever the
    user had set in any account survives the move. `translate_factory` is
    called only then.
    """
    general = settings.setdefault("general", {})
    if not general.get(MIGRATED_FLAG):
        local = sanitize_overrides(settings.get(SECTION), catalog)
        if local:
            translate = translate_factory()
            app.update(SECTION, lambda shared: merge_account_overrides(
                shared, local, catalog, translate))
        general[MIGRATED_FLAG] = True
    shared = sanitize_overrides(app.get(SECTION), catalog)
    settings[SECTION] = shared
    return shared


#: settings["general"]: does a shortcut with no choice of its own follow the
#: install (True, the default) or keep a key for this account only?
SHARED_FLAG = "keyboard_shortcuts_shared"
#: settings: {shortcut id: True (install) / False (this account)}, only the
#: commands whose own check box was set apart from the general one.
SCOPE_KEY = "keyboard_shortcuts_scope"
#: settings: this account's own overrides, kept while a command follows the
#: install, so moving it back to the account brings its key back.
OWN_KEY = "keyboard_shortcuts_own"


def shares_by_default(settings):
    return (settings.get("general") or {}).get(SHARED_FLAG, True) is not False


def is_shared(shortcut_id, scope, default_shared):
    """Whether one command takes the install's key: its own choice if it has
    one, else the general check box."""
    choice = (scope or {}).get(shortcut_id)
    return choice if isinstance(choice, bool) else default_shared


def scope_choice(shared, default_shared):
    """What to store for a command's check box: nothing when it agrees with
    the general one (it follows that one from then on), else the choice."""
    return None if shared == default_shared else shared


def effective_overrides(shared, own, scope, default_shared):
    """The overrides this account answers to: each command from the install
    or from the account, as its check box says."""
    result = {k: v for k, v in (shared or {}).items()
              if is_shared(k, scope, default_shared)}
    result.update({k: v for k, v in (own or {}).items()
                   if not is_shared(k, scope, default_shared)})
    return result


def route_effective(effective, shared, own, scope, default_shared):
    """(shared, own) holding `effective` -- a whole set chosen at once, an
    import -- each command in the home its check box names. A command's entry
    in the other home is left as it was: it is another account's key, or this
    account's own kept for later."""
    new_shared = {k: v for k, v in (shared or {}).items()
                  if not is_shared(k, scope, default_shared)}
    new_own = {k: v for k, v in (own or {}).items()
               if is_shared(k, scope, default_shared)}
    for key, binding in (effective or {}).items():
        target = new_shared if is_shared(key, scope, default_shared) else new_own
        target[key] = binding
    return new_shared, new_own


def apply_changes(base, before, after):
    """`base` with what changed from `before` to `after`, and nothing else.

    The Settings page edits the install's overrides as it found them on
    opening; writing its whole copy back would undo a change another account
    made since. Only the commands the page itself changed are carried."""
    missing = object()
    result = dict(base or {})
    for key in set(before or {}) | set(after or {}):
        old, new = (before or {}).get(key, missing), (after or {}).get(key, missing)
        if old == new:
            continue
        if new is missing:
            result.pop(key, None)
        else:
            result[key] = new
    return result


def own_seeded(shared, own, scope, was_default, now_default):
    """The account's own overrides once the general check box moves from the
    install to the account: a command following it that has no key of its own
    yet starts from the install's, so nothing changes under the user's hands."""
    result = dict(own or {})
    if not was_default or now_default:
        return result
    for key, binding in (shared or {}).items():
        if not isinstance((scope or {}).get(key), bool) and key not in result:
            result[key] = binding
    return result


def shortcuts_to_pull(local, snapshot, shared):
    """The install's overrides when this account should take them now, else None.

    None when the install holds what this account last took from it (nothing
    changed elsewhere), and when this account changed its own copy since:
    that change is its own save's to write, and taking the install's over it
    would undo what the user just chose.
    """
    if local != snapshot or shared == snapshot:
        return None
    return shared
