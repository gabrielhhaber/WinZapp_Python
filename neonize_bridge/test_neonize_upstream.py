"""neonize_upstream.json and its rules (neonize_upstream.py), plus the patches
next to it: pure, no git, no Go, no network."""
import copy
import os
import re

import pytest

import neonize_upstream as nu

HERE = os.path.dirname(os.path.abspath(__file__))
COMMIT = "ce082701aeb94d3fa2ede0e61f397b755051f556"
OTHER = "0" * 40


def test_only_neonize_metadata_is_stale():
    listing = ["neonize", "neonize-0.5.2.dist-info", "Neonize-0.4.0.dist-info",
               "neonize_extra-1.0.dist-info", "segno-1.6.6.dist-info", "neonize-0.5.2.data"]
    assert nu.stale_metadata(listing) == ["Neonize-0.4.0.dist-info", "neonize-0.5.2.dist-info"]


def manifest():
    return nu.load(os.path.join(HERE, nu.MANIFEST))


def test_the_committed_manifest_is_valid_and_pins_whole_commits():
    info = manifest()
    current = nu.current(info)
    assert re.fullmatch(r"\d+\.\d+\.\d+", current["version"])
    assert re.fullmatch(r"[0-9a-f]{40}", current["commit"])


@pytest.mark.parametrize("change, message", [
    (lambda m: m.update(schema=2), "schema"),
    (lambda m: m["neonize"].update(approved=[]), "empty"),
    (lambda m: m["neonize"]["approved"][0].update(version="0.6"), "X.Y.Z"),
    (lambda m: m["neonize"]["approved"][0].update(commit="ce08270"), "full SHA-1"),
    (lambda m: m["whatsmeow"].update(version="latest"), "Go module version"),
    (lambda m: m["meowcaller"].update(commit="main"), "full SHA-1"),
    (lambda m: m["toolchain"]["protoc"].update(sha256="x"), "SHA-256"),
    (lambda m: m.update(release={"tag": "../x", "asset": "a.zip", "sha256": "0" * 64}), "plain"),
])
def test_a_malformed_manifest_is_refused(change, message):
    broken = copy.deepcopy(manifest())
    change(broken)
    with pytest.raises(ValueError, match=message):
        nu.validate(broken)


def test_approval_appends_the_new_current_release_without_touching_the_original():
    info = manifest()
    updated = nu.with_approval(info, "9.9.9", OTHER, "2027-01-01")
    assert nu.current(updated) == {"version": "9.9.9", "commit": OTHER, "approved_on": "2027-01-01"}
    assert nu.current(info)["version"] != "9.9.9"
    again = nu.with_approval(updated, "9.9.9", OTHER, "2027-02-02")
    assert [e["version"] for e in again["neonize"]["approved"]].count("9.9.9") == 1
    assert nu.current(again)["approved_on"] == "2027-02-02"


def test_approval_refuses_a_version_moved_to_another_commit_or_a_short_sha():
    info = nu.with_approval(manifest(), "9.9.9", OTHER, "2027-01-01")
    with pytest.raises(ValueError, match="another commit"):
        nu.with_approval(info, "9.9.9", "1" * 40, "2027-01-02")
    with pytest.raises(ValueError):
        nu.with_approval(info, "9.9.10", "abc123", "2027-01-02")
    with pytest.raises(ValueError):
        nu.with_approval(info, "latest", OTHER, "2027-01-02")


GO_MOD = """module github.com/krypton-byte/neonize

go 1.26.0

require (
	github.com/lib/pq v1.12.3
	go.mau.fi/whatsmeow v0.0.0-20260921121126-35ae40906e74
)

replace go.mau.fi/whatsmeow => ../../whatsmeow

replace github.com/purpshell/meowcaller => ../../meowcaller
"""


def test_go_mod_reading():
    assert nu.required_version(GO_MOD, "go.mau.fi/whatsmeow") == "v0.0.0-20260921121126-35ae40906e74"
    assert nu.required_version(GO_MOD, "github.com/absent/x") == ""
    assert nu.required_version("require go.mau.fi/whatsmeow v1.2.3\n", "go.mau.fi/whatsmeow") == "v1.2.3"
    assert nu.replaced_by(GO_MOD, "go.mau.fi/whatsmeow") == "../../whatsmeow"
    assert nu.replaced_by(GO_MOD, "github.com/purpshell/meowcaller") == "../../meowcaller"
    assert nu.replaced_by(GO_MOD, "github.com/lib/pq") == ""


