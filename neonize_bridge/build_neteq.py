"""Build the locked, optional NetEq sidecar as part of the Neonize artifact."""
import json
import os
from pathlib import Path
import shutil
import subprocess

RUST = '1.91.1'
TARGET = 'x86_64-pc-windows-msvc'
DLL = 'winzapp_call_neteq.dll'


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


def build(package):
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
    if cargo is None:
        raise RuntimeError('NetEq build requires Rust: install rustup and toolchain 1.91.1-x86_64-pc-windows-msvc')
    subprocess.run([cargo, f'+{RUST}-{TARGET}', 'build', '--release', '--locked',
                    '--manifest-path', str(source / 'Cargo.toml'), '--target', TARGET],
                   env=env, check=True)
    shutil.copy2(work / 'neteq-target' / TARGET / 'release' / DLL, Path(package) / DLL)
    shutil.copy2(source / 'THIRD_PARTY_NOTICES.txt', Path(package) / 'NETEQ_NOTICES.txt')
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
