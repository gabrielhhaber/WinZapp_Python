"""Regression guards for live switching of the messages-list control.

The ListCtrl/ListBox choice used to be persisted but only constructed at app
startup, which forced a restart warning. The ListBox-only item-count setting
was also always visible even while Classic mode was selected.
"""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETTINGS_SOURCE = (ROOT / "client" / "ui" / "dialogs" / "settings_dialog.py").read_text(
    encoding="utf-8"
)
CONVERSATIONS_SOURCE = (ROOT / "client" / "ui" / "conversations.py").read_text(
    encoding="utf-8"
)


def _method_source(source: str, class_name: str, method_name: str) -> str:
    tree = ast.parse(source)
    lines = source.splitlines()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method_name:
                    return "\n".join(lines[child.lineno - 1 : child.end_lineno])
    raise AssertionError(f"{class_name}.{method_name} not found")


def test_listbox_count_option_is_visible_only_for_listbox_mode():
    method = _method_source(
        SETTINGS_SOURCE, "SettingsDialog", "_sync_listbox_count_visibility"
    )
    assert "self._msg_list_mode_listbox_rb.GetValue()" in method
    assert "self._show_listbox_count_cb.Show(show)" in method


def test_both_message_list_radios_refresh_the_listbox_only_option_visibility():
    build = _method_source(SETTINGS_SOURCE, "SettingsDialog", "_build_ui")
    assert build.count("self._on_message_list_mode_toggle") >= 2


def test_apply_persists_then_switches_message_list_without_requesting_restart():
    apply_values = _method_source(SETTINGS_SOURCE, "SettingsDialog", "_apply_values")
    assert "self._restart_required = True" not in apply_values
    assert "cp.apply_message_list_mode(new_message_list_mode)" in apply_values
    assert apply_values.index("self.main_window.save_settings()") < apply_values.index(
        "cp.apply_message_list_mode(new_message_list_mode)"
    )


def test_live_switch_uses_persistent_controls_without_destroying_windows():
    method = _method_source(
        CONVERSATIONS_SOURCE, "ConversationsPanel", "apply_message_list_mode"
    )
    assert "self._message_list_controls[mode]" in method
    assert "self._message_list_mode = mode" in method
    assert "old_list.Hide()" in method
    assert "new_list.Show()" in method
    assert ".Destroy()" not in method
    assert "sizer.Replace" not in method
    assert "self._rerender_messages_list_rows()" in method


def test_both_message_list_controls_are_created_once_at_startup():
    init_ui = _method_source(CONVERSATIONS_SOURCE, "ConversationsPanel", "init_UI")
    assert '"classic": self._create_messages_list_control("classic")' in init_ui
    assert '"listbox": self._create_messages_list_control("listbox")' in init_ui
    assert "self.messages_list = self._message_list_controls[message_list_mode]" in init_ui
