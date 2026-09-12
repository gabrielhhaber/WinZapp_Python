"""Release signatures: what the updater trusts, and the tool that signs.

core/release_signature.py decides whether a release's SHA256SUMS.txt may be
trusted; .github/scripts/release_signing.py produces the signatures. Both halves
are tested against each other with real Ed25519 keys generated per test, so a
change to either side that makes the other refuse its output fails here rather
than on hundreds of machines that stop updating.

The cases that matter most are the refusals — a correctly signed OLD release
republished under a newer tag, an alpha key vouching for a stable release, a
release with the signature simply left off — because each of those is what a
forged release would look like once a GitHub account is compromised.
"""

import base64
import hashlib
import importlib.util
import pathlib
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import updater
from core import release_keys
from core import release_signature as rs

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_signing_script():
    spec = importlib.util.spec_from_file_location(
        "release_signing", ROOT / ".github" / "scripts" / "release_signing.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


signing = _load_signing_script()


def _keypair():
    private = Ed25519PrivateKey.generate()
    return private, signing.public_key_b64(private)


def _manifest(version, body="abc  WinZapp.zip\r\ndef  WinZappInstaller.exe\r\n"):
    return f"{rs.MANIFEST_VERSION_PREFIX} {version}\r\n{body}".encode("ascii")


@pytest.fixture
def keys():
    stable, stable_pub = _keypair()
    alpha, alpha_pub = _keypair()
    return SimpleNamespace(stable=stable, stable_pub=stable_pub, alpha=alpha, alpha_pub=alpha_pub)


def _check(keys, manifest, signature, version, is_alpha):
    return rs.check_release_manifest(
        manifest, signature, version, is_alpha, [keys.stable_pub], [keys.alpha_pub],
    )


# ── The committed key configuration ──────────────────────────────────────────

def test_committed_keys_are_both_set_or_both_empty():
    """Half-configured is the one broken state: stable keys with no alpha key
    would make every signed-by-CI alpha fail verification (and vice versa)."""
    assert bool(release_keys.STABLE_PUBLIC_KEYS) == bool(release_keys.ALPHA_PUBLIC_KEYS)


def test_committed_keys_are_valid_ed25519_public_keys():
    for encoded in (*release_keys.STABLE_PUBLIC_KEYS, *release_keys.ALPHA_PUBLIC_KEYS):
        assert len(base64.b64decode(encoded, validate=True)) == 32
        rs.load_public_key(encoded)


def test_stable_signing_keeps_a_backup_key():
    """Losing the only stable key strands every stable user on their build."""
    if release_keys.STABLE_PUBLIC_KEYS:
        assert len(release_keys.STABLE_PUBLIC_KEYS) >= 2


def test_signing_script_reads_the_same_keys_the_updater_imports():
    stable, alpha = signing.read_configured_keys()
    assert stable == tuple(release_keys.STABLE_PUBLIC_KEYS)
    assert alpha == tuple(release_keys.ALPHA_PUBLIC_KEYS)


# ── Manifest version ─────────────────────────────────────────────────────────

def test_manifest_version_is_read_from_crlf_manifest():
    assert rs.manifest_version(_manifest("1.2.0.0Beta")) == "1.2.0.0beta"


def test_manifest_without_version_line():
    assert rs.manifest_version(b"abc  WinZapp.zip\n") == ""


def test_old_hash_parser_still_ignores_the_version_line(tmp_path, monkeypatch):
    """Builds released before signatures read the new manifest too."""
    zip_path = tmp_path / "WinZapp.zip"
    zip_path.write_bytes(b"zip")
    digest = hashlib.sha256(b"zip").hexdigest()
    manifest = _manifest("1.2.0.0", body=f"{digest}  WinZapp.zip\r\n")
    monkeypatch.setattr(
        updater.requests, "get",
        lambda *a, **kw: SimpleNamespace(content=manifest, text=manifest.decode(), raise_for_status=lambda: None),
    )
    ok, detail = updater._verify_sha256sums(
        str(zip_path), "WinZapp.zip", "https://x/SHA256SUMS.txt", stable_keys=(), alpha_keys=(),
    )
    assert ok, detail


# ── check_release_manifest ───────────────────────────────────────────────────

def test_unconfigured_build_accepts_anything():
    assert rs.check_release_manifest(None, None, "1.0.0.0", False, (), ()) == (True, "")


def test_stable_release_signed_by_stable_key_passes(keys):
    manifest = _manifest("1.2.0.0")
    assert _check(keys, manifest, signing.sign_manifest(manifest, keys.stable), "v1.2.0.0", False) == (True, "")


def test_missing_manifest_fails_closed_once_configured(keys):
    ok, detail = _check(keys, None, None, "1.2.0.0", False)
    assert not ok and "SHA256SUMS.txt" in detail


def test_missing_signature_fails_closed_once_configured(keys):
    ok, detail = _check(keys, _manifest("1.2.0.0"), None, "1.2.0.0", False)
    assert not ok and rs.SIGNATURE_ASSET_NAME in detail


def test_old_signed_release_republished_under_newer_tag_is_refused(keys):
    """The replay: a genuine signature, a version that is not the tag's."""
    manifest = _manifest("1.1.0.0")
    signature = signing.sign_manifest(manifest, keys.stable)
    ok, detail = _check(keys, manifest, signature, "1.2.0.0", False)
    assert not ok and "1.1.0.0" in detail


def test_manifest_with_no_version_is_refused(keys):
    manifest = b"abc  WinZapp.zip\n"
    ok, _ = _check(keys, manifest, signing.sign_manifest(manifest, keys.stable), "1.2.0.0", False)
    assert not ok


def test_tampered_manifest_is_refused(keys):
    manifest = _manifest("1.2.0.0")
    signature = signing.sign_manifest(manifest, keys.stable)
    ok, _ = _check(keys, manifest.replace(b"abc", b"abd"), signature, "1.2.0.0", False)
    assert not ok


def test_signature_from_an_unknown_key_is_refused(keys):
    stranger, _ = _keypair()
    manifest = _manifest("1.2.0.0")
    ok, detail = _check(keys, manifest, signing.sign_manifest(manifest, stranger), "1.2.0.0", False)
    assert not ok and "does not match" in detail


def test_garbage_signature_is_refused_not_raised(keys):
    ok, detail = _check(keys, _manifest("1.2.0.0"), "not base64!!\n", "1.2.0.0", False)
    assert not ok and "Invalid" in detail


def test_alpha_key_cannot_vouch_for_a_stable_release(keys):
    """The whole point of keeping the CI key apart."""
    manifest = _manifest("1.2.0.0")
    ok, _ = _check(keys, manifest, signing.sign_manifest(manifest, keys.alpha), "1.2.0.0", False)
    assert not ok


def test_alpha_key_needs_alpha_in_the_signed_version_not_only_the_tag(keys):
    manifest = _manifest("1.2.0.0")
    ok, _ = _check(keys, manifest, signing.sign_manifest(manifest, keys.alpha), "1.2.0.0", True)
    assert not ok


def test_alpha_key_needs_an_alpha_release_not_only_the_version(keys):
    manifest = _manifest("1.2.0.2300alpha")
    ok, _ = _check(keys, manifest, signing.sign_manifest(manifest, keys.alpha), "1.2.0.2300alpha", False)
    assert not ok


def test_alpha_release_signed_by_alpha_key_passes(keys):
    manifest = _manifest("1.2.0.2300alpha")
    signature = signing.sign_manifest(manifest, keys.alpha)
    assert _check(keys, manifest, signature, "v1.2.0.2300alpha", True) == (True, "")


def test_stable_key_may_sign_an_alpha(keys):
    manifest = _manifest("1.2.0.2300alpha")
    signature = signing.sign_manifest(manifest, keys.stable)
    assert _check(keys, manifest, signature, "1.2.0.2300alpha", True) == (True, "")


def test_a_malformed_trusted_key_does_not_hide_a_valid_one(keys):
    manifest = _manifest("1.2.0.0")
    ok, _ = rs.check_release_manifest(
        manifest, signing.sign_manifest(manifest, keys.stable), "1.2.0.0", False,
        ["AAAA", keys.stable_pub], [],
    )
    assert ok


# ── The updater wiring ───────────────────────────────────────────────────────

class _Responses:
    def __init__(self, by_url):
        self.by_url = by_url

    def __call__(self, url, **kwargs):
        data = self.by_url[url]
        return SimpleNamespace(
            content=data, text=data.decode("ascii"), raise_for_status=lambda: None,
        )


def _signed_zip_release(tmp_path, keys, version, signer):
    zip_path = tmp_path / "WinZapp.zip"
    zip_path.write_bytes(b"the release")
    digest = hashlib.sha256(b"the release").hexdigest()
    manifest = _manifest(version, body=f"{digest}  WinZapp.zip\r\n")
    return zip_path, manifest, signing.sign_manifest(manifest, signer).encode("ascii")


def test_updater_installs_a_correctly_signed_release(tmp_path, keys, monkeypatch):
    zip_path, manifest, sig = _signed_zip_release(tmp_path, keys, "1.2.0.0", keys.stable)
    monkeypatch.setattr(updater.requests, "get", _Responses({"https://x/sums": manifest, "https://x/sig": sig}))
    ok, detail = updater._verify_sha256sums(
        str(zip_path), "WinZapp.zip", "https://x/sums", signature_url="https://x/sig",
        expected_version="1.2.0.0", stable_keys=[keys.stable_pub], alpha_keys=[keys.alpha_pub],
    )
    assert ok, detail


def test_updater_refuses_an_unsigned_release_once_keys_exist(tmp_path, keys, monkeypatch):
    zip_path, manifest, _ = _signed_zip_release(tmp_path, keys, "1.2.0.0", keys.stable)
    monkeypatch.setattr(updater.requests, "get", _Responses({"https://x/sums": manifest}))
    ok, _ = updater._verify_sha256sums(
        str(zip_path), "WinZapp.zip", "https://x/sums", expected_version="1.2.0.0",
        stable_keys=[keys.stable_pub], alpha_keys=[keys.alpha_pub],
    )
    assert not ok


def test_updater_refuses_a_release_with_no_manifest_once_keys_exist(tmp_path, keys):
    ok, _ = updater._verify_sha256sums(
        str(tmp_path / "WinZapp.zip"), "WinZapp.zip", "", expected_version="1.2.0.0",
        stable_keys=[keys.stable_pub], alpha_keys=[keys.alpha_pub],
    )
    assert not ok


def test_updater_still_checks_the_hash_of_a_signed_manifest(tmp_path, keys, monkeypatch):
    zip_path, manifest, sig = _signed_zip_release(tmp_path, keys, "1.2.0.0", keys.stable)
    zip_path.write_bytes(b"swapped after signing")
    monkeypatch.setattr(updater.requests, "get", _Responses({"https://x/sums": manifest, "https://x/sig": sig}))
    ok, detail = updater._verify_sha256sums(
        str(zip_path), "WinZapp.zip", "https://x/sums", signature_url="https://x/sig",
        expected_version="1.2.0.0", stable_keys=[keys.stable_pub], alpha_keys=[keys.alpha_pub],
    )
    assert not ok and "mismatch" in detail.lower()


def test_signature_asset_is_found_by_name():
    assets = [
        {"name": "SHA256SUMS.txt", "browser_download_url": "https://x/SHA256SUMS.txt"},
        {"name": "SHA256SUMS.txt.sig", "browser_download_url": "https://x/SHA256SUMS.txt.sig"},
    ]
    assert updater._find_signature_asset(assets) == "https://x/SHA256SUMS.txt.sig"
    assert updater._find_sha256sums_asset(assets) == "https://x/SHA256SUMS.txt"


# ── The signing script ───────────────────────────────────────────────────────

def test_render_keys_file_round_trips(tmp_path):
    path = tmp_path / "release_keys.py"
    original = (ROOT / "client" / "core" / "release_keys.py").read_text(encoding="utf-8")
    path.write_text(signing.render_keys_file(original, ["S1", "S2"], ["A1"]), encoding="utf-8")
    assert signing.read_configured_keys(path) == (("S1", "S2"), ("A1",))
    # Rewriting an already-filled file must replace, not append.
    path.write_text(signing.render_keys_file(path.read_text(encoding="utf-8"), ["S3"], []), encoding="utf-8")
    assert signing.read_configured_keys(path) == (("S3",), ())


def test_ci_sign_skips_quietly_while_signing_is_not_configured(tmp_path):
    manifest = tmp_path / "SHA256SUMS.txt"
    manifest.write_bytes(_manifest("1.2.0.5alpha"))
    code, message = signing.ci_sign(manifest, "1.2.0.5alpha", None, (), ())
    assert code == 0 and "not configured" in message
    assert not (tmp_path / rs.SIGNATURE_ASSET_NAME).exists()


def test_ci_sign_fails_when_keys_exist_but_the_secret_is_missing(tmp_path, keys):
    manifest = tmp_path / "SHA256SUMS.txt"
    manifest.write_bytes(_manifest("1.2.0.5alpha"))
    code, _ = signing.ci_sign(manifest, "1.2.0.5alpha", "", [keys.stable_pub], [keys.alpha_pub])
    assert code == 1


def test_ci_sign_fails_when_the_secret_is_not_a_trusted_key(tmp_path, keys):
    stranger, _ = _keypair()
    manifest = tmp_path / "SHA256SUMS.txt"
    manifest.write_bytes(_manifest("1.2.0.5alpha"))
    pem = signing.private_key_pem(stranger, None).decode("ascii")
    code, _ = signing.ci_sign(manifest, "1.2.0.5alpha", pem, [keys.stable_pub], [keys.alpha_pub])
    assert code == 1
    assert not (tmp_path / rs.SIGNATURE_ASSET_NAME).exists()


def test_ci_sign_fails_when_the_manifest_is_for_another_version(tmp_path, keys):
    manifest = tmp_path / "SHA256SUMS.txt"
    manifest.write_bytes(_manifest("1.2.0.4alpha"))
    pem = signing.private_key_pem(keys.alpha, None).decode("ascii")
    code, _ = signing.ci_sign(manifest, "1.2.0.5alpha", pem, [keys.stable_pub], [keys.alpha_pub])
    assert code == 1


def test_ci_sign_writes_a_signature_the_updater_accepts(tmp_path, keys):
    manifest = tmp_path / "SHA256SUMS.txt"
    manifest.write_bytes(_manifest("1.2.0.5alpha"))
    pem = signing.private_key_pem(keys.alpha, None).decode("ascii")
    code, message = signing.ci_sign(manifest, "1.2.0.5alpha", pem, [keys.stable_pub], [keys.alpha_pub])
    assert code == 0, message
    signature = (tmp_path / rs.SIGNATURE_ASSET_NAME).read_text(encoding="ascii")
    assert _check(keys, manifest.read_bytes(), signature, "v1.2.0.5alpha", True) == (True, "")


def test_encrypted_stable_key_round_trips_through_its_passphrase(keys):
    pem = signing.private_key_pem(keys.stable, b"correct horse battery")
    assert b"ENCRYPTED" in pem
    loaded = signing.load_private_key(pem, b"correct horse battery")
    assert signing.public_key_b64(loaded) == keys.stable_pub
    with pytest.raises(Exception):
        signing.load_private_key(pem, b"wrong passphrase")


def test_generate_refuses_to_write_private_keys_inside_the_repository(capsys):
    code = signing.cmd_generate(SimpleNamespace(out_dir=str(ROOT / "keys"), replace_existing=False))
    assert code == 1
    assert "inside the repository" in capsys.readouterr().err
    assert not (ROOT / "keys").exists()


def test_find_release_matches_the_exact_tag():
    releases = [{"tag_name": "v1.2.0.0beta"}, {"tag_name": "v1.2.0.0"}]
    assert signing.find_release(releases, "v1.2.0.0") == {"tag_name": "v1.2.0.0"}
    assert signing.find_release(releases, "v1.3.0.0") is None
