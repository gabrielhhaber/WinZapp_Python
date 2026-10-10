"""Opt-in removal of phone fallback prefixes; never rewrite message content.

Real row and foreground formatters run on plain stubs, without wx windows,
accounts, database access or screen-reader processes.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from core.message_sender_labels import (
    hide_unnamed_sender_numbers_enabled, message_sender_label, quoted_sender_label,
)
from core.notification_manager import (
    format_foreground_message, format_foreground_sender, format_notification_title,
)
from core.utils import DEFAULT_SETTINGS, backfill_missing_defaults, format_number, is_phone_like
from ui.conversation_panel.message_rendering import MessageRenderingMixin
from main_window.backfill import BackfillMixin
from main_window.identity import IdentityMixin

PHONE = "5511900000002@s.whatsapp.net"
LID = "222222222222222@lid"
GROUP = "120000000000000@g.us"
ME = "5511900000001@s.whatsapp.net"
BODY = "Call +55 11 90000-0002 at 12:34; https://example.com/1234567"


class _I18n:
    def t(self, key):
        return {
            "unnamed_participant": "Unnamed participant",
            "replying_to": "replying to {name}",
            "quoted_message_label": "quoted message",
            "of": "of",
        }.get(key, key)


class _MW:
    _normalize_jid = staticmethod(IdentityMixin._normalize_jid)
    _chat_jids_equivalent = BackfillMixin._chat_jids_equivalent
    _jid_address_forms = BackfillMixin._jid_address_forms

    def __init__(self, enabled=True):
        self.settings = {"user_interface": {"hide_unnamed_sender_numbers": enabled}}
        self.i18n = _I18n()
        self.contacts = {}
        self.chats = {}
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self._presence_pushname_map = {}
        self.my_jid = ME

    def self_reference_label(self):
        return "Me"

    def _is_self_jid(self, jid):
        return jid == ME

    def _is_bad_contact_name(self, name):
        return not name.strip() or is_phone_like(name) or "unknown" in name.lower()

    def get_chat(self, jid):
        return self.chats.get(jid)

    def _get_contact_tolerant(self, jid):
        return self.contacts.get(jid)

    def _resolve_contact_name(self, chat):
        contact = self.contacts.get(chat.get("remoteJid"), {})
        for value in (contact.get("name"), chat.get("name")):
            if value and not self._is_bad_contact_name(value):
                return value
        return ""

    def find_name_through_messages(self, chat):
        return ""


class _Panel:
    _render_message_line = MessageRenderingMixin._render_message_line
    _sender_label = MessageRenderingMixin._sender_label
    _saved_contact_name = MessageRenderingMixin._saved_contact_name
    _get_quoted_sender = MessageRenderingMixin._get_quoted_sender
    _is_message_forwarded = MessageRenderingMixin._is_message_forwarded
    _is_system_event = staticmethod(MessageRenderingMixin._is_system_event)

    def __init__(self, mw, remote=PHONE, mode="classic"):
        self.main_window = mw
        self.conversation = mw.chats.setdefault(remote, {"remoteJid": remote})
        self._sorted_messages = []
        self._message_list_mode = mode
        self._media_upload_progress = {}
        self.selected_messages = set()

    def _is_separator(self, msg):
        return False

    def _extract_timestamp(self, msg):
        return 0

    def _get_message_content(self, msg):
        return msg["message"]["conversation"]

    def _map_status(self, msg):
        return ""

    def _get_context_info(self, msg):
        return msg.get("contextInfo")

    def _get_quoted_preview(self, quoted):
        return quoted.get("conversation", "")

    def _reaction_counts(self, msg_id):
        return {}

    def _get_participant_name(self, jid, msg=None):
        return self._saved_contact_name(jid) or (msg or {}).get("pushName") or format_number(jid)


def _msg(remote=PHONE, participant="", push="", mine=False, **extra):
    return {"key": {"id": "M1", "remoteJid": remote, "participant": participant,
                    "fromMe": mine}, "pushName": push, "messageType": "conversation",
            "message": {"conversation": BODY}, **extra}


@pytest.mark.parametrize("settings,expected", [
    ({"user_interface": {"hide_unnamed_sender_numbers": True}}, True),
    ({"user_interface": {"hide_unnamed_sender_numbers": False}}, False),
    ({"user_interface": {"hide_unnamed_sender_numbers": "yes"}}, False),
    ({"user_interface": None}, False), ({}, False), (None, False),
])
def test_only_explicit_opt_in(settings, expected):
    assert hide_unnamed_sender_numbers_enabled(settings) is expected


def test_new_and_existing_accounts_default_off_without_overwriting_choice():
    defaults_file = Path(__file__).resolve().parents[1] / "client/data/settings_default.json"
    defaults = json.loads(defaults_file.read_text(encoding="utf-8"))
    assert defaults["user_interface"]["hide_unnamed_sender_numbers"] is False
    assert DEFAULT_SETTINGS["user_interface"]["hide_unnamed_sender_numbers"] is False
    legacy = {"user_interface": {"hide_own_sender_in_message_list": True}}
    backfill_missing_defaults(legacy, DEFAULT_SETTINGS)
    assert not hide_unnamed_sender_numbers_enabled(legacy)
    assert legacy["user_interface"]["hide_own_sender_in_message_list"] is True
    legacy["user_interface"]["hide_unnamed_sender_numbers"] = True
    backfill_missing_defaults(legacy, DEFAULT_SETTINGS)
    assert hide_unnamed_sender_numbers_enabled(legacy)


@pytest.mark.parametrize("remote,participant", [(PHONE, ""), (LID, ""), (GROUP, PHONE),
                                                   (GROUP, LID), (GROUP, "")])
@pytest.mark.parametrize("mode", ["classic", "listbox"])
def test_unnamed_rows_start_with_body_without_substitute_label(remote, participant, mode):
    mw = _MW()
    panel = _Panel(mw, remote, mode)
    msg = _msg(remote, participant)
    before = deepcopy(msg)
    assert panel._render_message_line(msg) == BODY
    assert msg == before
    assert format_foreground_message(msg, mw, mw.i18n, BODY) == BODY


@pytest.mark.parametrize("remote,participant", [(PHONE, ""), (GROUP, PHONE)])
def test_default_off_and_toggle_back_restore_number(remote, participant):
    mw = _MW(False)
    panel = _Panel(mw, remote)
    msg = _msg(remote, participant)
    original = panel._render_message_line(msg)
    assert original == f"{format_number(PHONE)}: {BODY}"
    mw.settings["user_interface"]["hide_unnamed_sender_numbers"] = True
    assert panel._render_message_line(msg) == BODY
    mw.settings["user_interface"]["hide_unnamed_sender_numbers"] = False
    assert panel._render_message_line(msg) == original


@pytest.mark.parametrize("remote", [LID, ""])
def test_default_off_keeps_existing_foreground_prefix(remote):
    mw = _MW(False)
    msg = _msg(remote)
    # Preserve the old formatter, including its unresolved-LID fallback, off.
    assert format_foreground_message(msg, mw, mw.i18n, BODY) == f"{format_number(remote)}: {BODY}"


def test_strict_checkbox_guard_is_recognized_without_accepting_other_comparisons():
    import ast
    from tests.test_settings_checkboxes_wiring import _unwrap_bool
    node = ast.parse("value is True", mode="eval").body
    assert isinstance(_unwrap_bool(node), ast.Name)
    for expression in ("value is False", "value == True", "value is True is False"):
        node = ast.parse(expression, mode="eval").body
        assert _unwrap_bool(node) is node


@pytest.mark.parametrize("name", ["Ayşe", "@someone", "user@example.com", "Studio 54"])
@pytest.mark.parametrize("remote,participant", [(PHONE, ""), (GROUP, LID)])
def test_push_names_are_kept_even_without_saved_contact(name, remote, participant):
    mw = _MW()
    panel = _Panel(mw, remote)
    msg = _msg(remote, participant, name)
    assert panel._render_message_line(msg) == f"{name}: {BODY}"
    assert format_foreground_sender(msg, mw, mw.i18n) == name


def test_lid_bridge_saved_contact_wins_and_new_name_appears_immediately():
    mw = _MW()
    mw._lid_to_phone[LID] = PHONE
    mw._phone_to_lid[PHONE] = LID
    panel = _Panel(mw, GROUP)
    msg = _msg(GROUP, LID)
    assert panel._render_message_line(msg) == BODY
    mw.contacts[PHONE] = {"remoteJid": PHONE, "name": "Saved name"}
    assert panel._render_message_line(msg) == f"Saved name: {BODY}"
    assert format_foreground_sender(msg, mw, mw.i18n) == "Saved name"


def test_real_push_name_wins_over_stale_phone_chat_name_when_opted_in():
    mw = _MW()
    panel = _Panel(mw)
    panel.conversation["name"] = format_number(PHONE)
    assert panel._render_message_line(_msg(push="Alice")) == f"Alice: {BODY}"


def test_own_prefix_and_independent_hide_own_setting():
    mw = _MW()
    panel = _Panel(mw)
    msg = _msg(mine=True)
    assert panel._render_message_line(msg) == f"Me: {BODY}"
    mw.settings["user_interface"]["hide_own_sender_in_message_list"] = True
    assert panel._render_message_line(msg) == BODY
    assert message_sender_label("12345678", msg, mw, mw.i18n) == "12345678"


def test_reply_omits_unnamed_quote_prefix_but_keeps_quoted_content_numbers():
    mw = _MW()
    panel = _Panel(mw, GROUP)
    msg = _msg(GROUP, LID, "Alice", contextInfo={
        "participant": PHONE, "quotedMessage": {"conversation": "123456789"},
    })
    assert panel._render_message_line(msg) == f"Alice: {BODY} , quoted message: 123456789"
    mw.contacts[PHONE] = {"name": "Bob"}
    assert "Alice, replying to Bob:" in panel._render_message_line(msg)


def test_quote_uses_original_push_name_and_not_replying_sender():
    mw = _MW()
    panel = _Panel(mw, GROUP)
    original = _msg(GROUP, PHONE, "Bob")
    panel._sorted_messages = [original]
    reply = _msg(GROUP, LID, "Alice")
    ctx = {"stanzaId": "M1"}
    assert quoted_sender_label(format_number(PHONE), ctx, reply, panel) == "Bob"
    panel._sorted_messages = []
    assert quoted_sender_label(format_number(PHONE), ctx, reply, panel) == ""


@pytest.mark.parametrize("participant,original_remote", [
    (PHONE, PHONE), ("5511900000002:5@c.us", PHONE), (PHONE, LID), (LID, PHONE),
    ("551100000002@s.whatsapp.net", LID),
    ("551199999999@s.whatsapp.net", "5511999999999@s.whatsapp.net"),
])
def test_private_quote_with_participant_preserves_original_message_name(participant, original_remote):
    mw = _MW()
    mw._lid_to_phone[LID] = PHONE
    mw._phone_to_lid[PHONE] = LID
    panel = _Panel(mw, original_remote)
    original = _msg(original_remote, push="Bob")
    before = deepcopy(original)
    panel._sorted_messages = [original]
    reply = _msg(original_remote, mine=True, contextInfo={
        "participant": participant, "stanzaId": "M1", "quotedMessage": {},
    })
    assert "Me, replying to Bob:" in panel._render_message_line(reply)
    assert original == before


@pytest.mark.parametrize("participant,original_remote", [
    (PHONE, "5511900000003@s.whatsapp.net"),
    ("222222222222222@s.whatsapp.net", LID),  # LID digits are not a phone
    (GROUP, GROUP),
])
def test_mismatched_quote_identity_does_not_borrow_original_name(participant, original_remote):
    mw = _MW()
    panel = _Panel(mw)
    panel._sorted_messages = [_msg(original_remote, push="Wrong person")]
    assert quoted_sender_label(format_number(PHONE), {"participant": participant, "stanzaId": "M1"},
                               _msg(mine=True), panel) == ""


def test_numeric_self_reference_is_kept_in_a_quote():
    mw = _MW()
    panel = _Panel(mw, GROUP)
    assert quoted_sender_label("12345678", {"participant": ME}, _msg(GROUP, LID), panel) == "12345678"


def test_other_notifications_and_chat_identity_are_not_hidden():
    mw = _MW()
    msg = _msg()
    assert format_notification_title(msg, mw, mw.i18n) == format_number(PHONE)
    assert msg["key"]["remoteJid"] == PHONE
    assert not hasattr(mw, "_unnamed_sender_labels")


@pytest.mark.parametrize("label", ["", "Unnamed participant", "12345678", "+55 11 90000-0002", LID])
def test_missing_phone_and_raw_identity_labels_have_no_substitute(label):
    mw = _MW()
    assert message_sender_label(label, _msg(), mw, mw.i18n) == ""
