"""Preserve and refresh the expiring catalogue while npm rebuilds an API.

Only called on an installer worker, before it announces success. Staging
builds mutate their own tree, never the running server's. No npm update of
the executable WPPConnect/WA-JS pair, and no new direct catalogue dependency.
"""
import contextlib
import logging
import os
import shutil
import tempfile
import time
import uuid

from core import wa_version_refresh as wr
from core.wa_version_lifecycle import filesystem_path, tree_lock


class CatalogueSetupError(Exception):
    """A usable catalogue could not be retained in the new server."""


def _preserve_newer(snapshot, node_modules):
    previous = wr._installed_version(snapshot)
    target = wr._target(node_modules)
    installed = wr._installed_version(target)
    if not previous or not installed:
        return
    if wr.parse_version(previous) <= wr.parse_version(installed):
        return
    # A snapshot may belong to a different server release. Unknown compatibility
    # is not permission to replace the catalogue npm selected for the new one.
    wanted = wr._wanted_range(node_modules)
    if (wr.parse_version(previous)[0] != wr.parse_version(installed)[0]
            or wr.satisfies_range(previous, wanted) is not True):
        logging.warning('[wa-version] previous catalogue incompatible with rebuilt API')
        return
    if not wr.dependencies_satisfied(wr._read_json(os.path.join(snapshot, 'package.json')),
                                     snapshot, node_modules, resolution_target=target):
        raise CatalogueSetupError('previous catalogue dependencies unavailable')
    staged = wr._staged_path(target, previous)
    # A private complete copy, never reuse an unrelated incomplete stage.
    temporary = staged + '.copy-' + uuid.uuid4().hex
    try:
        shutil.copytree(filesystem_path(snapshot), filesystem_path(temporary))
        if not wr.sanity_check(temporary, previous):
            raise CatalogueSetupError('previous catalogue copy invalid')
        if os.path.isdir(filesystem_path(staged)):
            shutil.rmtree(filesystem_path(staged))
        os.rename(filesystem_path(temporary), filesystem_path(staged))
        outcome = wr._apply_one(node_modules, target, staged, previous, installed, time.time(),
                                carry_dependencies=False)
        if outcome != 'updated':
            raise CatalogueSetupError('could not preserve previous catalogue')
    finally:
        shutil.rmtree(filesystem_path(temporary), ignore_errors=True)


def finish_catalogue_setup(api_dir, snapshot, state_dir, *, cancelled=lambda: False,
                           fetch=None):
    """Private new-tree check bypasses live throttle; offline retains a valid copy.

    /latest outside the supported major/range is explicitly 'unsupported', not
    evidence of freshness. We do not resolve a whole changing dependency graph
    just to refresh the HTML catalogue. Missing new dependencies likewise remain
    an explicit skipped-deps result, with the previous working copy retained.
    """
    if cancelled():
        return 'cancelled'
    nm = os.path.join(api_dir, 'node_modules')
    _preserve_newer(snapshot, nm)
    if cancelled():
        return 'cancelled'
    kwargs = {} if fetch is None else {'fetch': fetch}
    checked = wr.check_and_stage(nm, state_dir, interval=0, **kwargs)
    if cancelled():
        return 'cancelled'
    applied = wr.apply_staged(nm, state_dir)
    target = wr._target(nm)
    installed = wr._installed_version(target)
    if not installed or not wr.sanity_check(target, installed):
        raise CatalogueSetupError('rebuilt API has no usable WhatsApp catalogue')
    if checked in {'offline', 'unsupported', 'skipped-deps', 'rejected', 'error', 'changed', 'locked'}:
        logging.warning('[wa-version] setup catalogue freshness unconfirmed: %s', checked)
    if applied not in {'updated', 'nothing-staged'}:
        logging.warning('[wa-version] setup catalogue apply: %s', applied)
    logging.info('[wa-version] setup catalogue: checked=%s applied=%s installed=%s',
                 checked, applied, installed)
    return checked


@contextlib.contextmanager
def catalogue_setup(api_dir, previous_api_dir, *, cancelled=lambda: False, fetch=None):
    """Yield the post-npm action, or None without changing the tree if busy.

    The snapshot is outside api/, so cleaning/extracting that tree cannot delete
    a newer known catalogue. Snapshot failure aborts before the destructive step.
    This gate is also used by API swap/rollback and by runtime apply/publish.
    """
    with tree_lock(api_dir) as held:
        if not held:
            yield None
            return
        parent = os.path.dirname(os.path.abspath(api_dir))
        os.makedirs(parent, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.wa-setup-', dir=parent) as scratch:
            snapshot = os.path.join(scratch, 'previous')
            with tree_lock(previous_api_dir) as source_held:
                if not source_held:
                    yield None
                    return
                target = wr._target(os.path.join(previous_api_dir, 'node_modules'))
                version = wr._installed_version(target)
                if version and wr.sanity_check(target, version):
                    shutil.copytree(filesystem_path(target), filesystem_path(snapshot))
            try:
                yield lambda: finish_catalogue_setup(api_dir, snapshot,
                    os.path.join(scratch, 'state'), cancelled=cancelled, fetch=fetch)
            finally:
                # Cancellation can land just after npm returned an older copy,
                # before the caller reaches its post-install action. Restore a
                # compatible snapshot locally even on that early-return path.
                # A staged build never changed the source API in the first place.
                if os.path.realpath(api_dir) == os.path.realpath(previous_api_dir):
                    _preserve_newer(snapshot, os.path.join(api_dir, 'node_modules'))
