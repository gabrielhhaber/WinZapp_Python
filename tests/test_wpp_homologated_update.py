from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_homologated_server_version_is_shipped():
    assert (ROOT / "client/wpp_minimum_version.txt").read_text(
        encoding="utf-8"
    ).strip() == "2.10.16"


def test_all_install_paths_prefer_the_homologated_version():
    for path in (
        ROOT / "setup_api.py",
        ROOT / "client/ui/dialogs/api_setup.py",
        ROOT / "client/updater.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert "wpp_minimum_version.txt" in source
        assert "homologated" in source
