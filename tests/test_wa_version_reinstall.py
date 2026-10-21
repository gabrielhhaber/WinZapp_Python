"""Reinstall/rollback must not inherit a different catalogue's six-hour verdict.

All installations are tiny tmp_path trees; registry and npm are fakes. Nothing
opens wx, a browser, a device, or a network connection.
"""
import json
import os
from pathlib import Path
import shutil
import threading

import pytest

from core import api_staging, wa_version_refresh as wr
from core import wa_version_lifecycle as life
from core.wa_version_setup import catalogue_setup, CatalogueSetupError
from core.wa_version_setup import finish_catalogue_setup
from tests.test_wa_version_refresh import (
    tree, _settle_discards, _write_package, _fetcher, _good_tarball, _sri, TARBALL,
)


def offline(*args):
    raise OSError('offline')


@pytest.mark.parametrize('version', ['1.5.4480', '1.5.4490', '1.5.4500'])
def test_changed_or_same_version_reinstall_bypasses_old_verdict(tree, version):
    assert wr.check_and_stage(tree.nm, tree.state, fetch=_fetcher(), clock=lambda: 100) == 'staged'
    shutil.rmtree(tree.target)
    _write_package(tree.target, version)
    # npm also reconstructs the package's dependencies.
    nested = Path(tree.target, 'node_modules/semver')
    nested.mkdir(parents=True)
    (nested / 'package.json').write_text('{"version":"7.8.5"}')
    calls = []
    assert wr.check_and_stage(tree.nm, tree.state, fetch=_fetcher(calls=calls),
                              clock=lambda: 101) != 'throttled'
    assert calls


@pytest.mark.parametrize('state', [{}, {'checked': 100}, {'checked': True},
                                  {'checked': float('inf')}, {'checked': '100'}])
def test_legacy_or_invalid_state_cannot_throttle(tree, state):
    Path(tree.state).mkdir()
    Path(tree.state, wr._STATE_FILE).write_text(json.dumps(state))
    assert wr.check_and_stage(tree.nm, tree.state, fetch=offline, clock=lambda: 101) == 'offline'


def test_apply_keeps_checked_time_for_the_package_it_actually_promoted(tree):
    wr.check_and_stage(tree.nm, tree.state, fetch=_fetcher(), clock=lambda: 100)
    assert wr.apply_staged(tree.nm, tree.state) == 'updated'
    assert wr.check_and_stage(tree.nm, tree.state, fetch=lambda *a: pytest.fail('redundant GET'),
                              clock=lambda: 101) == 'throttled'


def test_missing_html_bypasses_throttle_and_repairs_equal_version(tree):
    latest = {'version': '1.5.4490', 'dependencies': {},
              'dist': {'tarball': TARBALL, 'integrity': _sri(_good_tarball('1.5.4490'))}}
    fetch = _fetcher(tarball=_good_tarball('1.5.4490'), latest=latest)
    assert wr.check_and_stage(tree.nm, tree.state, fetch=fetch, clock=lambda: 100) == 'current'
    shutil.rmtree(Path(tree.target, 'html'))
    assert wr.check_and_stage(tree.nm, tree.state, fetch=fetch, clock=lambda: 101) == 'staged'
    assert wr.apply_staged(tree.nm, tree.state) == 'updated'
    assert wr.sanity_check(tree.target, '1.5.4490')


def test_nested_catalogue_is_refreshed_not_unrelated_hoisted_copy(tree):
    nested = Path(tree.nm, '@wppconnect-team/wppconnect/node_modules/@wppconnect/wa-version')
    nested.parent.mkdir(parents=True)
    shutil.copytree(tree.target, nested)
    _write_package(tree.target, '1.5.9999')
    assert wr._target(tree.nm) == str(nested)
    assert wr.check_and_stage(tree.nm, tree.state, fetch=_fetcher()) == 'staged'
    assert wr.apply_staged(tree.nm, tree.state) == 'updated'
    assert wr._installed_version(str(nested)) == '1.5.4964'
    assert wr._installed_version(tree.target) == '1.5.9999'


def test_api_replaced_during_download_never_receives_old_result(tree):
    fetch = _fetcher()
    replaced = False
    def swap_during_fetch(url, *args):
        nonlocal replaced
        data = fetch(url, *args)
        if url == TARBALL:
            old = tree.nm + '.previous'
            os.rename(tree.nm, old)
            shutil.copytree(old, tree.nm)
            _write_package(tree.target, '1.5.6000')
            replaced = True
        return data
    assert wr.check_and_stage(tree.nm, tree.state, fetch=swap_during_fetch) == 'changed'
    assert replaced
    assert not Path(tree.state, wr._STATE_FILE).exists()
    assert not wr._siblings(tree.target, 'staged')
    assert wr._installed_version(tree.target) == '1.5.6000'


