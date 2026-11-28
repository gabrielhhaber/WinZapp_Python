"""Next / previous message of a kind: unplayed voice, photo/video/sticker,
document, poll, link (ui/conversation_panel/semantic_jump.py).

The matching and the position arithmetic are pure and tested directly; the
handler runs on a small stub (ConversationsPanel needs a wx.App), as in
tests/test_next_mention_and_reply.py.
"""

import pathlib
import re

import pytest

import core.keyboard_shortcuts as keyboard_shortcuts
from core.shortcut_catalog import SHORTCUTS
from tests.locales import load_strings, registered_locale_codes
from ui.conversation_panel.semantic_jump import (
    AUDIO, DOCUMENT, KIND_LABEL_KEYS, KINDS, LINK, MEDIA, POLL,
    SemanticJumpMixin, message_matches_kind, pick_semantic_position, semantic_indices,
)

CLIENT = pathlib.Path(__file__).resolve().parents[1] / "client"
LOCALES = list(registered_locale_codes())


def _msg(msg_type, from_me=False, inner=None, **extra):
    return {"messageType": msg_type, "key": {"id": "x", "fromMe": from_me},
            "message": inner if inner is not None else {}, **extra}


class TestUnplayedAudio:
    def test_a_received_voice_message_with_no_status_is_unplayed(self):
        assert message_matches_kind(_msg("audioMessage"), AUDIO)

    def test_a_played_status_in_the_history_makes_it_played(self):
        for status in ("PLAYED", "5", 5):
            assert not message_matches_kind(
                _msg("audioMessage", MessageUpdate=[{"status": "2"}, {"status": status}]), AUDIO)

    def test_the_root_status_and_ack_count_too(self):
        assert not message_matches_kind(_msg("audioMessage", status="PLAYED"), AUDIO)
        assert not message_matches_kind(_msg("audioMessage", ack=4), AUDIO)

    def test_delivered_or_read_is_not_played(self):
        assert message_matches_kind(
            _msg("audioMessage", MessageUpdate=[{"status": "3"}, {"status": "4"}], ack=3), AUDIO)

    def test_my_own_voice_message_is_never_unplayed(self):
        assert not message_matches_kind(_msg("audioMessage", from_me=True), AUDIO)

    def test_other_types_are_not_audio(self):
        assert not message_matches_kind(_msg("videoMessage"), AUDIO)


class TestOtherKinds:
    @pytest.mark.parametrize("msg_type", ["imageMessage", "videoMessage", "stickerMessage"])
    def test_visual_media(self, msg_type):
        assert message_matches_kind(_msg(msg_type), MEDIA)
        assert not message_matches_kind(_msg(msg_type), DOCUMENT)

    def test_document(self):
        assert message_matches_kind(_msg("documentMessage"), DOCUMENT)
        assert not message_matches_kind(_msg("documentMessage"), MEDIA)

    @pytest.mark.parametrize(
        "msg_type", ["pollCreationMessage", "pollCreationMessageV2", "pollCreationMessageV3"])
    def test_poll_creation_but_not_a_vote(self, msg_type):
        assert message_matches_kind(_msg(msg_type), POLL)
        assert not message_matches_kind(_msg("pollUpdateMessage"), POLL)

    def test_a_link_in_a_plain_text(self):
        assert message_matches_kind(
            _msg("conversation", inner={"conversation": "veja https://exemplo.com/a"}), LINK)

    def test_a_link_in_an_extended_text_or_a_caption(self):
        assert message_matches_kind(
            _msg("extendedTextMessage", inner={"extendedTextMessage": {"text": "www.exemplo.com"}}), LINK)
        assert message_matches_kind(
            _msg("imageMessage", inner={"imageMessage": {"caption": "fonte http://a.b"}}), LINK)

    def test_a_link_when_the_body_is_a_json_string(self):
        assert message_matches_kind(
            _msg("conversation", inner='{"conversation": "https://a.b"}'), LINK)

    def test_text_without_a_link_and_a_broken_body(self):
        assert not message_matches_kind(_msg("conversation", inner={"conversation": "oi"}), LINK)
        assert not message_matches_kind(_msg("conversation", inner="not json"), LINK)

    def test_a_quoted_link_is_not_the_messages_own(self):
        quoted = {"extendedTextMessage": {
            "text": "sim",
            "contextInfo": {"quotedMessage": {"conversation": "https://a.b"}}}}
        assert not message_matches_kind(_msg("extendedTextMessage", inner=quoted), LINK)

    def test_junk_is_nothing(self):
        assert not message_matches_kind(None, LINK)
        assert not message_matches_kind({}, "unknown")


