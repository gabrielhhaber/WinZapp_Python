"""Tests for Settings > Interface do usuário > "Anunciar 'Encaminhada' no
início da mensagem" (user_interface.forwarded_prefix_enabled, default off).

Off keeps the long-standing behavior: a trailing ", Encaminhada" clause,
appended after the sender/body/time/status/edited — same position as
"Editada". On moves it to the very front of the row instead, ahead of the
sender (and even ahead of the star/pin markers), so a screen reader user
arrowing through a forwarded chat hears "Encaminhada" before anything else.

ConversationsPanel is a wx.Panel and cannot be instantiated without a running
wx.App, so _render_message_line() is exercised against a stub that binds the
real method plus _is_message_forwarded()/_is_system_event(), with every other
collaborator (sender label, status, timestamp, context) reduced to a fixed
value — same approach as tests/test_video_duration_unknown.py.
"""

from ui.conversations import ConversationsPanel


class _FakeI18n:
    _STRINGS = {
        "status_forwarded": "Encaminhada",
        "status_edited": "Editada",
        "selected_suffix": "selecionada",
    }

    def t(self, key):
        return self._STRINGS.get(key, f"[{key}]")


class _Stub:
    _render_message_line = ConversationsPanel._render_message_line
    _is_message_forwarded = ConversationsPanel._is_message_forwarded
    _is_system_event = staticmethod(ConversationsPanel._is_system_event)

    def __init__(self, forwarded_prefix_enabled=False):
        self.main_window = type("MW", (), {
            "settings": {"user_interface": {"forwarded_prefix_enabled": forwarded_prefix_enabled}},
            "i18n": _FakeI18n(),
        })()
        self._message_list_mode = "classic"
        self._media_upload_progress = {}
        self._upload_stages_seen = {}
        self.selected_messages = set()

    def _is_separator(self, msg):
        return False

    def _extract_timestamp(self, msg):
        return 0

    def _format_date(self, ts):
        return ""

    def _get_message_content(self, msg):
        return "corpo da mensagem"

    def _sender_label(self, msg):
        return "Fulano"

    def _map_status(self, msg):
        return ""

    def _get_context_info(self, msg):
        return None

    def _get_quoted_sender(self, ctx, msg):
        return ""

    def _reaction_counts(self, msg_id):
        return {}


def _forwarded_msg(**overrides):
    msg = {
        "key": {"id": "MSG1"},
        "messageType": "conversation",
        "message": {"conversation": "corpo da mensagem"},
        "contextInfo": {"isForwarded": True},
    }
    msg.update(overrides)
    return msg


class TestDefaultOffKeepsTheTrailingSuffix:
    def test_forwarded_clause_stays_at_the_end(self):
        stub = _Stub(forwarded_prefix_enabled=False)
        line = stub._render_message_line(_forwarded_msg())
        assert line == "Fulano: corpo da mensagem, Encaminhada"

    def test_a_non_forwarded_message_gets_no_clause_at_all(self):
        stub = _Stub(forwarded_prefix_enabled=False)
        msg = _forwarded_msg(contextInfo={})
        line = stub._render_message_line(msg)
        assert "Encaminhada" not in line


class TestEnabledMovesItToTheFront:
    def test_forwarded_clause_precedes_the_sender(self):
        stub = _Stub(forwarded_prefix_enabled=True)
        line = stub._render_message_line(_forwarded_msg())
        assert line == "Encaminhada, Fulano: corpo da mensagem"

    def test_the_suffix_is_not_also_appended(self):
        stub = _Stub(forwarded_prefix_enabled=True)
        line = stub._render_message_line(_forwarded_msg())
        assert line.count("Encaminhada") == 1

    def test_it_precedes_the_star_marker_too(self):
        stub = _Stub(forwarded_prefix_enabled=True)
        msg = _forwarded_msg(starred=True)
        line = stub._render_message_line(msg)
        assert line == "Encaminhada, ★ Fulano: corpo da mensagem"

    def test_a_non_forwarded_message_is_unaffected(self):
        stub = _Stub(forwarded_prefix_enabled=True)
        msg = _forwarded_msg(contextInfo={})
        line = stub._render_message_line(msg)
        assert "Encaminhada" not in line

    def test_edited_suffix_still_appends_after_the_body(self):
        stub = _Stub(forwarded_prefix_enabled=True)
        msg = _forwarded_msg(_edited=True)
        line = stub._render_message_line(msg)
        assert line == "Encaminhada, Fulano: corpo da mensagem, Editada"
