from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_existing_outdated_node_is_upgraded_before_wppconnect_starts():
    source = (ROOT / "client/main.py").read_text(encoding="utf-8")
    gate = source[source.index("node_needs_download =") :]
    gate = gate[: gate.index("# Detect and clean legacy node_modules")]

    assert "NODE_VERSION" in gate
    assert "Version(installed_node_version) < Version(NODE_VERSION)" in gate
    assert '[node_exe, npm_cli, "install", "--help"]' in gate
    assert "npm_probe.returncode != 0" in gate
    assert "NodeDownloadDialog" in gate


def test_node_upgrade_replaces_instead_of_overlaying_the_npm_tree():
    source = (ROOT / "client/ui/dialogs/node_download.py").read_text(
        encoding="utf-8"
    )
    extract = source[source.index("def _extract_node") : source.index("def _run_download")]

    assert 'tempfile.mkdtemp(prefix=".node-staging-"' in extract
    assert "os.replace(node_dir, backup_dir)" in extract
    assert "os.replace(staging_dir, node_dir)" in extract


def test_setup_rejects_a_versioned_but_broken_portable_npm():
    source = (ROOT / "setup_api.py").read_text(encoding="utf-8")

    assert '[node_bin, npm_bin, "install", "--help"]' in source
    assert "Portable npm is unhealthy" in source
