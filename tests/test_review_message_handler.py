"""The handler of the ten review shortcuts (issue #462), on a plain stub.

ConversationsPanel needs a wx.App; the pure counting and the double-press rule
are in tests/test_review_message_shortcuts.py. Here: what is said, what is
copied, and that nothing moves.
"""

import types

from ui.conversation_panel import message_accels
from ui.conversations import ConversationsPanel


class _Panel:
    _on_accel_review_message = ConversationsPanel._on_accel_review_message
    _is_separator = ConversationsPanel._is_separator

    def __init__(self, ids=("a", "b", "c"), conversation=True):
        self._sorted_messages = [{"key": {"id": i}} for i in ids]
        self.conversation = {"remoteJid": "x@s.whatsapp.net"} if conversation else None
        self.said = []
        self.copied = []
        self.main_window = types.SimpleNamespace(
            i18n=types.SimpleNamespace(t=lambda key: f"[{key}]"),
            output=lambda text, interrupt=False: self.said.append(text),
        )
        self.messages_list = types.SimpleNamespace(
            Focus=lambda i: self.said.append("<focus moved>"),
        )

    def _render_message_line(self, msg, index=None, total=None, **_kw):
        return f"line {msg['key']['id']} {index}/{total}"

    def _on_menu_copy_message(self, msg):
        self.copied.append(msg["key"]["id"])


class _Clock:
    now = 100.0


def _clock(monkeypatch):
    monkeypatch.setattr(message_accels.time, "monotonic", lambda: _Clock.now)
    _Clock.now = 100.0


def test_one_reads_the_newest_and_moves_nothing(monkeypatch):
    _clock(monkeypatch)
    panel = _Panel()
    panel._on_accel_review_message(1)
    assert panel.said == ["line c 2/3"]


def test_two_reads_the_one_before(monkeypatch):
    _clock(monkeypatch)
    panel = _Panel()
    panel._on_accel_review_message(2)
    assert panel.said == ["line b 1/3"]


def test_zero_is_the_tenth(monkeypatch):
    _clock(monkeypatch)
    panel = _Panel(ids=[f"m{n}" for n in range(12)])
    panel._on_accel_review_message(0)
    assert panel.said == ["line m2 2/12"]


def test_a_position_with_no_message_says_so(monkeypatch):
    _clock(monkeypatch)
    panel = _Panel(ids=("a",))
    panel._on_accel_review_message(5)
    assert panel.said == ["[review_no_message]"]


def test_the_same_key_twice_quickly_copies_instead_of_reading_again(monkeypatch):
    _clock(monkeypatch)
    panel = _Panel()
    panel._on_accel_review_message(1)
    _Clock.now += 0.4
    panel._on_accel_review_message(1)
    assert panel.copied == ["c"]
    assert panel.said == ["line c 2/3"]


def test_the_third_press_reads_again_rather_than_copying_a_second_time(monkeypatch):
    _clock(monkeypatch)
    panel = _Panel()
    for _ in range(3):
        panel._on_accel_review_message(1)
        _Clock.now += 0.2
    assert panel.copied == ["c"]
    assert panel.said == ["line c 2/3", "line c 2/3"]


def test_a_slow_second_press_reads_again(monkeypatch):
    _clock(monkeypatch)
    panel = _Panel()
    panel._on_accel_review_message(1)
    _Clock.now += 5
    panel._on_accel_review_message(1)
    assert panel.copied == [] and len(panel.said) == 2


def test_a_different_key_in_between_starts_over(monkeypatch):
    _clock(monkeypatch)
    panel = _Panel()
    panel._on_accel_review_message(1)
    panel._on_accel_review_message(2)
    panel._on_accel_review_message(1)
    assert panel.copied == []


def test_with_no_conversation_open_it_does_nothing(monkeypatch):
    _clock(monkeypatch)
    panel = _Panel(conversation=False)
    panel._on_accel_review_message(1)
    assert panel.said == [] and panel.copied == []
