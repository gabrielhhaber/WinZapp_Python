"""Ed25519 signatures over a release's SHA256SUMS.txt.

The checksum manifest on its own proves a download matches the release it came
from — and nothing about who made that release. It is uploaded next to the ZIP
it describes, so anyone who can replace the ZIP (a stolen maintainer account,
any of the collaborators with write access, a hijacked workflow) replaces the
manifest in the same breath. A signature made with a key that never lives on
GitHub is what separates "the release says so" from "the maintainer says so".

What is signed is the manifest's exact bytes, and the manifest carries the
version it belongs to on a comment line (``# winzapp-version: 1.2.0.0``,
written by build-windows.yml). Binding the version is not decoration: without
it, an attacker could take a genuine, correctly signed OLD release and publish
it again under a higher tag — every check would pass and users would be
"updated" onto a build with known holes. The updater already ignores ``#``
lines when reading hashes, so builds that predate this module parse the new
manifest unchanged.

Pure module on purpose: no wx, no network, no reading of the configured keys.
The updater and the signing script both pass keys in, which is what lets the
tests drive every branch.
"""

from __future__ import annotations

import base64
import binascii
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

SIGNATURE_ASSET_NAME = "SHA256SUMS.txt.sig"
MANIFEST_VERSION_PREFIX = "# winzapp-version:"

_ALPHA_VERSION_RE = re.compile(r"alpha$", re.IGNORECASE)


def normalize_version(version: str) -> str:
    """``v1.2.0.0Beta`` and ``1.2.0.0beta`` name the same release."""
    return (version or "").strip().lstrip("vV").lower()


def manifest_version(manifest: bytes) -> str:
    """The version a manifest declares it belongs to, or "" when it has none."""
    text = manifest.decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith(MANIFEST_VERSION_PREFIX):
            return normalize_version(line[len(MANIFEST_VERSION_PREFIX):])
    return ""


def signing_is_configured(stable_keys, alpha_keys) -> bool:
    """Whether this build enforces signatures at all.

    False only while both lists are empty — the state before the maintainer ran
    ``release_signing.py generate``. Enforcement then turns on for every build
    made after the keys are committed, with no flag to forget.
    """
    return bool(stable_keys) or bool(alpha_keys)


def accepted_keys(version: str, is_alpha_release: bool, stable_keys, alpha_keys) -> "list[str]":
    """The keys allowed to vouch for *version*.

    Stable keys always count. Alpha keys count only when the release is an
    alpha by its tag/name (*is_alpha_release*, the same test the channel filter
    uses) AND by the version string inside the signed manifest — both, because
    either one alone is a label the release's publisher chooses freely.
    """
    keys = list(stable_keys)
    if is_alpha_release and _ALPHA_VERSION_RE.search(normalize_version(version)):
        keys.extend(alpha_keys)
    return keys


def encode_signature(signature: bytes) -> str:
    """The ``.sig`` asset's text: the raw signature in base64, one line."""
    return base64.b64encode(signature).decode("ascii") + "\n"


def decode_signature(text: str) -> bytes:
    """Inverse of encode_signature(). ``#`` lines are comments. Raises ValueError."""
    body = "".join(
        line.strip() for line in (text or "").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    try:
        signature = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"signature is not valid base64: {exc}") from None
    if len(signature) != 64:
        raise ValueError(f"signature has {len(signature)} bytes, expected 64")
    return signature


def load_public_key(encoded: str) -> Ed25519PublicKey:
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) != 32:
        raise ValueError(f"public key has {len(raw)} bytes, expected 32")
    return Ed25519PublicKey.from_public_bytes(raw)


def signature_matches(manifest: bytes, signature: bytes, keys) -> bool:
    """True when any of *keys* verifies *signature* over *manifest*.

    A malformed key is skipped rather than raised: one bad entry in the list
    must not stop the other, valid keys from being tried.
    """
    for encoded in keys:
        try:
            load_public_key(encoded).verify(signature, manifest)
            return True
        except (InvalidSignature, ValueError, binascii.Error):
            continue
    return False


def check_release_manifest(
    manifest: "bytes | None",
    signature_text: "str | None",
    expected_version: str,
    is_alpha_release: bool,
    stable_keys,
    alpha_keys,
) -> "tuple[bool, str]":
    """Decide whether a release's manifest may be trusted. Returns (ok, detail).

    *manifest* / *signature_text* are None when the release carries no such
    asset. Fails CLOSED on everything once signing is configured: a release
    that shows up without a signature after the keys exist is precisely what a
    forged release looks like. Before that, only the manifest's own presence
    matters, exactly as it did before signatures existed.
    """
    if not signing_is_configured(stable_keys, alpha_keys):
        return True, ""

    if manifest is None:
        return False, "Release has no SHA256SUMS.txt, but this build requires signed releases"
    if signature_text is None:
        return False, f"Release has no {SIGNATURE_ASSET_NAME}, but this build requires signed releases"

    declared = manifest_version(manifest)
    if not declared:
        return False, "SHA256SUMS.txt does not declare which version it belongs to"
    if declared != normalize_version(expected_version):
        return False, (
            f"SHA256SUMS.txt belongs to version {declared}, "
            f"not to the release being installed ({normalize_version(expected_version)})"
        )

    try:
        signature = decode_signature(signature_text)
    except ValueError as exc:
        return False, f"Invalid {SIGNATURE_ASSET_NAME}: {exc}"

    keys = accepted_keys(declared, is_alpha_release, stable_keys, alpha_keys)
    if not signature_matches(manifest, signature, keys):
        return False, "The release signature does not match any key this build trusts"
    return True, ""
