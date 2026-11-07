import pytest

from ui.conversation_panel.copy_target import copy_method_for_message


@pytest.mark.parametrize("msg_type", [
    "documentMessage", "imageMessage", "videoMessage", "audioMessage"])
def test_media_copies_the_file(msg_type):
    assert copy_method_for_message({"messageType": msg_type}) == "_on_menu_copy_file"


def test_contact_copies_number():
    assert copy_method_for_message({"messageType": "contactMessage"}) == "_on_contact_copy_number"


@pytest.mark.parametrize("msg", [{"messageType": "conversation"}, {}, None])
def test_text_copies_text(msg):
    assert copy_method_for_message(msg) == "_on_menu_copy_message"
