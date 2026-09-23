"""Tests for ConversationsPanel._get_message_content()'s rendering of a link
preview (title/description WhatsApp itself generated — see
tests/test_link_preview_normalization.py for how they land on the
extendedTextMessage dict in the first place).

Gated by Settings > Interface do usuário's "show_link_previews" toggle
(default True, user_interface.show_link_previews)."""

from ui.conversations import ConversationsPanel


class _FakeI18n:
    def t(self, key):
        return f"[{key}]"


class _Stub:
    _get_message_content = ConversationsPanel._get_message_content
    _resolve_mentions_in_text = ConversationsPanel._resolve_mentions_in_text
    _message_mentioned_jids = staticmethod(ConversationsPanel._message_mentioned_jids)

    def __init__(self, show_link_previews=True):
        self.main_window = type("MW", (), {
            "i18n": _FakeI18n(),
            "settings": {"user_interface": {"show_link_previews": show_link_previews}},
        })()


def _msg(ext):
    return {
        "messageType": "extendedTextMessage",
        "message": {"extendedTextMessage": ext},
        "key": {"id": "ABC"},
    }


class TestLinkPreviewRendering:
    def test_title_and_description_are_prepended_before_the_text(self):
        stub = _Stub()
        msg = _msg({
            "text": "check this out https://example.com",
            "title": "Example Domain",
            "description": "Example site used for illustration",
        })

        text = stub._get_message_content(msg)

        assert text == (
            "Example Domain. Example site used for illustration. "
            "check this out https://example.com"
        )

    def test_title_only_still_renders(self):
        stub = _Stub()
        msg = _msg({"text": "https://example.com", "title": "Example Domain"})

        text = stub._get_message_content(msg)

        assert text == "Example Domain. https://example.com"

    def test_no_preview_fields_leaves_text_untouched(self):
        stub = _Stub()
        msg = _msg({"text": "just some text, no link"})

        text = stub._get_message_content(msg)

        assert text == "just some text, no link"

    def test_disabled_setting_suppresses_the_preview(self):
        stub = _Stub(show_link_previews=False)
        msg = _msg({
            "text": "check this out https://example.com",
            "title": "Example Domain",
            "description": "Example site used for illustration",
        })

        text = stub._get_message_content(msg)

        assert text == "check this out https://example.com"

    def test_empty_text_with_preview_shows_just_the_preview(self):
        stub = _Stub()
        msg = _msg({"text": "", "title": "Example Domain", "description": "desc"})

        text = stub._get_message_content(msg)

        assert text == "Example Domain. desc"
