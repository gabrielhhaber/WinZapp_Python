"""core/bookmark_settings.py: which bookmark kinds remove themselves after use."""

from core.bookmark_settings import remove_after_use


def test_off_by_default():
    assert remove_after_use({}, temporary=False) is False
    assert remove_after_use({}, temporary=True) is False
    assert remove_after_use(None, temporary=True) is False


def test_each_kind_has_its_own_setting():
    settings = {"user_interface": {"remove_bookmarks_after_use": True}}
    assert remove_after_use(settings, temporary=False) is True
    assert remove_after_use(settings, temporary=True) is False
    settings = {"user_interface": {"remove_temp_bookmarks_after_use": True}}
    assert remove_after_use(settings, temporary=False) is False
    assert remove_after_use(settings, temporary=True) is True


def test_the_settings_dialog_writes_the_same_keys():
    from core import bookmark_settings as bs, reading_position as rp
    assert (bs.SECTION, bs.REMOVE_BOOKMARKS_SETTING, bs.REMOVE_TEMP_BOOKMARKS_SETTING) == (
        "user_interface", "remove_bookmarks_after_use", "remove_temp_bookmarks_after_use")
    assert (rp.SECTION, rp.SETTING) == ("user_interface", "message_list_open_position")