def test_release_url_and_asset_name():
    release = {"tag": "neonize-0.6.0-winzapp.1", "asset": nu.asset_name("0.6.0"), "sha256": "0" * 64}
    assert nu.release_url("gabrielhhaber/WinZapp_Python", release) == (
        "https://github.com/gabrielhhaber/WinZapp_Python/releases/download/"
        "neonize-0.6.0-winzapp.1/neonize-0.6.0-winzapp-windows-amd64.zip")
    assert nu.sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


# ------------------------------------------------------------- the patches
def _patched_paths(name):
    folder = os.path.join(HERE, "neonize_patches", name)
    paths = set()
    for file_name in sorted(os.listdir(folder)):
        with open(os.path.join(folder, file_name), encoding="utf-8") as f:
            paths.update(re.findall(r"^diff --git a/(\S+) b/", f.read(), re.MULTILINE))
    return paths


@pytest.mark.parametrize("name", ["neonize", "whatsmeow", "meowcaller"])
def test_every_tree_has_numbered_patches(name):
    files = sorted(os.listdir(os.path.join(HERE, "neonize_patches", name)))
    assert files and all(re.match(r"^\d{4}-.+\.patch$", f) for f in files)


def test_no_patch_carries_generated_protobuf_code():
    # build_neonize.py regenerates them from Neonize.proto; in a patch they
    # would conflict on every upstream release
    assert not set(nu.GENERATED) & _patched_paths("neonize")


def test_the_patches_name_winzapp_not_another_program():
    folder = os.path.join(HERE, "neonize_patches")
    for root, _dirs, files in os.walk(folder):
        for file_name in files:
            with open(os.path.join(root, file_name), encoding="utf-8") as f:
                assert "telezapp" not in f.read().lower(), file_name


def test_uncommitted_reads_porcelain_z_and_ignores_generated_code():
    listing = (" M goneonize/defproto/Neonize.pb.go\0"
               " M goneonize/main.go\0"
               "?? notes.txt\0"
               "R  new/name.go\0old/name.go\0"
               " M neonize/proto/Neonize_pb2.py\0")
    assert nu.uncommitted(listing) == ["goneonize/main.go", "notes.txt", "new/name.go"]
    assert nu.uncommitted("") == []


def test_the_identity_changes_with_any_patch_byte_or_pin():
    info = manifest()
    patches = nu.read_patches(os.path.join(HERE, "neonize_patches"))
    base = nu.build_identity(nu.pins(info), patches)
    assert base == nu.checkout_identity(HERE)
    tree, name = "neonize", sorted(patches["neonize"])[0]
    edited = {t: dict(files) for t, files in patches.items()}
    edited[tree][name] = patches[tree][name] + b" "
    assert nu.build_identity(nu.pins(info), edited) != base
    assert nu.build_identity(nu.pins(info, OTHER), patches) != base


def test_native_neteq_sources_and_cargo_checksums_invalidate_artifact_identity(tmp_path):
    folder=tmp_path/'neonize_bridge/neonize_patches'
    folder.mkdir(parents=True)
    native=tmp_path/'native/call_neteq'
    (native/'src').mkdir(parents=True)
    for name in ('Cargo.toml','Cargo.lock','src/lib.rs','THIRD_PARTY_NOTICES.txt'):
        (native/name).write_bytes(b'first\nsecond\n')
    helper=folder.parent/'build_neteq.py'
    helper.write_bytes(b'build\n')
    baseline=nu.read_patches(str(folder))
    assert set(baseline['call_neteq'])=={'Cargo.toml','Cargo.lock','src/lib.rs','THIRD_PARTY_NOTICES.txt','build_neteq.py'}
    (native/'src/lib.rs').write_bytes(b'first\r\nsecond\r\n')
    assert nu.read_patches(str(folder))==baseline
    (native/'Cargo.lock').write_bytes(b'new dependency checksum\n')
    assert nu.build_identity({},nu.read_patches(str(folder)))!=nu.build_identity({},baseline)
