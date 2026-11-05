"""core/chat_row_text.py: where a chat row says how many new messages it has (issue 684)."""
import pytest

from core.chat_row_text import name_with_unread, unread_badge, unread_position


class I18n:
    def t(self, key):
        return {"unread_messages": "unread messages", "unread_message": "unread message"}[key]


def test_the_count_goes_after_the_name_by_default():
    assert name_with_unread("Ana", 3, I18n()) == "Ana 3 unread messages"
    assert name_with_unread("Ana", 1, I18n(), "after") == "Ana 1 unread message"


def test_the_count_can_go_before_the_name():
    assert name_with_unread("Ana", 3, I18n(), "before") == "3 unread messages Ana"


@pytest.mark.parametrize("unread", [0, None, -1, "x"])
def test_no_count_leaves_the_name_alone(unread):
    assert name_with_unread("Ana", unread, I18n(), "before") == "Ana"
    assert unread_badge(unread, I18n()) == ""


@pytest.mark.parametrize("settings, position", [
    ({"user_interface": {"unread_count_position": "before"}}, "before"),
    ({"user_interface": {"unread_count_position": "after"}}, "after"),
    ({"user_interface": {"unread_count_position": "sideways"}}, "after"),
    ({}, "after"), (None, "after"),
])
def test_the_setting_is_read_with_after_as_the_default(settings, position):
    assert unread_position(settings) == position


def test_every_row_builder_uses_the_one_function():
    import inspect
    from main_window import chat_events, chat_list
    from ui import chat_lock
    for module in (chat_list, chat_events, chat_lock):
        source = inspect.getsource(module)
        assert "name_with_unread(" in source, module.__name__
        assert '+ (self.i18n.t("unread_messages")' not in source, module.__name__