class TestIndices:
    def test_separators_and_other_rows_are_skipped(self):
        rows = [_msg("documentMessage"), {"_type": "unread_separator"}, _msg("conversation"),
                _msg("documentMessage")]
        assert semantic_indices(rows, DOCUMENT, lambda m: m.get("_type") == "unread_separator") == [0, 3]


class TestPosition:
    def test_forward_is_the_first_one_below_the_focused_row(self):
        assert pick_semantic_position([2, 5, 9], 3, True) == 1

    def test_forward_from_a_match_goes_to_the_next(self):
        assert pick_semantic_position([2, 5, 9], 5, True) == 2

    def test_forward_past_the_last_is_none_and_does_not_wrap(self):
        assert pick_semantic_position([2, 5, 9], 9, True) == -1
        assert pick_semantic_position([2, 5, 9], 12, True) == -1

    def test_backward_is_the_closest_one_above(self):
        assert pick_semantic_position([2, 5, 9], 6, False) == 1
        assert pick_semantic_position([2, 5, 9], 5, False) == 0

    def test_backward_before_the_first_is_none(self):
        assert pick_semantic_position([2, 5, 9], 2, False) == -1
        assert pick_semantic_position([2, 5, 9], 0, False) == -1

    def test_nothing_focused_starts_from_the_oldest_or_the_newest(self):
        assert pick_semantic_position([2, 5, 9], -1, True) == 0
        assert pick_semantic_position([2, 5, 9], -1, False) == 2

    def test_no_matches(self):
        assert pick_semantic_position([], 3, True) == -1
        assert pick_semantic_position([], 3, False) == -1


class _I18n:
    def t(self, key):
        return {"semantic_kind_poll": "Poll", "semantic_jump_found": "{kind}: {position}/{total}",
                "semantic_jump_none": "none {kind}", "semantic_jump_no_next": "no next {kind}",
                "semantic_jump_no_previous": "no previous {kind}"}.get(key, key)


class _Window:
    def __init__(self):
        self.i18n = _I18n()
        self.said = []

    def output(self, text, interrupt=False):
        self.said.append(text)


class _Panel(SemanticJumpMixin):
    def __init__(self, messages, focused=-1, open_chat=True):
        self.main_window = _Window()
        self._sorted_messages = messages
        self.conversation = {"remoteJid": "a@s.whatsapp.net"} if open_chat else None
        self._focused = focused
        self.focused_rows = []
        self.messages_list = self

    def GetFocusedItem(self):
        return self._focused

    def _is_separator(self, msg):
        return msg.get("_type") == "unread_separator"

    def _focus_message_row(self, idx):
        self.focused_rows.append(idx)
        self._focused = idx


class TestHandler:
    ROWS = [_msg("conversation"), _msg("pollCreationMessage"), _msg("conversation"),
            _msg("pollCreationMessageV3")]

    def test_next_focuses_the_row_and_says_kind_and_position(self):
        panel = _Panel(self.ROWS, focused=0)
        panel._on_accel_semantic_jump(POLL, True)
        assert panel.focused_rows == [1]
        assert panel.main_window.said == ["Poll: 1/2"]

    def test_previous_goes_back(self):
        panel = _Panel(self.ROWS, focused=3)
        panel._on_accel_semantic_jump(POLL, False)
        assert panel.focused_rows == [1]
        assert panel.main_window.said == ["Poll: 1/2"]

    def test_past_the_last_it_says_so_and_stays(self):
        panel = _Panel(self.ROWS, focused=3)
        panel._on_accel_semantic_jump(POLL, True)
        assert panel.focused_rows == []
        assert panel.main_window.said == ["no next Poll"]

    def test_before_the_first_it_says_so_and_stays(self):
        panel = _Panel(self.ROWS, focused=1)
        panel._on_accel_semantic_jump(POLL, False)
        assert panel.focused_rows == []
        assert panel.main_window.said == ["no previous Poll"]

    def test_with_none_in_the_conversation_it_says_so(self):
        panel = _Panel(self.ROWS, focused=0)
        panel._on_accel_semantic_jump(DOCUMENT, True)
        assert panel.focused_rows == []
        assert panel.main_window.said == ["none semantic_kind_document"]

    def test_with_no_conversation_open_it_does_nothing(self):
        panel = _Panel(self.ROWS, open_chat=False)
        panel._on_accel_semantic_jump(POLL, True)
        assert panel.main_window.said == []


