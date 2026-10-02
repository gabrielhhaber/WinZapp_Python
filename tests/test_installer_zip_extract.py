"""The installer's ZIP extractor (installer/zipextract.c) and its build wiring.

The installer used to read only ZIP_STORED entries, so the payload was stored
and WinZappInstaller.exe came out ~330 MB next to a 125 MB WinZapp.zip. The
extractor now inflates DEFLATE entries (zlib, linked statically), verifies
each entry's CRC-32 and refuses names that would leave the install folder.

Real tests: gcc builds tests/c/zip_extract_cli.c, a console program around the
same zipextract.c, and it extracts archives written by Python's zipfile into
tmp_path. No window is ever created; the installer itself is only compiled
syntax-only by test_uninstaller_language.py and never run. Skipped when gcc or
libz.a is missing.
"""

import hashlib
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER_DIR = ROOT / "installer"
CLI_SOURCE = ROOT / "tests" / "c" / "zip_extract_cli.c"


def _libz_path():
    gcc = shutil.which("gcc")
    if gcc is None:
        return None
    out = subprocess.run([gcc, "-print-file-name=libz.a"], capture_output=True,
                         text=True, timeout=60).stdout.strip()
    return out if out and os.path.isfile(out) else None


@pytest.fixture(scope="module")
def cli(tmp_path_factory):
    if shutil.which("gcc") is None:
        pytest.skip("gcc not installed")
    if _libz_path() is None:
        pytest.skip("libz.a (static zlib) not found by gcc")
    exe = tmp_path_factory.mktemp("zipcli") / "zip_extract_cli.exe"
    result = subprocess.run(
        ["gcc", "-Wall", "-municode", "-mconsole", "-I", str(INSTALLER_DIR),
         str(CLI_SOURCE), str(INSTALLER_DIR / "zipextract.c"),
         "-o", str(exe), "-l:libz.a", "-lpsapi"],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stderr
    return exe


def _run(cli, archive, dest, *extra):
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([str(cli), str(archive), str(dest), *extra],
                          capture_output=True, timeout=300)
    out = proc.stdout.decode("utf-8", "replace")
    info = {"rc": proc.returncode, "out": out, "error": "", "files": 0}
    for line in out.splitlines():
        if line.startswith("ERROR "):
            info["error"] = line[6:]
        elif line.startswith("FILE "):
            info["files"] += 1
        elif m := re.match(r"OPEN entries=(\d+) total=(\d+)", line):
            info["entries"], info["total"] = int(m[1]), int(m[2])
        elif m := re.match(r"(?:DONE|CANCELLED) bytes=(\d+)(?: peak_kb=(\d+))?", line):
            info["bytes"] = int(m[1])
            info["peak_kb"] = int(m[2] or 0)
    return info


def _write_zip(path, entries, mode="deflated"):
    """entries: name -> bytes (None for a directory). mode: stored, deflated
    or mixed (alternating)."""
    with zipfile.ZipFile(path, "w") as zf:
        for i, (name, data) in enumerate(entries.items()):
            if data is None:
                zf.writestr(name if name.endswith("/") else name + "/", b"")
                continue
            deflate = mode == "deflated" or (mode == "mixed" and i % 2 == 0)
            zf.writestr(name, data, compress_type=(
                zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED))


def _tree(root):
    files, dirs = {}, set()
    for dp, dn, fn in os.walk(root):
        for d in dn:
            dirs.add(os.path.relpath(os.path.join(dp, d), root).replace("\\", "/"))
        for f in fn:
            full = os.path.join(dp, f)
            files[os.path.relpath(full, root).replace("\\", "/")] = (
                os.path.getsize(full), _sha(full))
    return files, dirs


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sample_entries():
    text = (b"WinZapp installer payload line\n" * 2000)
    return {
        "WinZapp.exe": os.urandom(70_000),
        "readme.txt": text,
        "empty.bin": b"",
        "dir/": None,
        "emptydir/": None,
        "deep/er/est/file.dat": os.urandom(5000) + text,
        "_internal/lib.dll": bytes(range(256)) * 3000,
        "\u00fcn\u00ef/\u65e5\u672c\u8a9e \u6587\u4ef6.txt": "caf\u00e9".encode() * 500,
        "with space/a b.txt": b"x",
    }


def _expected(entries):
    files = {n: (len(d), hashlib.sha256(d).hexdigest())
             for n, d in entries.items() if d is not None}
    dirs = set()
    for n, d in entries.items():
        parts = n.rstrip("/").split("/")
        for i in range(1, len(parts) if d is not None else len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    return files, dirs


@pytest.mark.parametrize("mode", ["stored", "deflated", "mixed"])
@pytest.mark.parametrize("prefixed", [False, True], ids=["plain", "appended-to-stub"])
def test_extracts_every_kind_of_entry(cli, tmp_path, mode, prefixed):
    entries = _sample_entries()
    plain = tmp_path / "payload.zip"
    _write_zip(plain, entries, mode)
    archive = plain
    if prefixed:
        # build.py appends the finished ZIP to the stub, so the ZIP's own
        # offsets stay relative to its first byte, behind an arbitrary prefix.
        archive = tmp_path / "installer.exe"
        archive.write_bytes(b"MZ" + os.urandom(300_000) + plain.read_bytes())

    dest = tmp_path / "out dir"
    info = _run(cli, archive, dest)

    assert info["rc"] == 0, info["out"]
    exp_files, exp_dirs = _expected(entries)
    got_files, got_dirs = _tree(dest)
    assert got_files == exp_files
    assert exp_dirs <= got_dirs
    # Progress is counted in uncompressed bytes of the files.
    assert info["entries"] == len(entries)
    assert info["total"] == sum(len(d) for d in entries.values() if d is not None)
    assert info["bytes"] == info["total"]
    assert info["files"] == len(exp_files)


def test_deflate_payload_is_smaller_than_stored(tmp_path):
    entries = _sample_entries()
    stored, deflated = tmp_path / "s.zip", tmp_path / "d.zip"
    _write_zip(stored, entries, "stored")
    _write_zip(deflated, entries, "deflated")
    assert deflated.stat().st_size < stored.stat().st_size


def test_trailing_bytes_after_the_payload_are_tolerated(cli, tmp_path):
    # An Authenticode signature appended after the payload must not hide it.
    entries = {"a.txt": b"hello" * 100}
    plain = tmp_path / "p.zip"
    _write_zip(plain, entries)
    archive = tmp_path / "signed.exe"
    archive.write_bytes(os.urandom(1000) + plain.read_bytes() + os.urandom(9000))
    info = _run(cli, archive, tmp_path / "out")
    assert info["rc"] == 0, info["out"]
    assert _tree(tmp_path / "out")[0] == _expected(entries)[0]


def test_data_descriptor_entries(cli, tmp_path):
    # Writing to a stream that cannot seek makes zipfile set general-purpose
    # flag bit 3: the local header carries zero sizes and CRC, and only the
    # central directory knows the real ones.
    class Unseekable:
        def __init__(self, fh):
            self.fh = fh

        def write(self, b):
            return self.fh.write(b)

        def flush(self):
            self.fh.flush()

    entries = {"x/one.bin": os.urandom(40_000) + b"a" * 90_000, "two.txt": b"t" * 10}
    archive = tmp_path / "dd.zip"
    with open(archive, "wb") as fh, zipfile.ZipFile(Unseekable(fh), "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    with zipfile.ZipFile(archive) as zf:
        assert all(i.flag_bits & 0x08 for i in zf.infolist())

    info = _run(cli, archive, tmp_path / "out")
    assert info["rc"] == 0, info["out"]
    assert _tree(tmp_path / "out")[0] == _expected(entries)[0]


def test_zip64_records(cli, tmp_path, monkeypatch):
    # Shrinking zipfile's limits makes it write the same ZIP64 structures a
    # multi-gigabyte payload gets: extra fields in the central directory and a
    # ZIP64 end-of-central-directory record plus locator.
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 2000)
    monkeypatch.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 3)
    entries = {f"f{i}.bin": os.urandom(3000 + i) + b"z" * 9000 for i in range(8)}
    plain = tmp_path / "z64.zip"
    _write_zip(plain, entries, "mixed")
    raw = plain.read_bytes()
    assert b"PK\x06\x06" in raw and b"PK\x06\x07" in raw
    archive = tmp_path / "installer.exe"
    archive.write_bytes(os.urandom(70_000) + raw)

    info = _run(cli, archive, tmp_path / "out")
    assert info["rc"] == 0, info["out"]
    assert info["entries"] == 8
    assert _tree(tmp_path / "out")[0] == _expected(entries)[0]


def test_force_zip64_entry(cli, tmp_path):
    data = os.urandom(20_000) + b"q" * 50_000
    archive = tmp_path / "f64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        with zf.open("big.bin", "w", force_zip64=True) as w:
            w.write(data)
    info = _run(cli, archive, tmp_path / "out")
    assert info["rc"] == 0, info["out"]
    assert (tmp_path / "out" / "big.bin").read_bytes() == data


def test_large_entry_is_streamed(cli, tmp_path):
    # >100 MB of semi-compressible data: fresh random block every fourth
    # chunk, repeats in between. Only the two fixed-size chunk buffers are ever
    # held, so the harness finishes with a small working set.
    archive = tmp_path / "big.zip"
    digest = hashlib.sha256()
    block = os.urandom(65536)
    size = 0
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        with zf.open("big.pak", "w", force_zip64=True) as w:
            for i in range(1700):
                if i % 4 == 0:
                    block = os.urandom(65536)
                w.write(block)
                digest.update(block)
                size += len(block)
    assert size > 100 * 1024 * 1024

    dest = tmp_path / "out"
    info = _run(cli, archive, dest)
    assert info["rc"] == 0, info["out"]
    assert info["bytes"] == size == info["total"]
    out = dest / "big.pak"
    assert out.stat().st_size == size
    assert _sha(out) == digest.hexdigest()
    # Generous on purpose (a whole-entry buffer would be >100 MB): the point
    # is that memory does not scale with the entry.
    assert info["peak_kb"] < 64 * 1024


@pytest.mark.parametrize("mode", ["stored", "deflated"])
def test_corrupt_payload_fails_and_leaves_no_partial_file(cli, tmp_path, mode):
    data = os.urandom(30_000) + b"k" * 60_000
    plain = tmp_path / "c.zip"
    _write_zip(plain, {"ok_first.txt": b"fine", "victim.bin": data, "after.txt": b"x"},
               mode)
    raw = bytearray(plain.read_bytes())
    pos = raw.find(b"victim.bin")
    pos += len(b"victim.bin") + 5_000        # well inside the entry's data
    raw[pos] ^= 0xFF
    plain.write_bytes(bytes(raw))

    dest = tmp_path / "out"
    info = _run(cli, plain, dest)
    assert info["rc"] == 2
    assert re.search(r"CRC|inflate", info["error"]), info["out"]
    assert not (dest / "victim.bin").exists()
    assert (dest / "ok_first.txt").read_bytes() == b"fine"
    assert not (dest / "after.txt").exists()   # extraction stopped at the fault


def test_crc_mismatch_in_central_directory(cli, tmp_path):
    plain = tmp_path / "crc.zip"
    _write_zip(plain, {"a.bin": b"abc" * 1000}, "stored")
    raw = bytearray(plain.read_bytes())
    cd = raw.rfind(b"PK\x01\x02")
    raw[cd + 16] ^= 0x01                      # the stored CRC-32
    plain.write_bytes(bytes(raw))
    info = _run(cli, plain, tmp_path / "out")
    assert info["rc"] == 2 and "CRC-32 mismatch" in info["error"]
    assert not (tmp_path / "out" / "a.bin").exists()


def test_truncated_archive_is_rejected(cli, tmp_path):
    plain = tmp_path / "t.zip"
    _write_zip(plain, {"a.bin": os.urandom(50_000)}, "deflated")
    raw = plain.read_bytes()
    plain.write_bytes(raw[:-40])
    info = _run(cli, plain, tmp_path / "out")
    assert info["rc"] == 1
    assert "truncated" in info["error"] or "central directory" in info["error"]
    assert not any((tmp_path / "out").iterdir())


def test_truncated_deflate_stream_is_rejected(cli, tmp_path):
    plain = tmp_path / "ts.zip"
    _write_zip(plain, {"a.bin": os.urandom(30_000) + b"p" * 90_000}, "deflated")
    raw = bytearray(plain.read_bytes())
    cd = raw.rfind(b"PK\x01\x02")
    comp = int.from_bytes(raw[cd + 20:cd + 24], "little")
    raw[cd + 20:cd + 24] = (comp // 2).to_bytes(4, "little")   # stream cut short
    plain.write_bytes(bytes(raw))
    info = _run(cli, plain, tmp_path / "out")
    assert info["rc"] == 2
    assert "truncated" in info["error"] or "inflate" in info["error"]
    assert not (tmp_path / "out" / "a.bin").exists()


@pytest.mark.parametrize("method,name", [
    (zipfile.ZIP_BZIP2, "bzip2"), (zipfile.ZIP_LZMA, "lzma"),
])
def test_unsupported_method_is_a_clear_error(cli, tmp_path, method, name):
    archive = tmp_path / "m.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.txt", b"hello" * 100, compress_type=method)
    info = _run(cli, archive, tmp_path / "out")
    assert info["rc"] == 2
    assert "unsupported compression method" in info["error"]
    assert not (tmp_path / "out" / "a.txt").exists()


UNSAFE_NAMES = [
    "../evil.txt", "a/../../evil.txt", "a/b/../../../evil.txt", "..",
    "/abs_evil.txt", "\\\\server\\share\\evil.txt", "C:/zipx_evil.txt",
    "C:zipx_evil.txt", "..\\evil.txt", "ok/..\\..\\evil.txt",
    "file.txt:stream", "file.txt::$DATA", "a//b.txt", "bad|name.txt",
    "star*.txt",
]


@pytest.mark.parametrize("name", UNSAFE_NAMES)
def test_unsafe_entry_names_are_rejected(cli, tmp_path, name):
    outer = tmp_path / "outer"
    dest = outer / "dest"
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        info_ = zipfile.ZipInfo("placeholder")
        info_.filename = name           # bypass zipfile's own normalisation
        zf.writestr(info_, b"pwned")
    info = _run(cli, archive, dest)
    assert info["rc"] == 2
    assert "unsafe entry name" in info["error"]
    # Nothing written anywhere but (possibly empty) dest.
    assert [p.name for p in outer.iterdir()] == ["dest"]
    assert not any(dest.iterdir())
    assert not list(tmp_path.glob("evil.txt"))
    assert not os.path.exists("C:\\zipx_evil.txt")
    assert not os.path.exists("C:zipx_evil.txt")


def test_cancel_flag_stops_extraction_early(cli, tmp_path):
    entries = {f"f{i}.bin": os.urandom(2_000_000) for i in range(6)}
    archive = tmp_path / "cancel.zip"
    _write_zip(archive, entries, "stored")
    dest = tmp_path / "out"
    info = _run(cli, archive, dest, "--cancel-after", "5000000")
    assert info["rc"] == 3 and "CANCELLED" in info["out"]
    got, _ = _tree(dest)
    assert 0 < len(got) < len(entries)
    # A file cut off mid-way is deleted, so every file left is complete.
    exp, _ = _expected(entries)
    assert all(exp[n] == v for n, v in got.items())


def test_missing_archive_reports_an_error(cli, tmp_path):
    info = _run(cli, tmp_path / "nope.exe", tmp_path / "out")
    assert info["rc"] == 1 and "cannot open" in info["error"]


# ── Source contracts: the wiring a console harness cannot see ────────────

def _read(path):
    return Path(path).read_text(encoding="utf-8")


def test_installer_no_longer_parses_zip_structures():
    src = _read(INSTALLER_DIR / "installer.c")
    assert '#include "zipextract.h"' in src
    for needle in ("ZipLocal", "ZipCentral", "ZipEOCD", "find_zip_info",
                   "ZIP_CD_SIG", "ZIP_LOCAL_SIG", "ZIP_STORED", "read_at("):
        assert needle not in src, needle
    assert "zipx_open(" in src and "zipx_extract_all(" in src


def test_extractor_supports_deflate_with_zlib_and_checks_crc():
    src = _read(INSTALLER_DIR / "zipextract.c")
    assert "<zlib.h>" in src
    assert "inflateInit2(&zs, -15)" in src
    assert "crc32(" in src
    assert "unsupported compression method" in src
    # Sizes come from the central directory, never the local header.
    assert "rd32(lh + 18)" not in src and "rd32(lh + 22)" not in src


def test_build_compresses_the_payload_and_links_zlib_statically():
    src = _read(ROOT / "build.py")
    body = src[src.index("def create_payload_zip"):src.index("def compile_installer_stub")]
    assert "ZIP_DEFLATED" in body and "ZIP_STORED" not in body
    assert "ZIP_STORED" not in src
    stub = src[src.index("def compile_installer_stub"):src.index("def append_zip_to_stub")]
    assert 'os.path.join(INSTALLER_DIR, "zipextract.c")' in stub
    assert '"-l:libz.a"' in stub
    # Fails early, with the package name, instead of an obscure ld error.
    assert "check_static_zlib()" in src
    assert "mingw-w64-ucrt-x86_64-zlib" in src


def test_ci_installs_zlib_for_the_installer_stub():
    wf = _read(ROOT / ".github" / "workflows" / "build-windows.yml")
    assert "mingw-w64-ucrt-x86_64-zlib" in wf


def test_find_static_zlib_returns_a_real_file_or_none(monkeypatch):
    # build.py cannot be imported (it runs build-time code), so load just the
    # function the way the other build tests do.
    src = _read(ROOT / "build.py")
    start = src.index("def find_static_zlib")
    end = src.index("def check_static_zlib")
    ns = {"os": os, "subprocess": subprocess, "GCC_CMD": "gcc"}
    exec(src[start:end], ns)
    find = ns["find_static_zlib"]
    assert find("this-compiler-does-not-exist") is None
    found = find()
    assert found is None or os.path.isfile(found)
    if shutil.which("gcc"):
        assert found == _libz_path()
