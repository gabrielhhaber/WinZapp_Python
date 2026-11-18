"""Tests for Settings > User interface > "Announce 'Starred' at the start of the
message" (user_interface.starred_prefix_enabled, default off).

Off: a trailing ", Favoritada" clause, like "Editada" and "Encaminhada". On: the
word moves ahead of the sender. When a message is also forwarded, "Encaminhada"
still comes first. Same stub approach as tests/test_forwarded_prefix_setting.py.
"""

from tests.test_forwarded_prefix_setting import _Stub, _forwarded_msg


def _stub(starred_prefix_enabled, forwarded_prefix_enabled=False):
    stub = _Stub(forwarded_prefix_enabled=forwarded_prefix_enabled)
    stub.main_window.settings["user_interface"][
        "starred_prefix_enabled"
    ] = starred_prefix_enabled
    return stub


def _starred(**overrides):
    return _forwarded_msg(contextInfo={}, starred=True, **overrides)


def test_default_keeps_the_word_at_the_end():
    line = _stub(False)._render_message_line(_starred())
    assert line == "Fulano: corpo da mensagem, Favoritada"


def test_a_missing_setting_counts_as_off():
    stub = _Stub()
    assert stub._render_message_line(_starred()) == "Fulano: corpo da mensagem, Favoritada"


def test_enabled_moves_the_word_before_the_sender():
    line = _stub(True)._render_message_line(_starred())
    assert line == "Favoritada, Fulano: corpo da mensagem"
    assert line.count("Favoritada") == 1


def test_an_unstarred_message_is_unaffected_either_way():
    for enabled in (False, True):
        line = _stub(enabled)._render_message_line(_forwarded_msg(contextInfo={}))
        assert "Favoritada" not in line


def test_the_word_stays_after_edited_when_off():
    line = _stub(False)._render_message_line(_starred(_edited=True))
    assert line == "Fulano: corpo da mensagem, Editada, Favoritada"


def test_forwarded_still_comes_first_when_both_move_to_the_front():
    stub = _stub(True, forwarded_prefix_enabled=True)
    line = stub._render_message_line(_forwarded_msg(starred=True))
    assert line == "Encaminhada, Favoritada, Fulano: corpo da mensagem"
