"""Tests for the "auto_play_voice_in_sequence" setting: with it off, a voice
note that finishes ends the sequence instead of starting the next one.

Same stub approach as tests/test_audio_chain_auto_focus_setting.py.
"""

import pytest

from ui.conversations import ConversationsPanel
from tests.test_audio_chain_auto_focus_setting import (  # noqa: F401
    _Stub as _ChainStub,
    _voice_msg,
    call_later_now,
)


class _Stub(_ChainStub):
    _next_message_is_chainable_audio = ConversationsPanel._next_message_is_chainable_audio

    def __init__(self, sorted_messages, focused, play_in_sequence=None):
        super().__init__(sorted_messages, focused)
        if play_in_sequence is not None:
            self.main_window.settings["user_interface"][
                "auto_play_voice_in_sequence"
            ] = play_in_sequence


def test_default_setting_chains_the_next_voice_note(call_later_now):
    stub = _Stub([_voice_msg("m1"), _voice_msg("m2")], focused=0)

    stub._auto_chain_next_audio("m1")

    assert stub.toggle_calls == ["m2"]
    assert stub._next_message_is_chainable_audio("m1") is True


def test_disabled_setting_plays_nothing_after_the_finished_voice_note(call_later_now):
    stub = _Stub([_voice_msg("m1"), _voice_msg("m2")], focused=0, play_in_sequence=False)

    stub._auto_chain_next_audio("m1")

    assert stub.toggle_calls == []
    assert stub.messages_list.focus_calls == []
    assert stub._is_in_audio_chain is False
    assert stub._next_message_is_chainable_audio("m1") is False


def test_disabled_setting_still_flushes_the_pending_played_refresh(call_later_now):
    stub = _Stub([_voice_msg("m1"), _voice_msg("m2")], focused=0, play_in_sequence=False)

    stub._auto_chain_next_audio("m1", pending_played_msg_id="m1")

    assert stub.events == [("refresh", "m1")]
