"""Guard the uv-based local build path against a return to a hardcoded venv."""

from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_project_exposes_the_developer_shortcuts():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = metadata["project"]["scripts"]

    assert scripts["winzapp"] == "winzapp_tools.cli:app"
    assert scripts["setup-api"] == "winzapp_tools.cli:setup_api"
    assert scripts["build-onefile"] == "winzapp_tools.cli:build_onefile"
    assert scripts["build-installer"] == "winzapp_tools.cli:build_installer"


def test_build_uses_the_active_uv_environment_and_bootstraps_runtime_assets():
    source = (ROOT / "build.py").read_text(encoding="utf-8")

    assert "PYTHON_CMD      = sys.executable" in source
    assert "-m\", \"PyInstaller" in source
    assert "def ensure_build_assets():" in source
    assert "_download_portable_node()" in source
    assert "ensure_build_assets()" in source
    assert "VENV_DIR" not in source
