"""The neteq-* release's files: the DLL with its notices, and the pin the
client's download checks. No Rust build runs here."""
import hashlib

import pytest

import neteq_release


def _sidecar(folder):
    folder.mkdir()
    (folder / 'winzapp_call_neteq.dll').write_bytes(b'MZ dll bytes')
    (folder / 'NETEQ_NOTICES.txt').write_text('NetEq BSD notice', encoding='utf-8')
    (folder / 'NETEQ_DEPENDENCY_NOTICES.txt').write_text('dependency 1.0 (MIT)', encoding='utf-8')
    (folder / 'neteq_rust_licenses' / 'licenses').mkdir(parents=True)
    (folder / 'neteq_rust_licenses' / 'licenses' / 'MIT').write_text('Rust MIT', encoding='utf-8')
    return folder


def test_the_notices_travel_in_one_file_with_every_licence(tmp_path):
    text = neteq_release.combined_notices(_sidecar(tmp_path / 'sidecar'))
    assert 'NetEq BSD notice' in text and 'dependency 1.0 (MIT)' in text
    assert 'Rust: licenses/MIT' in text and 'Rust MIT' in text


def test_a_sidecar_without_notices_is_not_released(tmp_path):
    (tmp_path / 'winzapp_call_neteq.dll').write_bytes(b'MZ')
    with pytest.raises(ValueError):
        neteq_release.combined_notices(tmp_path)


def test_the_release_files_and_the_pin_agree(tmp_path):
    assets = neteq_release.write_release(_sidecar(tmp_path / 'sidecar'), tmp_path / 'dist')
    dll = (tmp_path / 'dist' / 'winzapp_call_neteq.dll').read_bytes()
    assert assets['winzapp_call_neteq.dll'] == (len(dll), hashlib.sha256(dll).hexdigest())
    sums = (tmp_path / 'dist' / 'SHA256SUMS.txt').read_text(encoding='ascii')
    for name, (_size, sha) in assets.items():
        assert f'{sha}  {name}' in sums
    pin = neteq_release.pin_text('neteq-1.0.1-winzapp.1', assets)
    assert 'RELEASE_TAG = "neteq-1.0.1-winzapp.1"' in pin
    assert f'DLL_BYTES = {len(dll)}' in pin
    assert f'NOTICES_SHA256 = "{assets["winzapp_call_neteq_NOTICES.txt"][1]}"' in pin


@pytest.mark.parametrize('tag', ['v1.0.0', 'neonize-0.6.0', 'neteq-1.0-alpha', ''])
def test_a_tag_the_updater_could_take_is_refused(tag):
    with pytest.raises(ValueError):
        neteq_release.check_tag(tag)
