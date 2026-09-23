"""core.utils.link_preview_text() — the single rendering both surfaces share.

The message list (ConversationsPanel._get_message_content) and the toast
(core.notification_manager.format_notification_body) each used to carry their
own character-for-character copy of this switch, and for a long time the
toast's copy simply did not exist: a link with a preview read as its title in
the list and as the raw URL in the notification for the same message. The two
copies are gone; what is left is this function plus an equivalence test, so a
future edit to one surface can't quietly diverge from the other.

tests/test_link_preview_message_content.py and
tests/test_notification_content.py still cover each surface end to end — this
file covers the shared helper itself and the fact that they agree.
"""

import ast
from pathlib import Path

from core.notification_manager import format_notification_body
from core.utils import link_preview_text
from ui.conversations import ConversationsPanel


ROOT = Path(__file__).resolve().parents[1]


class _FakeI18n:
    def t(self, key):
        return f"[{key}]"


class _MainWindow:
    def __init__(self, show_link_previews=True):
        self.i18n = _FakeI18n()
        self.settings = {
            "user_interface": {"show_link_previews": show_link_previews}
        }
        self._lid_to_phone = {}

    @staticmethod
    def _is_self_jid(jid):
        return False


class _PanelStub:
    _get_message_content = ConversationsPanel._get_message_content
    _resolve_mentions_in_text = ConversationsPanel._resolve_mentions_in_text
    _message_mentioned_jids = staticmethod(ConversationsPanel._message_mentioned_jids)

    def __init__(self, main_window):
        self.main_window = main_window


def _msg(ext):
    return {
        "messageType": "extendedTextMessage",
        "message": {"extendedTextMessage": ext},
        "key": {"id": "ABC"},
    }


class TestLinkPreviewText:
    def test_title_and_description_precede_the_text(self):
        ext = {
            "text": "https://example.com/a",
            "title": "Título",
            "description": "Uma descrição",
        }
        assert link_preview_text(ext, ext["text"]) == (
            "Título. Uma descrição. https://example.com/a"
        )

    def test_title_alone_still_renders(self):
        ext = {"text": "https://example.com", "title": "Título"}
        assert link_preview_text(ext, ext["text"]) == "Título. https://example.com"

    def test_description_alone_still_renders(self):
        ext = {"text": "https://example.com", "description": "Descrição"}
        assert link_preview_text(ext, ext["text"]) == "Descrição. https://example.com"

    def test_whitespace_only_fields_count_as_absent(self):
        ext = {"text": "https://example.com", "title": "   ", "description": "\n"}
        assert link_preview_text(ext, ext["text"]) == "https://example.com"

    def test_no_preview_fields_leaves_the_text_untouched(self):
        ext = {"text": "https://example.com"}
        assert link_preview_text(ext, ext["text"]) == "https://example.com"

    def test_empty_text_yields_just_the_preview_with_no_trailing_separator(self):
        ext = {"text": "", "title": "Título", "description": "Descrição"}
        assert link_preview_text(ext, "") == "Título. Descrição"

    def test_disabled_setting_suppresses_the_preview(self):
        ext = {"text": "https://example.com", "title": "Título"}
        mw = _MainWindow(show_link_previews=False)
        assert link_preview_text(ext, ext["text"], mw) == "https://example.com"

    def test_missing_main_window_defaults_to_showing_the_preview(self):
        """Callers with no window in reach must get the same default the
        setting itself has (on), not an AttributeError."""
        ext = {"text": "https://example.com", "title": "Título"}
        assert link_preview_text(ext, ext["text"], None) == "Título. https://example.com"

    def test_a_non_dict_payload_is_returned_unchanged(self):
        assert link_preview_text(None, "texto") == "texto"


class TestBothSurfacesAgree:
    """The regression the extraction exists to prevent: the list and the
    toast rendering the same message differently."""

    PAYLOADS = (
        {"text": "https://example.com/a", "title": "T", "description": "D"},
        {"text": "https://example.com/b", "title": "Só título"},
        {"text": "https://example.com/c", "description": "Só descrição"},
        {"text": "https://example.com/d"},
        {"text": "", "title": "T", "description": "D"},
    )

    def test_list_and_toast_render_identically(self):
        for ext in self.PAYLOADS:
            mw = _MainWindow()
            from_list = _PanelStub(mw)._get_message_content(_msg(ext))
            from_toast = format_notification_body(_msg(ext), mw, _FakeI18n())
            assert from_list == from_toast, ext

    def test_they_agree_with_the_setting_off_too(self):
        for ext in self.PAYLOADS:
            mw = _MainWindow(show_link_previews=False)
            from_list = _PanelStub(mw)._get_message_content(_msg(ext))
            from_toast = format_notification_body(_msg(ext), mw, _FakeI18n())
            assert from_list == from_toast, ext


def _calls_link_preview_text(rel_path):
    src = (ROOT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(src, filename=rel_path)
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "link_preview_text"
        for node in ast.walk(tree)
    )


def test_both_surfaces_go_through_the_shared_helper():
    """Structural guard behind the equivalence tests above: those compare
    outputs, so they'd still pass if someone re-inlined an identical copy —
    and an identical copy is exactly what drifts on the next edit (same
    reasoning as status_panel's _status_content_label()). This asserts the
    call itself is still there."""
    for rel_path in ("client/ui/conversations.py", "client/core/notification_manager.py"):
        assert _calls_link_preview_text(rel_path), (
            f"{rel_path} no longer calls core.utils.link_preview_text() — a "
            f"private copy of the link-preview rendering is back."
        )
