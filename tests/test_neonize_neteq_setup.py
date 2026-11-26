"""setup-neonize prepares NetEq even when Go and MinGW already exist.

No installers or compiler commands are executed by these tests.
"""
from types import SimpleNamespace

import pytest

from winzapp_tools import neonize_neteq_setup as neteq
from winzapp_tools import neonize_setup as setup


@pytest.fixture
def tools(monkeypatch):
    state = SimpleNamespace(msvc=True, rustup=True, pinned=True, calls=[], installs=[])
    monkeypatch.setenv("CARGO_HOME", "C:/test-cargo-home")
    monkeypatch.setenv("PATH", "original")
    monkeypatch.setattr(setup, "_path_from_registry", lambda: "refreshed")
    monkeypatch.setattr(neteq, "_has_msvc", lambda: state.msvc)
    monkeypatch.setattr(neteq, "_msvc_installation", lambda **k: "")

    def which(name, path=None):
        assert "refreshed" in path
        return "rustup.exe" if name == "rustup" and state.rustup else None

    def install(package, path, *extra):
        state.installs.append((package, extra))
        if package == "Rustlang.Rustup":
            state.rustup = True
        else:
            state.msvc = True

    def run(command, **kwargs):
        state.calls.append(command)
        if "run" in command:
            return SimpleNamespace(returncode=0 if state.pinned else 1)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(neteq.shutil, "which", which)
    monkeypatch.setattr(neteq, "_install", install)
    monkeypatch.setattr(neteq.subprocess, "run", run)
    return state


def test_existing_tools_are_reused_and_refreshed_path_reaches_the_build(tools):
    neteq.ensure_neteq_tools()
    assert tools.installs == []
    assert tools.calls == [["rustup.exe", "run", f"{neteq.RUST}-{neteq.TARGET}",
                            "cargo", "--version"]]
    assert neteq.os.environ["PATH"].endswith("refreshed")
    assert str(neteq.Path(neteq.os.environ["CARGO_HOME"]) / "bin") in neteq.os.environ["PATH"]


def test_missing_rustup_and_linker_are_installed_with_pinned_rust(tools):
    tools.msvc = tools.rustup = tools.pinned = False
    neteq.ensure_neteq_tools()
    assert [p for p, _ in tools.installs] == [
        "Microsoft.VisualStudio.2022.BuildTools", "Rustlang.Rustup"]
    assert "Microsoft.VisualStudio.Workload.VCTools" in tools.installs[0][1][-1]
    assert tools.calls[-1] == ["rustup.exe", "toolchain", "install",
                               f"{neteq.RUST}-{neteq.TARGET}", "--profile", "minimal"]


def test_missing_pin_does_not_change_the_default_toolchain(tools):
    tools.pinned = False
    neteq.ensure_neteq_tools()
    assert tools.installs == []
    assert tools.calls[-1][1:3] == ["toolchain", "install"]
    assert all("default" not in command for command in tools.calls)


