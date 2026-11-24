"""neonize_platform.py: the system the neonize library is built for and the
names that follow from it. Pure, runs on any system."""
import os

import pytest

import neonize_platform as np

WINDOWS = np.Target("windows", "amd64")
MAC_ARM = np.Target("darwin", "arm64")
MAC_INTEL = np.Target("darwin", "amd64")


@pytest.mark.parametrize("platform_name, machine, expected", [
    ("win32", "AMD64", WINDOWS),
    ("win32", "ARM64", WINDOWS),          # the bridge ships as x64, emulated on ARM
    ("darwin", "arm64", MAC_ARM),
    ("darwin", "x86_64", MAC_INTEL),      # an Intel Mac, or Python under Rosetta
    ("linux", "x86_64", np.Target("linux", "amd64")),
    ("linux", "aarch64", np.Target("linux", "arm64")),
])
def test_the_target_follows_the_system_and_the_processor(platform_name, machine, expected):
    assert np.target(platform_name, machine) == expected


@pytest.mark.parametrize("platform_name, machine", [("freebsd13", "amd64"), ("darwin", "ppc"),
                                                    ("linux", "i686")])
def test_a_system_or_processor_the_bridge_is_not_built_for_is_refused(platform_name, machine):
    with pytest.raises(ValueError, match="not built for"):
        np.target(platform_name, machine)


def test_the_library_has_the_name_neonize_loads_it_by():
    # neonize/utils/platform.py generated_name(): neonize-<os>-<arch>.<ext>
    assert np.library_name(WINDOWS) == "neonize-windows-amd64.dll"
    assert np.library_name(MAC_ARM) == "neonize-darwin-arm64.dylib"
    assert np.library_name(MAC_INTEL) == "neonize-darwin-amd64.dylib"


def test_the_zip_names_its_system_and_the_windows_one_keeps_its_name():
    assert np.asset_name("0.12.4", WINDOWS) == "neonize-0.12.4-winzapp-windows-amd64.zip"
    assert np.asset_name("0.12.4", MAC_ARM) == "neonize-0.12.4-winzapp-darwin-arm64.zip"


def test_protoc_and_the_virtualenv_follow_the_system():
    assert np.protoc_asset_key(WINDOWS) == "win64"
    assert np.protoc_asset_key(MAC_ARM) == "osx-aarch_64"
    assert np.protoc_asset_key(MAC_INTEL) == "osx-x86_64"
    assert np.executable("protoc", WINDOWS) == "protoc.exe"
    assert np.executable("protoc", MAC_ARM) == "protoc"
    assert np.venv_python("v", WINDOWS) == os.path.join("v", "Scripts", "python.exe")
    assert np.venv_python("v", MAC_ARM) == os.path.join("v", "bin", "python")


@pytest.mark.parametrize("build, dumpmachine", [
    (WINDOWS, "x86_64-w64-mingw32\n"),
    (MAC_ARM, "arm64-apple-darwin24.6.0"),
    (MAC_INTEL, "x86_64-apple-darwin24.6.0"),
    (np.Target("linux", "amd64"), "x86_64-linux-gnu"),
])
def test_a_compiler_for_the_target_is_accepted(build, dumpmachine):
    assert np.compiler_problem(build, dumpmachine) == ""


@pytest.mark.parametrize("build, dumpmachine, message", [
    (WINDOWS, "i686-w64-mingw32", "64-bit"),
    (WINDOWS, "x86_64-pc-cygwin", "MinGW"),
    (MAC_ARM, "x86_64-apple-darwin24.6.0", "not for arm64"),
    (MAC_INTEL, "x86_64-linux-gnu", "not for macOS"),
])
def test_a_compiler_for_another_target_is_refused(build, dumpmachine, message):
    assert message in np.compiler_problem(build, dumpmachine)


def test_the_compiler_cgo_uses_by_default():
    assert np.default_compiler(WINDOWS) == "gcc"
    assert np.default_compiler(MAC_ARM) == "cc"
