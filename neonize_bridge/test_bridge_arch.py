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


def test_under_emulation_go_async_preemption_is_turned_off():
    environ = {}
    module = _platform("ARM64")
    assert bridge_arch.prepare("win-amd64", module, types.SimpleNamespace(platform="win32"), environ) is True
    assert environ["GODEBUG"] == "asyncpreemptoff=1"


def test_a_native_process_keeps_go_preemption():
    environ = {"GODEBUG": "madvdontneed=1"}
    module = _platform("AMD64")
    assert bridge_arch.prepare("win-amd64", module, types.SimpleNamespace(platform="win32"), environ) is False
    assert environ == {"GODEBUG": "madvdontneed=1"}


def test_other_godebug_settings_are_kept():
    assert bridge_arch.without_async_preemption("madvdontneed=1") == "madvdontneed=1,asyncpreemptoff=1"
    assert bridge_arch.without_async_preemption("") == "asyncpreemptoff=1"


def test_an_explicit_asyncpreemptoff_is_respected():
    assert bridge_arch.without_async_preemption("asyncpreemptoff=0") == "asyncpreemptoff=0"
