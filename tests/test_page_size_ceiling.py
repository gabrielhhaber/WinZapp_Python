"""The messages-per-chat setting has a ceiling (issue #812): a value of tens of
thousands froze the window for over a minute."""

from core.page_size import (DEFAULT_MESSAGES_PAGE_SIZE, MAX_MESSAGES_PAGE_SIZE,
                            clamp_messages_page_size, messages_page_size)


class TestClamp:
    def test_a_value_above_the_ceiling_is_the_ceiling(self):
        assert clamp_messages_page_size(50000) == MAX_MESSAGES_PAGE_SIZE == 1000

    def test_a_value_inside_the_range_is_unchanged(self):
        assert clamp_messages_page_size(15) == 15
        assert clamp_messages_page_size(1000) == 1000

    def test_zero_and_negative_become_one(self):
        assert clamp_messages_page_size(0) == 1
        assert clamp_messages_page_size(-5) == 1

    def test_text_that_is_a_number_works_and_junk_is_the_default(self):
        assert clamp_messages_page_size("300") == 300
        assert clamp_messages_page_size("abc") == DEFAULT_MESSAGES_PAGE_SIZE
        assert clamp_messages_page_size(None) == DEFAULT_MESSAGES_PAGE_SIZE


class TestMessagesPageSize:
    def test_reads_and_bounds_the_setting(self):
        settings = {"user_interface": {"messages_page_size": 100000}}
        assert messages_page_size(settings) == 1000

    def test_a_missing_setting_is_the_default(self):
        assert messages_page_size({}) == DEFAULT_MESSAGES_PAGE_SIZE
        assert messages_page_size({"user_interface": {}}) == DEFAULT_MESSAGES_PAGE_SIZE

    def test_a_settings_object_that_is_not_a_dict_is_the_default(self):
        assert messages_page_size(None) == DEFAULT_MESSAGES_PAGE_SIZE
