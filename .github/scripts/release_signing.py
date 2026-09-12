"""Create, use and check the keys that sign WinZapp releases.

See client/core/release_signature.py for what a signature proves and
client/core/release_keys.py for why stable and alpha keys are kept apart.

Four subcommands:

  generate --out-dir DIR
      One-time setup, run by the maintainer on their own machine. Creates a
      primary and a backup stable key (encrypted with a passphrase) and the
      alpha key CI signs with, then writes the three PUBLIC keys into
      client/core/release_keys.py. Refuses to write private keys anywhere
      inside the repository.

  ci-sign MANIFEST --version V
      Run by alpha-release.yml. Signs dist/SHA256SUMS.txt with the key in the
      ALPHA_SIGNING_KEY environment variable. While release_keys.py is still
      empty it signs nothing and exits 0, so alphas keep publishing before
      signing is set up; once keys exist a missing or mismatched secret FAILS
      the run, because every client built from then on would refuse the alpha.

  sign-stable TAG --key PATH
      Run by the maintainer after release.yml has built a stable release into a
      draft. Downloads the draft's SHA256SUMS.txt, signs it with the offline
      stable key, uploads SHA256SUMS.txt.sig and then publishes the draft.

  verify MANIFEST SIGNATURE --version V [--alpha]
      Checks a signature against the keys in release_keys.py, for debugging.

This file only ever reads the manifest and talks to GitHub through the `gh`
CLI the maintainer is already logged into; no token is read or printed here.
"""

from __future__ import annotations

import argparse
import ast
import getpass
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "client"))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from core import release_signature as rs  # noqa: E402

KEYS_FILE = ROOT / "client" / "core" / "release_keys.py"
ALPHA_KEY_ENV = "ALPHA_SIGNING_KEY"
MIN_PASSPHRASE_LENGTH = 12

_KEY_ASSIGNMENT_RE = {
    # Base64 never contains ")", so the first one closes the tuple.
    name: re.compile(rf"^{name}: \"tuple\[str, \.\.\.\]\" = \([^)]*\)", re.MULTILINE)
    for name in ("STABLE_PUBLIC_KEYS", "ALPHA_PUBLIC_KEYS")
}


# ── Keys file ────────────────────────────────────────────────────────────────

def read_configured_keys(path: pathlib.Path = KEYS_FILE) -> "tuple[tuple[str, ...], tuple[str, ...]]":
    """(stable, alpha) as written in release_keys.py, read without importing it."""
    values = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            values[node.target.id] = tuple(ast.literal_eval(node.value))
    return values.get("STABLE_PUBLIC_KEYS", ()), values.get("ALPHA_PUBLIC_KEYS", ())


def render_keys_file(text: str, stable: "list[str]", alpha: "list[str]") -> str:
    """*text* (release_keys.py) with both key tuples replaced."""
    for name, keys in (("STABLE_PUBLIC_KEYS", stable), ("ALPHA_PUBLIC_KEYS", alpha)):
        body = "".join(f'\n    "{k}",' for k in keys)
        replacement = f'{name}: "tuple[str, ...]" = ({body}\n)' if keys else f'{name}: "tuple[str, ...]" = ()'
        text, count = _KEY_ASSIGNMENT_RE[name].subn(lambda _m: replacement, text, count=1)
        if count != 1:
            raise ValueError(f"could not find the {name} line in release_keys.py")
    return text


# ── Key material ─────────────────────────────────────────────────────────────

def public_key_b64(private_key: Ed25519PrivateKey) -> str:
    import base64
    raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def private_key_pem(private_key: Ed25519PrivateKey, passphrase: "bytes | None") -> bytes:
    encryption = (
        serialization.BestAvailableEncryption(passphrase) if passphrase
        else serialization.NoEncryption()
    )
    return private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption,
    )


def load_private_key(pem: bytes, passphrase: "bytes | None") -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(pem, password=passphrase)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("the key file is not an Ed25519 private key")
    return key


def sign_manifest(manifest: bytes, private_key: Ed25519PrivateKey) -> str:
    return rs.encode_signature(private_key.sign(manifest))


