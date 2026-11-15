"""Alt+Shift+H opens the writing help, and the person can rebind it."""
from pathlib import Path
from types import SimpleNamespace as NS

from core.keyboard_shortcuts import ALT, SHIFT, default_binding, scopes_overlap
from core.shortcut_catalog import SHORTCUTS
from ui.conversation_panel.ai_writing import AIWritingMixin

ROOT = Path(__file__).resolve().parents[1]


def _own():
    entries = [s for s in SHORTCUTS if s.command == "ID_WRITING_HELP"]
    assert len(entries) == 1
    return entries[0]


def test_it_is_alt_shift_h_in_the_conversation():
    own = _own()
    assert own.id == "messages.ID_WRITING_HELP"
    assert default_binding(own, lambda key: key) == (ALT | SHIFT, ord("H"))


def test_no_shortcut_of_an_overlapping_scope_uses_it():
    own = _own()
    taken = default_binding(own, lambda key: key)
    clashes = [s.id for s in SHORTCUTS
               if s.id != own.id and scopes_overlap(own.scope, s.scope)
               and default_binding(s, lambda key: key) == taken]
    assert clashes == []


def test_the_label_names_the_key_in_every_locale():
    import polib
    for po_path in sorted((ROOT / "translations").glob("*/LC_MESSAGES/winzapp.po")):
        entries = {e.msgctxt: e for e in polib.pofile(str(po_path))}
        assert entries["shortcut_writing_help_label"].msgstr.startswith("Alt+Shift+H"), po_path


def test_the_help_window_lists_it():
    source = (ROOT / "client" / "ui" / "dialogs" / "shortcuts_dialog.py").read_text(encoding="utf-8")
    assert 'i18n.t("shortcut_writing_help_label")' in source


def test_the_button_and_the_menu_announce_the_configured_shortcut():
    accessible = (ROOT / "client" / "ui" / "accessible.py").read_text(encoding="utf-8")
    assert "class AccessibleWritingHelp" in accessible
    assert "'messages.ID_WRITING_HELP'" in accessible and '"Alt+Shift+H"' in accessible
    writing = (ROOT / "client" / "ui" / "conversation_panel" / "ai_writing.py").read_text(encoding="utf-8")
    assert "button.SetAccessible(AccessibleWritingHelp())" in writing
    composer = (ROOT / "client" / "ui" / "conversation_panel" / "composer.py").read_text(encoding="utf-8")
    assert "menu.Append(wx.ID_ANY, self._writing_menu_label())" in composer


def test_the_accelerator_is_registered_and_bound():
    source = (ROOT / "client" / "ui" / "conversation_panel" / "accelerators.py").read_text(encoding="utf-8")
    assert 'ord("H"),          self.ID_WRITING_HELP)' in source
    assert "self._on_writing_help_shortcut,      id=self.ID_WRITING_HELP)" in source


def _stub(offered):
    calls = []
    stub = NS(_writing_help_offered=lambda: offered,
              _on_writing_help=lambda: calls.append("opened"))
    return stub, calls


def test_the_shortcut_opens_the_window_on_a_draft_that_can_be_helped():
    stub, calls = _stub(True)
    AIWritingMixin._on_writing_help_shortcut(stub)
    assert calls == ["opened"]


def test_the_shortcut_does_nothing_without_a_draft():
    stub, calls = _stub(False)
    AIWritingMixin._on_writing_help_shortcut(stub)
    assert calls == []


def test_utf8_bytes_survives_a_lone_surrogate():
    """#797: wx can return half of an emoji; the strict encode raised."""
    from core.ai_media.config import utf8_bytes
    assert utf8_bytes("ola") == b"ola"
    assert utf8_bytes("a\ud83d") == "a�".encode("utf-8")
    assert utf8_bytes("😀") == "\U0001F600".encode("utf-8")


def test_writing_help_offered_with_a_lone_surrogate_in_the_draft():
    field = NS(GetValue=lambda: "oi \ud83d", IsEnabled=lambda: True, IsEditable=lambda: True)
    stub = NS(message_field=field)
    assert AIWritingMixin._writing_help_offered(stub) is True