def test_completed_startup_check_can_restart_for_reinstalled_tree(tree, monkeypatch):
    monkeypatch.setattr(wr, '_thread', None)
    monkeypatch.setattr(wr, '_paths', None)
    assert wr.start_in_background(tree.nm, tree.state, fetch=offline)
    wr._thread.join(5)
    assert not wr.start_in_background(tree.nm, tree.state, fetch=offline)
    _write_package(tree.target, '1.5.4500')
    assert wr.start_in_background(tree.nm, tree.state, fetch=offline)
    wr._thread.join(5)
    assert not wr._thread.is_alive()


@pytest.mark.parametrize('staging', [False, True])
@pytest.mark.parametrize('online', [False, True])
def test_reinstall_preserves_newer_compatible_catalogue_even_offline(tree, staging, online):
    _write_package(tree.target, '1.5.5000')
    old_api = str(tree.root)
    api = str(tree.root / 'staging') if staging else old_api
    with catalogue_setup(api, old_api, fetch=_fetcher() if online else offline) as finish:
        assert finish is not None
        nm = Path(api, 'node_modules')
        if nm.exists():
            shutil.rmtree(nm)
        # Simulate an npm install selecting the older locked/cache version.
        target = nm / '@wppconnect/wa-version'
        _write_package(str(target), '1.5.4490')
        wpp = nm / '@wppconnect-team/wppconnect'
        wpp.mkdir(parents=True)
        (wpp / 'package.json').write_text('{"dependencies":{"@wppconnect/wa-version":"^1.5.4472"}}')
        dep = nm / 'node-fetch'
        dep.mkdir()
        (dep / 'package.json').write_text('{"version":"2.7.0"}')
        assert finish() in {'current', 'offline'}
        assert wr._installed_version(str(target)) == '1.5.5000'
        assert list(Path(api).parent.glob('.wa-setup-*'))  # owned until context exits
    assert not list(Path(api).parent.glob('.wa-setup-*'))


def test_invalid_npm_catalogue_prevents_setup_success(tree):
    with catalogue_setup(str(tree.root), str(tree.root), fetch=offline) as finish:
        shutil.rmtree(Path(tree.target, 'html'))
        with pytest.raises(CatalogueSetupError):
            finish()


def test_first_setup_applies_verified_online_update_before_success(tree):
    assert finish_catalogue_setup(str(tree.root), str(tree.root / 'no-snapshot'),
                                  tree.state, fetch=_fetcher()) == 'staged'
    assert wr._installed_version(tree.target) == '1.5.4964'


def test_offline_setup_retains_usable_catalogue_and_reports_uncertain_freshness(tree, caplog):
    assert finish_catalogue_setup(str(tree.root), str(tree.root / 'no-snapshot'),
                                  tree.state, fetch=offline) == 'offline'
    assert wr._installed_version(tree.target) == '1.5.4490'
    assert 'freshness unconfirmed: offline' in caplog.text


@pytest.mark.parametrize('latest', ['2.0.0', '1.5.4964-beta', None])
def test_setup_does_not_claim_unsupported_latest_is_current(tree, latest, caplog):
    assert finish_catalogue_setup(str(tree.root), str(tree.root / 'no-snapshot'),
        tree.state, fetch=_fetcher(latest={'version': latest})) == 'unsupported'
    assert wr._installed_version(tree.target) == '1.5.4490'
    assert 'freshness unconfirmed: unsupported' in caplog.text


def test_setup_does_not_overwrite_newer_check_after_api_identity_change(tree):
    wr.check_and_stage(tree.nm, tree.state, fetch=_fetcher(), clock=lambda: 100)
    state = Path(tree.state, wr._STATE_FILE)
    previous = json.loads(state.read_text())
    previous['identity'] = 'another-installation'
    state.write_text(json.dumps(previous))
    assert wr.apply_staged(tree.nm, tree.state) == 'updated'
    assert json.loads(state.read_text())['identity'] == 'another-installation'


def test_dialog_worker_runs_the_shared_catalogue_action_without_a_window(tree, monkeypatch):
    from types import SimpleNamespace
    pytest.importorskip('wx')
    from ui.dialogs import api_setup
    from core import wa_version_setup
    original = wa_version_setup.catalogue_setup
    monkeypatch.setattr(wa_version_setup, 'catalogue_setup',
        lambda api, old, **kw: original(api, old, fetch=offline, **kw))
    monkeypatch.setattr(api_setup, 'resource_path', lambda *args: str(tree.root))
    called = []
    def worker(stub, refresh):
        called.append(refresh())
    monkeypatch.setattr(api_setup.ApiSetupDialog, '_run_setup_locked', worker)
    stub = SimpleNamespace(_cancelled=False)
    api_setup.ApiSetupDialog._run_setup(stub)
    assert called == ['offline']


