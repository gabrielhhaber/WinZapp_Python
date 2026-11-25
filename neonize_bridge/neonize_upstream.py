"""The neonize WinZapp builds from: `neonize_upstream.json` and its rules.

The DLL is official neonize (github.com/krypton-byte/neonize) at an approved
release, plus WinZapp's patches in `neonize_patches/` (neonize, whatsmeow and
meowcaller), the same way WPPConnect Server is upstream plus
`client/api_patches/`. A new upstream release is approved by hand, after
`build_neonize.py check` has applied the patches and built it
(docs/reference/neonize-build.md).

The manifest, in short:

- `neonize.approved`: releases the patches were checked against, oldest
  first; builds use the last one. Each names its commit, so a moved tag
  cannot change what is built.
- `whatsmeow`: the version neonize's go.mod requires (the patched copy
  replaces it); `meowcaller`: the commit the call patches apply to.
- `toolchain`: what regenerates neonize's protobuf code byte for byte as
  upstream does (protoc and its SHA-256, protoc-gen-go, mypy-protobuf).
- `release`: the WinZapp release with the built package that
  `prepare.py` downloads, or null until one is published. Its `identity`
  (BUILD_INFO.json) says which pins and patches it was built from: a release
  of older patches is not used.

Pure functions, no network and no subprocess: tested in
test_neonize_upstream.py.
"""
import hashlib
import json
import re

SCHEMA = 1
MANIFEST = "neonize_upstream.json"

#: Generated from Neonize.proto at build time, never part of a patch.
GENERATED = ("goneonize/defproto/Neonize.pb.go",
             "neonize/proto/Neonize_pb2.py",
             "neonize/proto/Neonize_pb2.pyi")

DLL_NAME = "neonize-windows-amd64.dll"

_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_GO_PSEUDO = re.compile(r"^v\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TAG = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]*$")


def validate(manifest) -> dict:
    """The manifest itself when it is well formed; ValueError saying what is wrong."""
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise ValueError(f"not a schema {SCHEMA} manifest")
    approved = (manifest.get("neonize") or {}).get("approved")
    if not isinstance(approved, list) or not approved:
        raise ValueError("neonize.approved is empty")
    for entry in approved:
        if not _VERSION.match(str(entry.get("version", ""))):
            raise ValueError(f"neonize version {entry.get('version')!r} is not X.Y.Z")
        if not _COMMIT.match(str(entry.get("commit", ""))):
            raise ValueError(f"neonize {entry['version']}: commit is not a full SHA-1")
    if not _GO_PSEUDO.match(str((manifest.get("whatsmeow") or {}).get("version", ""))):
        raise ValueError("whatsmeow.version is not a Go module version")
    if not _COMMIT.match(str((manifest.get("meowcaller") or {}).get("commit", ""))):
        raise ValueError("meowcaller.commit is not a full SHA-1")
    protoc = (manifest.get("toolchain") or {}).get("protoc") or {}
    if not _SHA256.match(str(protoc.get("sha256", ""))):
        raise ValueError("toolchain.protoc.sha256 is not a SHA-256")
    release = manifest.get("release")
    if release is not None:
        if not _TAG.match(str(release.get("tag", ""))) or not _TAG.match(str(release.get("asset", ""))):
            raise ValueError("release tag/asset are not plain names")
        if not _SHA256.match(str(release.get("sha256", ""))):
            raise ValueError("release.sha256 is not a SHA-256")
        if not _SHA256.match(str(release.get("identity", ""))):
            raise ValueError("release.identity is not a build identity (BUILD_INFO.json)")
    return manifest


def load(path) -> dict:
    with open(path, encoding="utf-8") as stream:
        return validate(json.load(stream))


def current(manifest) -> dict:
    """The approved neonize release builds use: the last one."""
    return manifest["neonize"]["approved"][-1]


def with_approval(manifest, version, commit, today) -> dict:
    """A copy of the manifest with `version` at `commit` approved as the new
    current release. Approving the current one again only refreshes its date."""
    if not _VERSION.match(version) or not _COMMIT.match(commit):
        raise ValueError("approve needs an X.Y.Z version and a full commit SHA-1")
    updated = json.loads(json.dumps(manifest))
    approved = updated["neonize"]["approved"]
    for entry in approved:
        if entry["version"] == version and entry["commit"] != commit:
            raise ValueError(f"{version} was approved at another commit ({entry['commit']})")
    approved[:] = [e for e in approved if e["version"] != version]
    approved.append({"version": version, "commit": commit, "approved_on": today})
    return validate(updated)


