"""Dependency notices describe the resolved packages, without machine paths."""
from build_neteq import dependency_notices
import pytest


@pytest.mark.parametrize('system', ['darwin', 'linux'])
def test_other_platforms_keep_compatible_audio_without_windows_rust_build(monkeypatch, tmp_path, system):
    import build_neteq
    monkeypatch.setattr(build_neteq.shutil, 'which', lambda *a: pytest.fail('Windows toolchain searched'))
    monkeypatch.setattr(build_neteq.subprocess, 'run', lambda *a, **k: pytest.fail('Windows DLL build attempted'))
    build_neteq.build(tmp_path / 'package', system=system)
    assert not (tmp_path / 'package').exists()


def test_resolved_dependency_notices_keep_versions_and_license_text(tmp_path):
    (tmp_path/'Cargo.toml').write_text('')
    (tmp_path/'LICENSE-MIT').write_text('Original copyright and permission')
    metadata={'resolve':{'nodes':[{'id':'used'}]},'packages':[
        {'id':'used','name':'dependency','version':'1.2.3','source':'registry',
         'license':'MIT','manifest_path':str(tmp_path/'Cargo.toml')},
        {'id':'unused','name':'unused','version':'9','source':'registry','manifest_path':'missing'},
    ]}
    result=dependency_notices(metadata)
    assert 'dependency 1.2.3 (MIT)' in result
    assert 'Original copyright and permission' in result
    assert 'unused' not in result and str(tmp_path) not in result


def test_a_build_without_rust_is_skipped_so_the_sidecar_stays_optional(monkeypatch, tmp_path, capsys):
    import build_neteq
    monkeypatch.setattr(build_neteq.shutil, 'which', lambda *a: None)
    monkeypatch.setattr(build_neteq, '_has_toolchain', lambda *a: pytest.fail('probed without cargo'))
    monkeypatch.setattr(build_neteq.subprocess, 'run', lambda *a, **k: pytest.fail('cargo build attempted'))
    monkeypatch.setattr(build_neteq.Path, 'is_file', lambda self: False)
    assert build_neteq.build(tmp_path / 'package', system='windows') is False
    assert 'without the NetEq sidecar' in capsys.readouterr().out


def test_a_cargo_without_the_pinned_toolchain_is_skipped_too(monkeypatch, tmp_path):
    import build_neteq
    monkeypatch.setattr(build_neteq.shutil, 'which', lambda *a: 'cargo')
    monkeypatch.setattr(build_neteq, '_has_toolchain', lambda *a: False)
    monkeypatch.setattr(build_neteq.subprocess, 'run', lambda *a, **k: pytest.fail('cargo build attempted'))
    assert build_neteq.build(tmp_path / 'package', system='windows') is False
