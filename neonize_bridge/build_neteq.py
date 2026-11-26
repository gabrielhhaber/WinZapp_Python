"""Build the locked, optional NetEq sidecar as part of the Neonize artifact."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

RUST = '1.91.1'
TARGET = 'x86_64-pc-windows-msvc'
DLL = 'winzapp_call_neteq.dll'
# Names the sidecar, its notices and stamp; the sidecar files are kept under them.
STAMP = 'NETEQ_SOURCE.sha256'
SIDECAR = (DLL, 'NETEQ_NOTICES.txt', 'NETEQ_DEPENDENCY_NOTICES.txt', STAMP, 'neteq_rust_licenses')
SOURCES = ('Cargo.toml', 'Cargo.lock', 'src/lib.rs', 'THIRD_PARTY_NOTICES.txt')


def source_digest(source):
    """SHA-256 of the NetEq sources a sidecar is built from (line endings ignored)."""
    digest = hashlib.sha256()
    for name in SOURCES:
        digest.update(name.encode() + b'\0' + (Path(source) / name).read_bytes().replace(b'\r\n', b'\n'))
    return digest.hexdigest()


def read_sidecar(folder):
    """{relative path: bytes} of the sidecar a package folder holds; {} without its DLL."""
    folder = Path(folder)
    if not (folder / DLL).is_file():
        return {}
    kept = {}
    for name in SIDECAR:
        path = folder / name
        files = [path] if path.is_file() else sorted(p for p in path.rglob('*') if p.is_file())
        for item in files:
            kept[item.relative_to(folder).as_posix()] = item.read_bytes()
    return kept


def restore_sidecar(package, kept, digest, *, installed=False):
    """Put a kept sidecar back into a package built without Rust, or (with
    ``installed``) into a venv whose new build brought none.

    A sidecar stamped with other sources is stale and stays out; an unstamped
    one (copied from a CI artifact) is kept, its ABI is checked when loaded.
    Returns whether it was restored."""
    if not kept:
        return False
    origin = 'the installed copy' if installed else 'the previous build'
    stamp = kept.get(STAMP)
    if stamp is not None and stamp.decode('ascii', 'replace').strip() != digest:
        print(f'WARNING: the NetEq sidecar kept from {origin} was built from other '
              'sources; leaving it out', flush=True)
        return False
    for name, content in kept.items():
        path = Path(package) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    what = ('the new build has no NetEq sidecar; keeping the installed one' if installed
            else 'Rust not found; reusing the NetEq sidecar of the previous build')
    print('WARNING: ' + what
          + ('' if stamp is not None else ' (it carries no source stamp)'), flush=True)
    return True


def dependency_notices(metadata):
    """Bundle the exact resolved Cargo dependencies' license files."""
    used = {node['id'] for node in metadata['resolve']['nodes']}
    result = []
    for package in sorted(metadata['packages'], key=lambda p: (p['name'], p['version'])):
        if package['id'] not in used or package.get('source') is None:
            continue
        folder = Path(package['manifest_path']).parent
        result.append(f"{package['name']} {package['version']} ({package.get('license', '')})")
        for path in sorted(folder.iterdir()):
            if path.is_file() and path.name.upper().startswith(('LICENSE', 'COPYING', 'COPYRIGHT')):
                result.extend((path.name, path.read_text(encoding='utf-8', errors='replace')))
    return '\n\n'.join(result)


def _has_toolchain(cargo, env):
    probe = subprocess.run([cargo, f'+{RUST}-{TARGET}', '--version'], env=env,
                           capture_output=True, text=True)
    return probe.returncode == 0


def build(package, *, system="windows"):
    """Build the sidecar into `package`; False when it was skipped."""
    if system != "windows":
        return False
    root = Path(__file__).resolve().parents[1]
    work = root / 'neonize_bridge/neonize_build'
    source = root / 'native/call_neteq'
    env = dict(os.environ, CARGO_BUILD_JOBS='1', CARGO_TARGET_DIR=str(work / 'neteq-target'),
               RUSTFLAGS='-C target-feature=+crt-static')
    cargo = shutil.which('cargo')
    portable = work / 'tools/cargo/bin/cargo.exe'
    if cargo is None and portable.is_file():
        cargo = str(portable)
        env.update(CARGO_HOME=str(work / 'tools/cargo'), RUSTUP_HOME=str(work / 'tools/rustup'))
    if cargo is None or not _has_toolchain(cargo, env):
        # The sidecar is optional: without it calls keep the compatible playback.
        print(f'WARNING: Rust {RUST}-{TARGET} not found; building without the NetEq sidecar '
              '(install rustup and that toolchain to include it)', flush=True)
        return False
    subprocess.run([cargo, f'+{RUST}-{TARGET}', 'build', '--release', '--locked',
                    '--manifest-path', str(source / 'Cargo.toml'), '--target', TARGET],
                   env=env, check=True)
    shutil.copy2(work / 'neteq-target' / TARGET / 'release' / DLL, Path(package) / DLL)
    shutil.copy2(source / 'THIRD_PARTY_NOTICES.txt', Path(package) / 'NETEQ_NOTICES.txt')
    (Path(package) / STAMP).write_text(source_digest(source) + '\n', encoding='ascii')
    metadata = subprocess.run([cargo, f'+{RUST}-{TARGET}', 'metadata', '--locked',
                               '--format-version', '1', '--filter-platform', TARGET,
                               '--manifest-path', str(source / 'Cargo.toml')],
                              env=env, check=True, capture_output=True, text=True, encoding='utf-8')
    (Path(package) / 'NETEQ_DEPENDENCY_NOTICES.txt').write_text(
        dependency_notices(json.loads(metadata.stdout)), encoding='utf-8')
    rustup = Path(env.get('RUSTUP_HOME', str(Path.home() / '.rustup')))
    rust_licenses = rustup / 'toolchains' / f'{RUST}-{TARGET}' / 'share/doc/rust'
    if rust_licenses.is_dir():
        target = Path(package) / 'neteq_rust_licenses'
        target.mkdir(exist_ok=True)
        for name in ('COPYRIGHT-library.html', 'COPYRIGHT.html'):
            if (rust_licenses / name).is_file():
                shutil.copy2(rust_licenses / name, target / name)
        if (rust_licenses / 'licenses').is_dir():
            shutil.copytree(rust_licenses / 'licenses', target / 'licenses', dirs_exist_ok=True)
    return True