def uncommitted(porcelain_z: str) -> list:
    """Paths `git status --porcelain -z` lists, minus the generated files (which
    every build rewrites). -z keeps each entry's leading status columns intact,
    which a stripped plain listing does not."""
    paths, entries = [], porcelain_z.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        if entry[0] in "RC":        # a rename or copy: the source path follows
            index += 1
        path = entry[3:]
        if path not in GENERATED:
            paths.append(path)
    return paths


def required_version(go_mod: str, module: str) -> str:
    """The version `go.mod` requires of `module` ('' when it does not)."""
    found = re.search(rf"^\s*(?:require\s+)?{re.escape(module)}\s+(v\S+)", go_mod, re.MULTILINE)
    return found.group(1) if found else ""


def replaced_by(go_mod: str, module: str) -> str:
    """The local path a `replace` sends `module` to ('' when none does)."""
    found = re.search(rf"^\s*replace\s+{re.escape(module)}\s+=>\s+(\S+)\s*$", go_mod, re.MULTILINE)
    return found.group(1) if found else ""


def release_url(repository: str, release: dict) -> str:
    """Download URL of the WinZapp release asset (`repository` is owner/name)."""
    return (f"https://github.com/{repository}/releases/download/"
            f"{release['tag']}/{release['asset']}")


def asset_name(version: str) -> str:
    return f"neonize-{version}-winzapp-windows-amd64.zip"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stale_metadata(site_entries) -> list:
    """The `neonize-*.dist-info` folders among a site-packages listing: what a
    PyPI neonize left behind. WinZapp's build is copied in without one, so they
    would make pip report a neonize that is no longer what runs."""
    return sorted(name for name in site_entries
                  if name.lower().startswith("neonize-") and name.lower().endswith(".dist-info"))


# ------------------------------------------------------------ build identity
#: In the bridge's virtualenv: the identity of the neonize installed there,
#: written by build_neonize.py install and prepare.py.
MARKER = ".neonize-build-sha256"
TREES = ("neonize", "whatsmeow", "meowcaller")


def pins(manifest, neonize_commit=None) -> dict:
    """What a build is made of besides the patches (`neonize_commit` for a
    build of another ref than the approved one)."""
    return {"neonize": neonize_commit or current(manifest)["commit"],
            "whatsmeow": manifest["whatsmeow"]["version"],
            "meowcaller": manifest["meowcaller"]["commit"],
            "toolchain": manifest["toolchain"]}


def build_identity(build_pins, patches) -> str:
    """One SHA-256 for everything that decides the built package: the pins and
    every patch (`patches`: {tree: {file name: bytes}}). A virtualenv whose
    marker holds another identity has an older or different build."""
    digest = hashlib.sha256(json.dumps(build_pins, sort_keys=True).encode())
    for tree in sorted(patches):
        for name in sorted(patches[tree]):
            digest.update(f"\0{tree}/{name}\0".encode() + patches[tree][name])
    return digest.hexdigest()


def read_patches(patches_dir) -> dict:
    """{tree: {file name: bytes}} of neonize_patches/."""
    import os
    found = {}
    for tree in TREES:
        folder = os.path.join(patches_dir, tree)
        found[tree] = {}
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else ():
            if name.endswith(".patch"):
                with open(os.path.join(folder, name), "rb") as f:
                    found[tree][name] = f.read()
    # The sidecar source and Cargo checksums are part of the downloadable
    # package too. A changed NetEq wrapper must invalidate the installed build.
    native = os.path.abspath(os.path.join(patches_dir, "..", "..", "native", "call_neteq"))
    paths = ("Cargo.toml", "Cargo.lock", "src/lib.rs", "THIRD_PARTY_NOTICES.txt")
    if os.path.isdir(native):
        found["call_neteq"] = {}
        for name in paths:
            with open(os.path.join(native, name), "rb") as handle:
                found["call_neteq"][name] = handle.read().replace(b"\r\n", b"\n")
        helper = os.path.join(patches_dir, "..", "build_neteq.py")
        with open(helper, "rb") as handle:
            found["call_neteq"]["build_neteq.py"] = handle.read().replace(b"\r\n", b"\n")
    return found


def checkout_identity(bridge_dir) -> str:
    """The identity this checkout's manifest and patches call for."""
    import os
    manifest = load(os.path.join(bridge_dir, MANIFEST))
    return build_identity(pins(manifest), read_patches(os.path.join(bridge_dir, "neonize_patches")))
