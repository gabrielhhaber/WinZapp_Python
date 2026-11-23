import types

import bridge_arch


def _platform(machine):
    return types.SimpleNamespace(machine=lambda: machine)


def test_an_x64_process_under_emulation_on_arm_asks_for_the_amd64_dll():
    # platform.machine() says ARM64 there (it reads PROCESSOR_ARCHITEW6432 first)
    module = _platform("ARM64")
    assert bridge_arch.pin("win-amd64", module, types.SimpleNamespace(platform="win32")) is True
    assert module.machine() == "AMD64"


def test_a_native_process_is_left_alone():
    module = _platform("AMD64")
    assert bridge_arch.pin("win-amd64", module, types.SimpleNamespace(platform="win32")) is False
    native = _platform("ARM64")
    assert bridge_arch.pin("win-arm64", native, types.SimpleNamespace(platform="win32")) is False


def test_other_systems_are_left_alone():
    module = _platform("aarch64")
    assert bridge_arch.pin("linux-aarch64", module, types.SimpleNamespace(platform="linux")) is False
    assert module.machine() == "aarch64"


def test_process_machine_falls_back_to_what_the_system_reported():
    assert bridge_arch.process_machine("linux-x86_64", "x86_64") == "x86_64"
    assert bridge_arch.process_machine("win-amd64", "ARM64") == "AMD64"