def is_inside(path: pathlib.Path, parent: pathlib.Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


# ── ci-sign ──────────────────────────────────────────────────────────────────

def ci_sign(manifest_path: pathlib.Path, expected_version: str, key_pem: "str | None",
            stable_keys, alpha_keys) -> "tuple[int, str]":
    """Sign an alpha manifest in CI. Returns (exit code, message); writes the
    .sig next to the manifest only on success."""
    if not rs.signing_is_configured(stable_keys, alpha_keys):
        return 0, (
            "::warning::Release signing is not configured yet (client/core/release_keys.py "
            "is empty) — publishing this alpha unsigned, as before."
        )
    if not key_pem or not key_pem.strip():
        return 1, (
            f"::error::client/core/release_keys.py trusts signing keys but {ALPHA_KEY_ENV} is "
            "empty. Add the alpha private key as a secret of the 'alpha-release' environment; "
            "publishing unsigned would ship an alpha every updated client refuses."
        )

    manifest = manifest_path.read_bytes()
    try:
        private_key = load_private_key(key_pem.encode("utf-8"), None)
    except Exception as exc:
        return 1, f"::error::{ALPHA_KEY_ENV} is not a readable unencrypted Ed25519 PEM key: {exc}"

    signature = sign_manifest(manifest, private_key)
    ok, detail = rs.check_release_manifest(
        manifest, signature, expected_version, True, stable_keys, alpha_keys,
    )
    if not ok:
        return 1, (
            f"::error::The signed alpha would be refused by the updater: {detail}. "
            f"Check that {ALPHA_KEY_ENV} is the private half of a key in ALPHA_PUBLIC_KEYS."
        )

    sig_path = manifest_path.with_name(rs.SIGNATURE_ASSET_NAME)
    sig_path.write_text(signature, encoding="ascii", newline="\n")
    return 0, f"[INFO] Signed {manifest_path.name} for {expected_version} -> {sig_path.name}"


# ── GitHub (through the gh CLI) ──────────────────────────────────────────────

def _gh(args: "list[str]", *, binary: bool = False, input_path: "str | None" = None):
    cmd = ["gh", "api", *args]
    if input_path:
        cmd += ["--input", input_path]
    result = subprocess.run(cmd, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"gh api {args[0]} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout if binary else json.loads(result.stdout or b"null")


def _repo() -> str:
    out = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        capture_output=True, text=True, check=True, cwd=ROOT,
    )
    return out.stdout.strip()


def find_release(releases: list, tag: str) -> "dict | None":
    for release in releases or []:
        if (release.get("tag_name") or "") == tag:
            return release
    return None


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_generate(args) -> int:
    out_dir = pathlib.Path(args.out_dir).expanduser()
    if is_inside(out_dir, ROOT):
        print("Refusing: private keys must never be written inside the repository.", file=sys.stderr)
        return 1
    stable, alpha = read_configured_keys()
    if rs.signing_is_configured(stable, alpha) and not args.replace_existing:
        print(
            "Refusing: release_keys.py already trusts keys. Replacing them locks every "
            "installed client out of updates signed with the new keys unless the old key "
            "signs a release carrying them first. Pass --replace-existing only if you know that.",
            file=sys.stderr,
        )
        return 1

    names = ("stable-primary.pem", "stable-backup.pem", "alpha-signing.pem")
    out_dir.mkdir(parents=True, exist_ok=True)
    if any((out_dir / n).exists() for n in names):
        print(f"Refusing: {out_dir} already holds key files.", file=sys.stderr)
        return 1

    passphrase = getpass.getpass("Passphrase for the stable keys: ")
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        print(f"The passphrase must have at least {MIN_PASSPHRASE_LENGTH} characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Repeat the passphrase: ") != passphrase:
        print("The passphrases do not match.", file=sys.stderr)
        return 1

    primary, backup, alpha_key = (Ed25519PrivateKey.generate() for _ in range(3))
    (out_dir / names[0]).write_bytes(private_key_pem(primary, passphrase.encode("utf-8")))
    (out_dir / names[1]).write_bytes(private_key_pem(backup, passphrase.encode("utf-8")))
    (out_dir / names[2]).write_bytes(private_key_pem(alpha_key, None))

    KEYS_FILE.write_text(
        render_keys_file(
            KEYS_FILE.read_text(encoding="utf-8"),
            [public_key_b64(primary), public_key_b64(backup)],
            [public_key_b64(alpha_key)],
        ),
        encoding="utf-8", newline="\n",
    )

    print(f"""
Keys written to {out_dir}. Public keys written to client/core/release_keys.py.

Do these IN THIS ORDER — committing release_keys.py first makes the next alpha
build fail on purpose, since it could not be signed:

 1. Move stable-backup.pem to separate storage (a USB drive kept elsewhere).
    It is the only way to keep shipping stable updates if the primary is lost.
 2. Create the secret for the alpha key, then delete the file:
      gh secret set {ALPHA_KEY_ENV} --env alpha-release < "{out_dir / names[2]}"
 3. Commit client/core/release_keys.py to main.
""")
    return 0


def cmd_ci_sign(args) -> int:
    stable, alpha = read_configured_keys()
    code, message = ci_sign(
        pathlib.Path(args.manifest), args.version, os.environ.get(ALPHA_KEY_ENV), stable, alpha,
    )
    print(message, file=sys.stderr if code else sys.stdout)
    return code


def cmd_verify(args) -> int:
    stable, alpha = read_configured_keys()
    ok, detail = rs.check_release_manifest(
        pathlib.Path(args.manifest).read_bytes(),
        pathlib.Path(args.signature).read_text(encoding="ascii"),
        args.version, args.alpha, stable, alpha,
    )
    print("OK" if ok else f"FAILED: {detail}")
    return 0 if ok else 1


def cmd_sign_stable(args) -> int:
    tag = args.tag
    if "alpha" in tag.lower():
        print("Alpha releases are signed by alpha-release.yml, not by hand.", file=sys.stderr)
        return 1
    stable, alpha = read_configured_keys()
    repo = _repo()

    release = find_release(_gh([f"repos/{repo}/releases?per_page=100"]), tag)
    if release is None:
        print(f"No release for {tag}. Push the tag and wait for release.yml to finish.", file=sys.stderr)
        return 1
    if not release.get("draft"):
        print(
            f"{tag} is already published. Immutable releases cannot receive a signature "
            "afterwards: publish a new version instead.", file=sys.stderr,
        )
        return 1

    assets = {a["name"]: a for a in release.get("assets", [])}
    if "SHA256SUMS.txt" not in assets or "WinZapp.zip" not in assets:
        print(f"The {tag} draft is missing WinZapp.zip or SHA256SUMS.txt.", file=sys.stderr)
        return 1
    manifest = _gh(
        ["-H", "Accept: application/octet-stream", f"repos/{repo}/releases/assets/{assets['SHA256SUMS.txt']['id']}"],
        binary=True,
    )
    declared = rs.manifest_version(manifest)
    if declared != rs.normalize_version(tag):
        print(f"The draft's SHA256SUMS.txt declares {declared or 'no version'}, not {tag}.", file=sys.stderr)
        return 1

    if stable:
        key_pem = pathlib.Path(args.key).expanduser().read_bytes() if args.key else None
        if key_pem is None:
            print("--key is required: this repository trusts stable signing keys.", file=sys.stderr)
            return 1
        private_key = load_private_key(key_pem, getpass.getpass("Stable key passphrase: ").encode("utf-8"))
        signature = sign_manifest(manifest, private_key)
        ok, detail = rs.check_release_manifest(manifest, signature, tag, False, stable, ())
        if not ok:
            print(f"Not uploading: {detail}. Is this the key in STABLE_PUBLIC_KEYS?", file=sys.stderr)
            return 1
        if rs.SIGNATURE_ASSET_NAME in assets:
            _gh(["--method", "DELETE", f"repos/{repo}/releases/assets/{assets[rs.SIGNATURE_ASSET_NAME]['id']}"], binary=True)
        with tempfile.NamedTemporaryFile("w", suffix=".sig", delete=False, encoding="ascii", newline="\n") as tmp:
            tmp.write(signature)
        try:
            _gh([
                "--method", "POST", "-H", "Content-Type: application/octet-stream",
                f"https://uploads.github.com/repos/{repo}/releases/{release['id']}/assets?name={rs.SIGNATURE_ASSET_NAME}",
            ], input_path=tmp.name)
        finally:
            os.unlink(tmp.name)
        print(f"Signed and uploaded {rs.SIGNATURE_ASSET_NAME} to the {tag} draft.")
    else:
        print("Release signing is not configured yet — publishing without a signature.")

    if not args.yes and input(f"Publish {tag} to every stable user now? [s/N] ").strip().lower() not in ("s", "sim", "y", "yes"):
        print("Left as a draft. Run this again to publish.")
        return 0
    _gh(["--method", "PATCH", f"repos/{repo}/releases/{release['id']}",
         "-F", "draft=false", "-F", "prerelease=false", "-f", "make_latest=true"])
    print(f"Published {tag}. release-published.yml now bumps client/version.py on main.")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("generate")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--replace-existing", action="store_true")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("ci-sign")
    p.add_argument("manifest")
    p.add_argument("--version", required=True)
    p.set_defaults(func=cmd_ci_sign)

    p = sub.add_parser("sign-stable")
    p.add_argument("tag")
    p.add_argument("--key")
    p.add_argument("--yes", action="store_true", help="publish without asking")
    p.set_defaults(func=cmd_sign_stable)

    p = sub.add_parser("verify")
    p.add_argument("manifest")
    p.add_argument("signature")
    p.add_argument("--version", required=True)
    p.add_argument("--alpha", action="store_true")
    p.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
