"""Skin tones in the quick reactions list.

Asked for: the reactions list had no way to change the skin tone of an emoji
that has one (the thumbs up, for one), which the emoji picker already offers
with the Left and Right arrows.
"""

import inspect

from core.reaction_shortcuts import reaction_to_send
from ui.conversation_panel import reactions
from ui.dialogs.emoji_picker import EmojiPickerDialog, skin_tone_step

THUMB = "👍"
TONES = [THUMB + chr(code) for code in range(0x1F3FB, 0x1F400)]


def test_right_walks_from_the_plain_emoji_through_its_five_tones_and_round():
    emoji, seen = THUMB, []
    for _ in range(6):
        emoji = skin_tone_step(emoji, 1)
        seen.append(emoji)
    assert seen == TONES + [THUMB]


def test_left_walks_the_other_way():
    assert skin_tone_step(THUMB, -1) == TONES[-1]
    assert skin_tone_step(TONES[0], -1) == THUMB


def test_an_emoji_with_no_tones_is_left_alone():
    assert skin_tone_step("😀", 1) is None   # grinning face
    assert skin_tone_step("❤️", -1) is None  # red heart
    assert skin_tone_step("not an emoji", 1) is None


def test_the_picker_and_the_quick_list_share_the_one_ladder():
    assert "skin_tone_step(" in inspect.getsource(EmojiPickerDialog._cycle_skin_tone)
    source = inspect.getsource(reactions.ReactionsMixin._on_menu_react)
    assert "skin_tone_step(" in source and "wx.EVT_KEY_DOWN" in source
    assert "wx.WXK_LEFT" in source and "wx.WXK_RIGHT" in source
    # Said aloud: a changed row is silent to a screen reader otherwise.
    assert "self.main_window.output(toned, interrupt=True)" in source


def test_only_the_very_reaction_already_sent_is_removed_by_choosing_it_again():
    assert reaction_to_send(THUMB, THUMB) == ""
    # Another tone of the reaction I sent is another reaction: it replaces it.
    assert reaction_to_send(TONES[2], THUMB) == TONES[2]
    assert reaction_to_send(THUMB, "") == THUMB
