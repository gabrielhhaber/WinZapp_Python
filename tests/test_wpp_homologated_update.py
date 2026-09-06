"""client/wpp_minimum_version.txt is the single source of the WPPConnect tag.

Every install path has to reach the same WPPConnect Server release: the dev/CI
one (setup_api.py), the end-user one (ApiSetupDialog), and the in-app update
prompt (WppUpdateChecker). They used to each carry their own idea of it, so a
user could be pulled onto a release WinZapp's patch set had never been built
against — and the CI build then wrote the file from whatever client/api/
happened to hold, which made the whole thing agree with itself by accident.

The value is deliberately never written out a second time here. Repeating it
turns moving the pin into an edit of two files that must agree, and the test
can only ever catch the edit somebody already remembered to make.
"""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOMOLOGATED = (
    (ROOT / "client/wpp_minimum_version.txt").read_text(encoding="utf-8").strip()
)


def test_homologated_server_version_is_shipped():
    assert re.fullmatch(r"\d+\.\d+\.\d+", HOMOLOGATED), (
        f"client/wpp_minimum_version.txt holds {HOMOLOGATED!r}; it must be a "
        f"plain WPPConnect Server release version, e.g. 2.10.16 — setup_api.py "
        f"turns it straight into the git tag v<version>."
    )


def test_all_install_paths_prefer_the_homologated_version():
    for path in (
        ROOT / "setup_api.py",
        ROOT / "client/ui/dialogs/api_setup.py",
        ROOT / "client/updater.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert "wpp_minimum_version.txt" in source
        assert "homologated" in source


def test_the_committed_file_is_not_generated_by_the_build():
    """It used to be .gitignored and overwritten by build-windows.yml from
    client/api/package.json, which made it an output of the build rather than
    an input to it — self-consistent, and unable to disagree with anything.
    The workflow now verifies it instead."""
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "wpp_minimum_version.txt" not in gitignore

    workflow = (ROOT / ".github/workflows/build-windows.yml").read_text(
        encoding="utf-8"
    )
    assert r'Set-Content -Path "client\wpp_minimum_version.txt"' not in workflow
    assert "wpp_minimum_version.txt" in workflow