def test_setup_cli_uses_the_same_catalogue_action(tree, monkeypatch):
    import importlib.util
    source = Path(__file__).resolve().parents[1] / 'setup_api.py'
    spec = importlib.util.spec_from_file_location('catalogue_setup_cli', source)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    from core import wa_version_setup
    original = wa_version_setup.catalogue_setup
    monkeypatch.setattr(wa_version_setup, 'catalogue_setup',
        lambda api, old, **kw: original(api, old, fetch=offline, **kw))
    monkeypatch.setattr(cli, 'CLIENT_API_DIR', str(tree.root))
    monkeypatch.setattr(cli, '_main_with_catalogue', lambda refresh: refresh())
    assert cli.main() == 'offline'


def test_cancellation_never_starts_catalogue_network(tree):
    with catalogue_setup(str(tree.root), str(tree.root), cancelled=lambda: True,
                         fetch=lambda *a: pytest.fail('cancelled GET')) as finish:
        assert finish() == 'cancelled'


def test_cancel_just_after_npm_still_restores_newer_compatible_copy(tree):
    _write_package(tree.target, '1.5.5000')
    with catalogue_setup(str(tree.root), str(tree.root), fetch=offline):
        _write_package(tree.target, '1.5.4490')
        # The worker returns early without calling its post-npm action.
    assert wr._installed_version(tree.target) == '1.5.5000'


def test_snapshot_dependencies_resolve_at_future_nested_target(tree):
    nested = Path(tree.nm, '@wppconnect-team/wppconnect/node_modules/@wppconnect/wa-version')
    nested.parent.mkdir(parents=True)
    shutil.move(tree.target, nested)
    ancestor_dep = nested.parents[1] / 'semver'
    shutil.move(nested / 'node_modules/semver', ancestor_dep)
    _write_package(str(nested), '1.5.5000')
    assert not Path(tree.nm, 'semver').exists()
    with catalogue_setup(str(tree.root), str(tree.root), fetch=offline) as finish:
        _write_package(str(nested), '1.5.4490')
        assert finish() == 'offline'
    assert wr._installed_version(str(nested)) == '1.5.5000'
    assert (ancestor_dep / 'package.json').exists()


def test_snapshot_keeps_its_new_dependencies_instead_of_old_npm_copy(tree):
    _write_package(tree.target, '1.5.5000')
    with catalogue_setup(str(tree.root), str(tree.root), fetch=offline) as finish:
        _write_package(tree.target, '1.5.4490')
        Path(tree.target, 'node_modules/semver/package.json').write_text('{"version":"7.0.0"}')
        assert finish() == 'offline'
    assert wr._installed_version(tree.target) == '1.5.5000'
    assert wr._read_json(str(Path(tree.target, 'node_modules/semver/package.json')))['version'] == '7.8.5'


def test_apply_selects_resolved_target_only_after_tree_gate(tree, monkeypatch):
    import contextlib
    assert wr.check_and_stage(tree.nm, tree.state, fetch=_fetcher()) == 'staged'
    original_lock = wr.tree_lock
    nested = Path(tree.nm, '@wppconnect-team/wppconnect/node_modules/@wppconnect/wa-version')
    @contextlib.contextmanager
    def gate_after_swap(api_dir):
        with original_lock(api_dir) as held:
            assert held
            nested.parent.mkdir(parents=True)
            shutil.copytree(tree.target, nested)
            os.rename(wr._staged_path(tree.target, '1.5.4964'),
                      wr._staged_path(str(nested), '1.5.4964'))
            yield held
    monkeypatch.setattr(wr, 'tree_lock', gate_after_swap)
    assert wr.apply_staged(tree.nm, tree.state) == 'updated'
    assert wr._installed_version(str(nested)) == '1.5.4964'
    assert wr._installed_version(tree.target) == '1.5.4490'


def test_state_replace_failure_leaves_previous_record(tree, monkeypatch):
    path = Path(tree.state, 'state.json')
    life.write_state(str(path), {'old': True})
    def fail(*args):
        raise PermissionError('denied')
    monkeypatch.setattr(life.os, 'replace', fail)
    with pytest.raises(PermissionError):
        life.write_state(str(path), {'new': True})
    assert json.loads(path.read_text()) == {'old': True}
    assert not list(path.parent.glob('.wa-check-*'))


def test_api_swap_and_apply_refuse_another_threads_catalogue_gate(tree):
    results = []
    with life.tree_lock(str(tree.root)) as held:
        assert held
        def other():
            results.append(wr.apply_staged(tree.nm, tree.state))
            try:
                api_staging.swap_in_staged_api(str(tree.root), str(tree.root / 'staged'))
            except api_staging.SwapError:
                results.append('swap-refused')
            try:
                api_staging.restore_previous_api(str(tree.root), str(tree.root) + '_old')
            except api_staging.SwapError:
                results.append('rollback-refused')
        thread = threading.Thread(target=other)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
    assert results == ['locked', 'swap-refused', 'rollback-refused']
