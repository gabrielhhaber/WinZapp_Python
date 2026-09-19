from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_successful_status_api_reconciles_own_cache():
    source = (ROOT / "client" / "status_panel.py").read_text(encoding="utf-8")
    assert 'api_ok = getattr(self, "_last_status_api_ok", False)' in source
    assert "if api_ok:" in source
    assert "self._reconcile_my_status_cache(" in source
    assert "authoritative_empty=my_status_ready" in source
    assert "mw.remove_failed_status_update(message_id, refresh=False)" in source


def test_status_parser_drops_tombstones_and_reactions():
    source = (ROOT / "client" / "status_panel.py").read_text(encoding="utf-8")
    for marker in (
        '"protocolMessage"',
        '"reactionMessage"',
        '"isRevoked"',
        '"isDeleted"',
        '"isExpired"',
    ):
        assert marker in source


def test_reconciled_status_is_removed_from_memory_and_sqlite():
    main_source = (ROOT / "client" / "main.py").read_text(encoding="utf-8")
    bridge_source = (ROOT / "client" / "core" / "database_bridge.py").read_text(
        encoding="utf-8"
    )
    assert "def remove_failed_status_update(" in main_source
    assert "self.db.delete_status_update(message_id)" in main_source
    assert "def delete_status_update(" in bridge_source



def test_status_api_exposes_own_status_readiness_marker():
    source = (
        ROOT / "client" / "api_patches" / "src" / "controller" / "statusController.ts"
    ).read_text(encoding="utf-8")
    assert "myStatusReady: false" in source
    assert "out.myStatusReady = true" in source


def test_status_panel_only_accepts_empty_snapshot_when_ready():
    source = (ROOT / "client" / "status_panel.py").read_text(encoding="utf-8")
    assert 'my_status_ready = getattr(self, "_last_my_status_ready", False)' in source
    assert "authoritative_empty=my_status_ready" in source
