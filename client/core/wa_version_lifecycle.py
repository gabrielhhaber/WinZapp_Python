"""Catalogue identity and a short, per-API mutation gate (no wx or Node).

The gate lives beside api/, so replacing api/ cannot replace the lock itself.
Downloads never hold it. A setup owns it while rebuilding its target tree;
publishing/applying a catalogue and swapping/rolling back an API own it too.
"""
import contextlib
import hashlib
import json
import os
import tempfile
import threading


_owned = threading.local()


def filesystem_path(path):
    """Copying a catalogue can exceed MAX_PATH even when its root does not."""
    path = os.path.abspath(path)
    if os.name != 'nt' or path.startswith('\\\\?\\'):
        return path
    if path.startswith('\\\\'):
        return '\\\\?\\UNC\\' + path[2:]
    return '\\\\?\\' + path


@contextlib.contextmanager
def tree_lock(api_dir):
    """Nonblocking cross-process gate, reentrant only on the owning thread."""
    from core.wa_version_refresh import exclusive_lock

    key = os.path.normcase(os.path.realpath(api_dir)) + '.catalogue.lock'
    owned = getattr(_owned, 'paths', set())
    if key in owned:
        yield True
        return
    with exclusive_lock(key) as held:
        if held:
            _owned.paths = owned | {key}
        try:
            yield held
        finally:
            if held:
                _owned.paths = owned


def package_target(node_modules):
    """Match start.js: the copy resolved from WPPConnect, then the hoisted one.

    Presence wins over validity: Node does not fall through a broken nested
    package to a different, healthy hoisted catalogue.
    """
    wpp = os.path.join(node_modules, '@wppconnect-team', 'wppconnect')
    for base in (os.path.join(wpp, 'node_modules'),
                 os.path.join(node_modules, '@wppconnect-team', 'node_modules')):
        candidate = os.path.join(base, '@wppconnect', 'wa-version')
        if os.path.isdir(candidate):
            return candidate
    return os.path.join(node_modules, '@wppconnect', 'wa-version')


def resolution_roots(target, node_modules):
    """Node's ancestor node_modules search, bounded to this API tree."""
    root = os.path.realpath(node_modules)
    current = os.path.realpath(target)
    result = [os.path.join(target, 'node_modules')]
    while os.path.commonpath([root, current]) == root:
        if os.path.basename(current) != 'node_modules':
            result.append(os.path.join(current, 'node_modules'))
        if current == root:
            break
        current = os.path.dirname(current)
    if root not in result:
        result.append(root)
    return list(dict.fromkeys(result))


def catalogue_identity(node_modules, target, wanted_range):
    """Detect same-version reinstall, API swap and manifest/content changes.

    File identity/mtime/size avoids hashing 160 MB of catalogue HTML at every
    launch. Sanity validation remains a separate prerequisite for throttling.
    """
    parts = [os.path.normcase(os.path.realpath(target)), wanted_range]
    for path in (os.path.dirname(node_modules), node_modules, target,
                 os.path.join(target, 'package.json'),
                 os.path.join(target, 'versions.json')):
        try:
            st = os.stat(filesystem_path(path))
            parts.append([st.st_dev, st.st_ino, st.st_mtime_ns if os.path.isfile(filesystem_path(path)) else 0,
                          st.st_size if os.path.isfile(filesystem_path(path)) else 0])
        except OSError:
            parts.append(None)
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def write_state(path, state):
    """Replace one complete JSON record; failure leaves the prior record intact."""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.wa-check-', dir=folder)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(state, stream)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
