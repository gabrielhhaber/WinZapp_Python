"""Regression tests for media transfer progress wiring."""

import ast
from pathlib import Path
from tests.god_modules import conversations_method_source

ROOT = Path(__file__).resolve().parents[1]


def _method_source(path: Path, class_name: str, method_name: str) -> str:
    if class_name == "ConversationsPanel":
        return conversations_method_source(method_name)
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    lines = source.splitlines()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method_name:
                    return "\n".join(lines[item.lineno - 1:item.end_lineno])
    raise AssertionError(f"{class_name}.{method_name} not found in {path}")


def test_selection_cleanup_hides_previous_messages_transfer_gauge():
    src = _method_source(
        ROOT / "client" / "ui" / "conversations.py",
        "ConversationsPanel",
        "_hide_all_media_controls",
    )
    assert "self._hide_media_transfer_gauge()" in src


def test_leaving_conversation_explicitly_hides_transfer_gauge():
    path = ROOT / "client" / "ui" / "conversations.py"
    navigate = _method_source(path, "ConversationsPanel", "navigate_to_conversation")
    close = _method_source(path, "ConversationsPanel", "_close_conversation_core")
    assert "self._hide_media_transfer_gauge()" in navigate
    assert "self._hide_media_transfer_gauge()" in close


def test_attachment_http_progress_is_scoped_to_its_upload_id():
    src = _method_source(
        ROOT / "client" / "ui" / "conversations.py",
        "ConversationsPanel",
        "_on_send_attachment",
    )
    assert "CoalescedCallback(wx.CallAfter, self.update_media_upload_progress)" in src
    assert "notify(local_id, progress)" in src
    assert "wx.CallAfter(self._update_media_transfer_gauge, progress)" not in src


def test_wpp_bridge_tracks_the_upload_lifecycle_event_used_by_wa_js():
    src = (
        ROOT / "client" / "api_patches" / "src" / "controller" / "messageController.ts"
    ).read_text(encoding="utf-8")
    assert "change:mediaData.mediaStage" in src
    # Keep the numeric field listener too: some WhatsApp builds expose a real
    # fractional progressiveStage alongside the lifecycle event.
    assert "change:mediaData.progressiveStage" in src
