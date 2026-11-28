"""Offline unit tests for quick Applications context-menu actions and Enter-to-replace."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from core.ai_media import writing
from ui.conversation_panel.ai_actions import AIActionsMixin
from ui.conversation_panel.ai_writing import AIWritingMixin

ROOT = Path(__file__).resolve().parents[1]


def _method(relpath, cls_name, method_name, **env):
    tree = ast.parse((ROOT / relpath).read_text(encoding="utf-8"))
    owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name)
    fn = next(n for n in owner.body if isinstance(n, ast.FunctionDef) and n.name == method_name)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), relpath, "exec"), env)
    return env[method_name]


class _FakeMenuItem:
    def __init__(self, item_id, label, submenu=None):
        self.id = item_id
        self.label = label
        self.submenu = submenu


class _FakeMenu:
    def __init__(self):
        self.items = []
        self._next_id = 100

    def Append(self, _id, label):
        self._next_id += 1
        item = _FakeMenuItem(self._next_id, label)
        self.items.append(item)
        return item

    def AppendSubMenu(self, submenu, label):
        self._next_id += 1
        item = _FakeMenuItem(self._next_id, label, submenu=submenu)
        self.items.append(item)
        return item


class _FakePanel:
    def __init__(self, *, text="hello", enabled=True, editable=True, shown=True,
                 conversation="chat@s.whatsapp.net", providers=True, recent="Hi there"):
        self.conversation = {"remoteJid": conversation} if conversation else None
        self.message_field = NS(
            GetValue=lambda: text,
            IsEnabled=lambda: enabled,
            IsEditable=lambda: editable,
            IsShown=lambda: shown,
        )
        self._providers = providers
        self._recent = recent
        self.main_window = NS(i18n=NS(t=lambda k: k), settings={})

    def _writing_has_providers(self):
        return self._providers

    def _writing_recent_messages(self):
        return self._recent


def test_writing_quick_actions_adapt_to_draft_providers_and_history():
    qa = AIWritingMixin._writing_quick_actions

    p_full = _FakePanel(text="hello", providers=True, recent="Ana: hi")
    assert qa(p_full) == [
        ("check", "writing_action_check"),
        ("professional", "writing_action_professional"),
        (writing.REPLY, "writing_action_reply"),
        ("translate", "writing_action_translate"),
    ]

    p_no_recent = _FakePanel(text="hello", providers=True, recent="")
    assert qa(p_no_recent) == [
        ("check", "writing_action_check"),
        ("professional", "writing_action_professional"),
        ("translate", "writing_action_translate"),
    ]

    p_no_ai = _FakePanel(text="hello", providers=False, recent="Ana: hi")
    assert qa(p_no_ai) == [("translate", "writing_action_translate")]

    p_empty_with_reply = _FakePanel(text="   ", providers=True, recent="Ana: hi")
    assert qa(p_empty_with_reply) == [(writing.REPLY, "writing_action_reply")]

    p_empty_no_ai = _FakePanel(text="   ", providers=False, recent="Ana: hi")
    assert qa(p_empty_no_ai) == []

    p_readonly = _FakePanel(text="hello", editable=False, providers=True, recent="Ana: hi")
    assert qa(p_readonly) == []


def test_writing_dialog_auto_run_vs_explicit_choice():
    calls = []
    hook = _method(
        "client/ui/dialogs/ai_writing_dialog.py",
        "WritingHelpDialog",
        "_on_open",
        wx=NS(CallAfter=lambda fn: calls.append(("after", fn))),
    )
    regen = object()

    d_manual = NS(
        _auto_run=False,
        status=NS(ChangeValue=lambda t: calls.append(("status", t))),
        action=NS(SetFocus=lambda: calls.append("action-focus")),
        result=NS(SetFocus=lambda: calls.append("result-focus")),
        i18n=NS(t=lambda k: k),
        _regenerate=regen,
    )
    hook(d_manual)
    assert calls == [("status", "writing_choose_before_run"), "action-focus"]

    calls.clear()
    d_quick = NS(
        _auto_run=True,
        status=NS(ChangeValue=lambda t: calls.append(("status", t))),
        action=NS(SetFocus=lambda: calls.append("action-focus")),
        result=NS(SetFocus=lambda: calls.append("result-focus")),
        i18n=NS(t=lambda k: k),
        _regenerate=regen,
    )
    hook(d_quick)
    assert calls == ["result-focus", ("after", regen)]


def test_enter_in_result_replaces_draft_when_enabled():
    focused = [None]
    wx_stub = NS(
        WXK_ESCAPE=27,
        WXK_RETURN=13,
        WXK_NUMPAD_ENTER=370,
        Window=NS(FindFocus=lambda: focused[0]),
    )
    key_fn = _method("client/ui/dialogs/ai_text_result_dialog.py", "TextAIResultDialog", "_key", wx=wx_stub)

    calls = []
    result_ctrl = object()
    other_ctrl = object()

    def make_event(code, ctrl=False, alt=False, shift=False):
        return NS(
            GetKeyCode=lambda: code,
            ControlDown=lambda: ctrl,
            AltDown=lambda: alt,
            ShiftDown=lambda: shift,
            Skip=lambda: calls.append("skip"),
        )

    dlg = NS(
        result=result_ctrl,
        replace=NS(IsEnabled=lambda: True),
        _replace=lambda: calls.append("replace"),
        _close=lambda: calls.append("close"),
    )

    focused[0] = result_ctrl
    key_fn(dlg, make_event(13))
    key_fn(dlg, make_event(370))
    assert calls == ["replace", "replace"]

    calls.clear()
    key_fn(dlg, make_event(13, shift=True))
    focused[0] = other_ctrl
    key_fn(dlg, make_event(13))
    dlg_disabled = NS(result=result_ctrl, replace=NS(IsEnabled=lambda: False),
                      _replace=lambda: calls.append("replace"), _close=lambda: calls.append("close"))
    focused[0] = result_ctrl
    key_fn(dlg_disabled, make_event(13))
    assert calls == ["skip", "skip", "skip"]

    calls.clear()
    key_fn(dlg, make_event(27))
    assert calls == ["close"]


@pytest.mark.parametrize(
    ("offered_kind", "ocr_kind", "expected_labels", "expected_sublabels"),
    [
        ("image", "image", ["ai_read_text_offline", "ai_mode_label"], ["ai_mode_text_only", "ai_mode_receipt", "ai_mode_table"]),
        ("video", "video", ["ai_read_text_offline", "ai_full_video", "ai_mode_label"], ["ai_mode_text_only", "ai_mode_receipt", "ai_mode_table"]),
        ("pdf", None, ["ai_mode_label"], ["ai_mode_text_only", "ai_mode_receipt", "ai_mode_table"]),
        ("audio", None, [], []),
        (None, "image", [], []),
    ],
)
def test_ai_quick_menu_items_by_media_kind(monkeypatch, tmp_path, offered_kind, ocr_kind, expected_labels, expected_sublabels):
    import ui.conversation_panel.ai_actions as mod

    monkeypatch.setattr(mod, "global_dir", lambda: str(tmp_path))
    monkeypatch.setattr(mod, "CredentialStore", lambda _: NS(saved=lambda: ["gemini"]))
    monkeypatch.setattr(mod.ai_config, "offered_kind", lambda msg, cfg, saved: offered_kind)
    monkeypatch.setattr(mod, "offline_ocr_kind", lambda msg: ocr_kind)
    monkeypatch.setattr(mod.wx, "Menu", _FakeMenu)

    menu = _FakeMenu()
    bound = []
    panel = NS(Bind=lambda evt, fn, item: bound.append((item.label, fn)))
    i18n = NS(t=lambda k: k)
    AIActionsMixin._append_ai_quick_menu_items(panel, menu, {"messageType": "imageMessage"}, i18n, config={})

    assert [item.label for item in menu.items] == expected_labels
    sub = next((item.submenu for item in menu.items if item.submenu is not None), None)
    assert ([it.label for it in sub.items] if sub else []) == expected_sublabels


def test_composer_context_menu_wires_dictation_and_quick_writing():
    tree = ast.parse((ROOT / "client/ui/conversation_panel/composer.py").read_text(encoding="utf-8"))
    owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ComposerMixin")
    fn = next(n for n in owner.body if isinstance(n, ast.FunctionDef) and n.name == "_on_message_field_context_menu")
    attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    strings = {n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert {"_dictation_menu_offered", "_writing_quick_actions", "_writing_help_offered"} <= attrs
    assert "writing_quick_menu" in strings