def test_failed_pin_install_stops_setup_with_an_actionable_error(tools, monkeypatch):
    monkeypatch.setattr(neteq.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    with pytest.raises(SystemExit, match="rustup toolchain install"):
        neteq.ensure_neteq_tools()


def test_unavailable_linker_after_install_stops_before_rust(tools, monkeypatch):
    monkeypatch.setattr(neteq, "_has_msvc", lambda: False)
    with pytest.raises(SystemExit, match="Desktop development with C[+][+]"):
        neteq.ensure_neteq_tools()
    assert tools.calls == []


@pytest.mark.parametrize("with_neteq", [True, False])
def test_neteq_is_prepared_only_when_requested(monkeypatch, with_neteq):
    checked = []
    monkeypatch.setattr(setup.sys, "platform", "win32")
    monkeypatch.setattr(setup, "_path_from_registry", lambda: "refreshed")
    monkeypatch.setattr(setup, "_has_tool", lambda *a: True)
    monkeypatch.setattr(neteq, "ensure_neteq_tools", lambda: checked.append(True))
    monkeypatch.setenv("PATH", "original")
    setup.ensure_build_tools(with_neteq=with_neteq)
    assert checked == ([True] if with_neteq else [])
    assert setup.os.environ["PATH"] == "refreshed"


@pytest.mark.parametrize("argv, expected", [
    (["--with-neteq"], True), (["--force"], False), (["--force", "--with-neteq"], True)])
def test_cli_consumes_the_with_neteq_flag(monkeypatch, argv, expected):
    from winzapp_tools import cli
    seen = {}
    monkeypatch.setattr(cli.sys, "argv", ["setup-neonize", *argv])
    monkeypatch.setattr(setup, "ensure_build_tools",
                        lambda with_neteq=False: seen.update(neteq=with_neteq))
    monkeypatch.setattr(cli, "_exec", lambda command, cwd: seen.update(command=command))
    cli.setup_neonize()
    assert seen["neteq"] is expected
    assert "--with-neteq" not in seen["command"]
    assert seen["command"][2:] == [a for a in argv if a != "--with-neteq"]


@pytest.mark.parametrize("returncode", [1, 0])
def test_installer_failure_is_reported(monkeypatch, returncode):
    monkeypatch.setattr(neteq.shutil, "which", lambda *a, **k: "winget.exe")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(neteq.subprocess, "run", run)
    if returncode:
        with pytest.raises(SystemExit, match="exit 1"):
            neteq._install("Rustlang.Rustup", "path")
    else:
        neteq._install("Rustlang.Rustup", "path")
    assert commands[0][1:5] == ["install", "--id", "Rustlang.Rustup", "--exact"]


def test_missing_winget_reports_the_package_to_install(monkeypatch):
    monkeypatch.setattr(neteq.shutil, "which", lambda *a, **k: None)
    with pytest.raises(SystemExit, match="Install Rustlang.Rustup"):
        neteq._install("Rustlang.Rustup", "path")


@pytest.mark.parametrize("installed", [True, False])
def test_msvc_probe_requires_the_cpp_component(monkeypatch, installed):
    monkeypatch.setattr(neteq.Path, "is_file", lambda self: True)
    monkeypatch.setattr(neteq, "_has_windows_sdk", lambda: True)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="C:/VS\n" if installed else "")

    monkeypatch.setattr(neteq.subprocess, "run", run)
    assert neteq._has_msvc() is installed
    assert "Microsoft.VisualStudio.Component.VC.Tools.x86.x64" in commands[0]


def test_missing_vswhere_means_no_detected_cpp_tools(monkeypatch):
    monkeypatch.setattr(neteq.Path, "is_file", lambda self: False)
    monkeypatch.setattr(neteq.subprocess, "run", lambda *a, **k: pytest.fail("no vswhere"))
    assert not neteq._has_msvc()


def test_repair_selects_only_build_tools_but_readiness_accepts_any_edition(monkeypatch):
    monkeypatch.setattr(neteq.Path, "is_file", lambda self: True)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="C:/VS\n")

    monkeypatch.setattr(neteq.subprocess, "run", run)
    assert neteq._msvc_installation() == "C:/VS"
    assert neteq._msvc_installation(require_cpp=True) == "C:/VS"
    assert commands[0][1:3] == ["-products", "Microsoft.VisualStudio.Product.BuildTools"]
    assert commands[1][1:3] == ["-products", "*"]


def test_cpp_tools_without_a_windows_sdk_are_not_ready(monkeypatch):
    monkeypatch.setattr(neteq, "_msvc_installation", lambda **k: "C:/VS")
    monkeypatch.setattr(neteq, "_has_windows_sdk", lambda: False)
    assert not neteq._has_msvc()


def test_existing_visual_studio_is_modified_instead_of_reinstalled(monkeypatch):
    monkeypatch.setattr(neteq, "_msvc_installation", lambda **k: "C:/VS")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=3010)

    monkeypatch.setattr(neteq.subprocess, "run", run)
    monkeypatch.setattr(neteq, "_install", lambda *a: pytest.fail("must modify existing VS"))
    neteq._prepare_msvc("path")
    assert commands[0][1:4] == ["modify", "--installPath", "C:/VS"]
    assert "Microsoft.VisualStudio.Workload.VCTools" in commands[0]
    assert "--wait" not in commands[0]


def test_sdk_libraries_must_share_one_version(monkeypatch, tmp_path):
    monkeypatch.setattr(neteq, "_sdk_root", lambda: tmp_path)
    kernel = tmp_path / "Lib/10.0.1/um/x64/kernel32.lib"
    ucrt = tmp_path / "Lib/10.0.2/ucrt/x64/ucrt.lib"
    for library in (kernel, ucrt):
        library.parent.mkdir(parents=True)
        library.touch()
    assert not neteq._has_windows_sdk()
    matching_ucrt = tmp_path / "Lib/10.0.1/ucrt/x64/ucrt.lib"
    matching_ucrt.parent.mkdir(parents=True)
    matching_ucrt.touch()
    assert neteq._has_windows_sdk()
