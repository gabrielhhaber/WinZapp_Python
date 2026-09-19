"""Tests for client-only release/update package selection."""

import updater


def _assets(*names):
    return [
        {"name": n, "browser_download_url": f"https://example.invalid/{n}"}
        for n in names
    ]


def test_client_only_zip_asset_is_selected_explicitly():
    url = updater.find_zip_asset(
        _assets("WinZapp.zip", "WinZappClient.zip"), client_only=True
    )
    assert url.endswith("/WinZappClient.zip")


def test_client_only_zip_never_falls_back_to_full_package():
    assert updater.find_zip_asset(_assets("WinZapp.zip"), client_only=True) == ""


def test_full_package_never_falls_back_to_client_only_package():
    assert updater.find_zip_asset(_assets("WinZappClient.zip"), client_only=False) == ""


def test_client_only_distribution_marker_is_detected(tmp_path):
    (tmp_path / "distribution.json").write_text(
        '{"variant":"client-only"}\n', encoding="utf-8"
    )
    assert updater.is_client_only_install(str(tmp_path)) is True


def test_missing_distribution_marker_defaults_to_full(tmp_path):
    assert updater.is_client_only_install(str(tmp_path)) is False



def test_client_only_build_excludes_local_api_directories(monkeypatch):
    """The client ZIP must exclude API trees even if PyInstaller nests them."""
    import importlib.util
    import sys
    from pathlib import Path

    build_path = Path(__file__).resolve().parents[1] / "build.py"
    spec = importlib.util.spec_from_file_location("winzapp_build_for_test", build_path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(sys, "argv", [str(build_path)])
    spec.loader.exec_module(module)

    excluded = [
        "api/package.json",
        "api_patches/src/index.ts",
        "_internal/api/dist/server.js",
        "_internal/api_patches/src/index.ts",
        "_internal/client/api/dist/server.js",
        "_internal/client/api_patches/src/index.ts",
    ]
    for path in excluded:
        assert module._client_only_path_is_excluded(path) is True

    allowed = [
        "_internal/some_package/api_helpers.py",
        "lib/ffmpeg.exe",
        "sounds/incoming_call.ogg",
        "languages/pt-BR.json",
    ]
    for path in allowed:
        assert module._client_only_path_is_excluded(path) is False


def test_client_only_zip_validator_rejects_local_api_leaks(monkeypatch, tmp_path):
    import importlib.util
    import sys
    import zipfile
    from pathlib import Path

    build_path = Path(__file__).resolve().parents[1] / "build.py"
    spec = importlib.util.spec_from_file_location("winzapp_build_validator_test", build_path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(sys, "argv", [str(build_path)])
    spec.loader.exec_module(module)

    archive = tmp_path / "WinZappClient.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("WinZapp/WinZapp.exe", b"exe")
        zf.writestr("WinZapp/api/package.json", b"{}")

    try:
        module._assert_client_only_zip_has_no_local_api(str(archive))
    except RuntimeError as exc:
        assert "api/package.json" in str(exc)
    else:
        raise AssertionError("validator accepted a client ZIP containing api/")
