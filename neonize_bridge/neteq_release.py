"""The files of a ``neteq-*`` release: the NetEq DLL and its notices, as
client/core/call_neteq_component.py downloads them.

Run by .github/workflows/neteq-dll.yml (``python neonize_bridge/neteq_release.py
<out> <tag>``): builds the sidecar with build_neteq's pinned toolchain, then
writes into <out> the two assets, SHA256SUMS.txt and NETEQ_PIN.txt, the five
values a maintainer copies into call_neteq_component.py once the draft
release is published. Kept out of build_neteq.py, whose bytes are part of the
Neonize build identity: a release helper must not invalidate installed builds.
"""
import hashlib
from pathlib import Path
import shutil
import sys

import build_neteq

DLL_ASSET = build_neteq.DLL
NOTICES_ASSET = 'winzapp_call_neteq_NOTICES.txt'


def combined_notices(package):
    """NetEq's notices, its Cargo dependencies' and Rust's own licences, in
    one text: the download is two files, and the DLL never travels without it."""
    package = Path(package)
    parts = []
    for name in ('NETEQ_NOTICES.txt', 'NETEQ_DEPENDENCY_NOTICES.txt'):
        path = package / name
        if path.is_file():
            parts.append(f'===== {name} =====\n\n' + path.read_text(encoding='utf-8'))
    licenses = package / 'neteq_rust_licenses'
    if licenses.is_dir():
        for path in sorted(p for p in licenses.rglob('*') if p.is_file()):
            parts.append(f'===== Rust: {path.relative_to(licenses).as_posix()} =====\n\n'
                         + path.read_text(encoding='utf-8', errors='replace'))
    if not parts:
        raise ValueError('the NetEq sidecar carries no notices')
    return '\n\n'.join(parts) + '\n'


def write_release(package, out):
    """Write the assets and SHA256SUMS.txt into `out`; {asset: (bytes, sha256)}."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(package) / DLL_ASSET, out / DLL_ASSET)
    (out / NOTICES_ASSET).write_bytes(combined_notices(package).encode('utf-8'))
    assets = {}
    for name in (DLL_ASSET, NOTICES_ASSET):
        data = (out / name).read_bytes()
        assets[name] = (len(data), hashlib.sha256(data).hexdigest())
    (out / 'SHA256SUMS.txt').write_text(
        ''.join(f'{sha}  {name}\n' for name, (_size, sha) in assets.items()), encoding='ascii')
    return assets


def check_tag(tag):
    """A tag the updater can never take for a WinZapp release."""
    if not tag.startswith('neteq-') or 'alpha' in tag.lower():
        raise ValueError(f'the tag must start with neteq- and not contain alpha (got {tag!r})')
    return tag


def pin_text(tag, assets):
    """The lines that replace the pin block of call_neteq_component.py."""
    dll_bytes, dll_sha = assets[DLL_ASSET]
    notices_bytes, notices_sha = assets[NOTICES_ASSET]
    return (f'RELEASE_TAG = "{check_tag(tag)}"\n'
            f'DLL_BYTES = {dll_bytes}\n'
            f'DLL_SHA256 = "{dll_sha}"\n'
            f'NOTICES_BYTES = {notices_bytes}\n'
            f'NOTICES_SHA256 = "{notices_sha}"\n')


def main(argv):
    out, tag = Path(argv[1]), check_tag(argv[2])
    package = out / 'sidecar'
    package.mkdir(parents=True, exist_ok=True)
    if not build_neteq.build(package):
        raise SystemExit(f'Rust {build_neteq.RUST}-{build_neteq.TARGET} is needed to build the release')
    pin = pin_text(tag, write_release(package, out))
    shutil.rmtree(package)
    (out / 'NETEQ_PIN.txt').write_text(pin, encoding='ascii')
    print(pin, end='', flush=True)


if __name__ == '__main__':
    main(sys.argv)
