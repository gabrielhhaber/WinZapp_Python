"""Which name a group toast leads with (Settings > General > Notifications).

"participant" (default, what WinZapp always did): "Gustavo in WinZapp Devs".
"group" (the official WhatsApp Desktop order): "WinZapp Devs, Gustavo".
The choice shapes toast text only: the announcement spoken with the window
active asks for the title without ``toast=True`` and never changes order.
"""

import pytest

from core.notification_manager import (
    DEFAULT_GROUP_TOAST_ORDER,
    GROUP_TOAST_ORDERS,
    format_notification_title,
    group_toast_order,
)
from core.utils import DEFAULT_SETTINGS


class _I18n:
    _STRINGS = {
        "notif_in_group": "{participant} in {group}",
        "notif_in_group_group_first": "{group}, {participant}",
        "notif_replied_to_you": "",
        "notif_mentioned_you": "",
        "notif_mentioned_all": "",
    }

    def t(self, key):
        return self._STRINGS.get(key, f"[{key}]")


class _MW:
    def __init__(self, order=None):
        self.chats = {"g@g.us": {"remoteJid": "g@g.us", "name": "Devs"}}
        self.contacts = {}
        self.settings = {"general": {} if order is None else {"group_toast_order": order}}

    @staticmethod
    def _group_name_from_chat_dict(chat):
        return ""

    @staticmethod
    def _get_contact_tolerant(jid):
        return None

    @staticmethod
    def _resolve_contact_name(chat):
        return "Gustavo" if chat.get("remoteJid", "").startswith("5511999") else None


def _msg():
    return {"messageType": "conversation", "message": {}, "pushName": "Gustavo",
            "key": {"remoteJid": "g@g.us", "participant": "5511999999999@s.whatsapp.net"}}


def _title(order, toast):
    return format_notification_title(_msg(), _MW(order), _I18n(), toast=toast)


def test_the_default_is_participant_first_and_is_stored_that_way():
    assert DEFAULT_GROUP_TOAST_ORDER == "participant"
    assert DEFAULT_SETTINGS["general"]["group_toast_order"] == DEFAULT_GROUP_TOAST_ORDER
    assert group_toast_order({}) == "participant"


@pytest.mark.parametrize("junk", [None, "", "Group", 3])
def test_an_unknown_stored_value_reads_as_the_default(junk):
    assert group_toast_order({"general": {"group_toast_order": junk}}) == "participant"


def test_the_choices_are_the_two_documented_ones():
    assert GROUP_TOAST_ORDERS == ("participant", "group")


def test_a_toast_with_participant_first_names_the_participant_first():
    assert _title("participant", toast=True).endswith("in Devs")


def test_a_toast_with_group_first_names_the_group_first():
    title = _title("group", toast=True)
    assert title.startswith("Devs, ")
    assert " in " not in title


def test_the_spoken_announcement_ignores_the_choice():
    assert _title("group", toast=False) == _title("participant", toast=False)
    assert " in Devs" in _title("group", toast=False)


def test_a_private_chat_title_is_not_affected():
    msg = {"messageType": "conversation", "message": {}, "pushName": "Ana",
           "key": {"remoteJid": "5511988887777@s.whatsapp.net"}}
    mw = _MW("group")
    mw.chats = {}
    assert (format_notification_title(msg, mw, _I18n(), toast=True)
            == format_notification_title(msg, mw, _I18n(), toast=False))


def test_only_the_toast_paths_ask_for_the_toast_order():
    """The toast of a message and of a reaction ask for it; the announcement
    spoken with the window active (fg_new_msg, reaction speech) does not."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "client" / "main_window"
           / "message_events.py").read_text(encoding="utf-8")
    assert src.count("format_notification_title(msg, self, self.i18n, toast=True)") == 2
    assert src.count("format_notification_title(msg, self, self.i18n)") == 2