ROWS = [(k, d) for k in KINDS for d in ("next", "prev")]


def _row(kind, direction):
    [row] = [s for s in SHORTCUTS if s.id == f"messages.ID_JUMP_{kind.upper()}_{direction.upper()}"]
    return row


class TestTheKeys:
    @pytest.mark.parametrize("kind, direction", ROWS)
    def test_each_is_a_catalogue_row_the_shortcuts_tab_can_change(self, kind, direction):
        row = _row(kind, direction)
        assert row.scope == "messages"
        assert row.label == f"shortcut_jump_{kind}_{direction}_label"
        # Alt+F5..F9 forward, Alt+Shift+F5..F9 back; F5 is key code 344.
        assert (row.mod, row.key) == (1 if direction == "next" else 5, 344 + KINDS.index(kind))

    @pytest.mark.parametrize("locale", LOCALES)
    @pytest.mark.parametrize("kind, direction", ROWS)
    def test_no_other_command_shares_the_default_in_any_language(self, locale, kind, direction):
        table = load_strings(locale)
        translate = table.__getitem__
        row = _row(kind, direction)
        binding = keyboard_shortcuts.default_binding(row, translate)
        assert keyboard_shortcuts.conflicts(row, binding, {}, SHORTCUTS, translate) == []
        same = [
            other.id for other in SHORTCUTS
            if other.id != row.id
            and keyboard_shortcuts.default_binding(other, translate) == binding
            and keyboard_shortcuts.scopes_overlap(row.scope, other.scope)
        ]
        assert same == []

    @pytest.mark.parametrize("locale", LOCALES)
    @pytest.mark.parametrize("kind, direction", ROWS)
    def test_the_label_names_its_key(self, locale, kind, direction):
        table = load_strings(locale)
        prefix = "Alt+F" if direction == "next" else "Alt+Shift+F"
        assert table[f"shortcut_jump_{kind}_{direction}_label"].startswith(
            f"{prefix}{5 + KINDS.index(kind)}: ")

    @pytest.mark.parametrize("locale", LOCALES)
    def test_every_spoken_string_exists_with_its_placeholders(self, locale):
        table = load_strings(locale)
        for key in KIND_LABEL_KEYS.values():
            assert table[key].strip()
        assert {"{kind}", "{position}", "{total}"} <= set(re.findall(r"\{\w+\}", table["semantic_jump_found"]))
        for key in ("semantic_jump_none", "semantic_jump_no_next", "semantic_jump_no_previous"):
            assert "{kind}" in table[key]

    def test_they_are_in_the_messages_table_and_bound(self):
        source = (CLIENT / "ui/conversation_panel/accelerators.py").read_text(encoding="utf-8")
        for kind in KINDS:
            for direction in ("NEXT", "PREV"):
                assert f"self.ID_JUMP_{kind.upper()}_{direction})," in source
        assert "_on_accel_semantic_jump(k, True)" in source
        assert "_on_accel_semantic_jump(k, False)" in source

    @pytest.mark.parametrize("kind, direction", ROWS)
    def test_the_f1_list_describes_them(self, kind, direction):
        source = (CLIENT / "ui/dialogs/shortcuts_dialog.py").read_text(encoding="utf-8")
        assert f'i18n.t("shortcut_jump_{kind}_{direction}_label")' in source
