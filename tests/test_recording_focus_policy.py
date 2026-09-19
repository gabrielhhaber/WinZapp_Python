import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _method_source(relative: str, class_name: str, method_name: str) -> str:
    source = (ROOT / relative).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name == method_name:
                    return ast.get_source_segment(source, member) or ""
    raise AssertionError(f"{class_name}.{method_name} not found in {relative}")


@pytest.mark.parametrize(
    ("relative", "class_name"),
    [
        ("client/ui/conversations.py", "ConversationsPanel"),
        ("client/status_panel.py", "StatusPanel"),
    ],
)
def test_silent_recording_does_not_manufacture_send_focus_event(relative, class_name):
    method = _method_source(relative, class_name, "_focus_recording_button_silently")

    assert "if self._voice_recording_focus_suppression_enabled():" in method
    assert "return False" in method
    assert method.count("button.SetFocus()") == 1
    assert method.index("return False") < method.index("button.SetFocus()")
    assert "cloak_focus_announcement" not in method


def test_panel_focus_fallback_uses_nvda_silent_pane_role():
    source = (ROOT / "client/core/focus_cloak.py").read_text(encoding="utf-8")

    assert "class PanelFocusCloakAccessible" in source
    assert "return (wx.ACC_OK, wx.ROLE_SYSTEM_PANE)" in source
    assert 'return (wx.ACC_OK, "")' in source
    assert "def cloak_panel_focus_fallback" in source


def test_conversation_recording_cloaks_panel_before_hiding_focused_controls():
    method = _method_source(
        "client/ui/conversations.py", "ConversationsPanel", "_start_voice_recording"
    )

    assert method.count("cloak_panel_focus_fallback(") >= 2
    first_cloak = method.index("cloak_panel_focus_fallback(")
    first_record_hide = method.index("self.record_voice_message_btn.Hide()")
    assert first_cloak < first_record_hide
    assert "self.message_field" in method
    assert "recording_controls_to_hide" in method


def test_status_recording_cloaks_panel_before_hiding_start_button():
    method = _method_source(
        "client/status_panel.py", "StatusPanel", "_start_voice_recording"
    )

    cloak = method.index("cloak_panel_focus_fallback(")
    hide = method.index("self._voice_start_btn.Hide()")
    assert cloak < hide
    assert "self._voice_post_panel" in method


def test_silent_conversation_recording_preserves_message_field_focus():
    method = _method_source(
        "client/ui/conversations.py", "ConversationsPanel", "_start_voice_recording"
    )

    assert "keep_message_field_focused" in method
    assert "if keep_message_field_focused:" in method
    assert "else:\n                self.message_field.Hide()" in method

    silent_block = method.split("if keep_message_field_focused:", 1)[1].split(
        "else:\n                self.message_field.Hide()", 1
    )[0]
    assert "self.message_field" not in silent_block.split(
        "recording_controls_to_hide = [", 1
    )[1].split("]", 1)[0]
