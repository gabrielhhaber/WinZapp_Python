"""Tests for @mentions inside a QUOTED message's preview never resolving to a
contact name — always showing the raw @<phone-or-lid-digits> placeholder.

Root cause, in two parts:

1. core.utils._slim_quoted_message() strips a quoted message down to just a
   capped text preview (see its own docstring for why — the full quoted
   message WPPConnect embeds is mostly base64 thumbnails/mediaKeys/hashes that
   bloat messages.dat). It kept the mention *text* (the "@5511999999999" that
   ends up inside the preview string) but dropped the mentionedJid list that
   is the only thing that lets it be turned into "@Ana" — so the placeholder
   survived slimming forever, with no way left to resolve it.

2. ConversationsPanel._get_quoted_preview()'s "conversation" branch (the one
   actually hit for every slimmed quoted message — see _slim_quoted_message,
   which always emits {"conversation": text, ...}, never the
   {"extendedTextMessage": {...}} shape) returned the raw text unconditionally,
   without even attempting mention resolution. Only the "extendedTextMessage"
   branch — reachable in practice only for a locally-composed reply that has
   not yet round-tripped through the slimming step — ever called
   _resolve_mentions_in_text().

ConversationsPanel is a wx.Panel and cannot be instantiated without a running
wx.App, so the method under test is exercised as a plain function against a
small stub — same approach as tests/test_mentions.py.
"""

import pytest

from core.utils import _slim_quoted_message
from ui.conversations import ConversationsPanel


# ── core.utils._slim_quoted_message keeps mentionedJid ──────────────────────


class TestSlimQuotedMessageKeepsMentions:
    def test_flat_mentioned_jid_list_survives_slimming(self):
        """WPPConnect's raw quotedMsg shape: mentions flat on the dict itself,
        same convention as the outer message's own mentionedJidList."""
        quoted = {
            "conversation": "@5511999999999 bom dia",
            "mentionedJidList": ["5511999999999@s.whatsapp.net"],
        }
        slim = _slim_quoted_message(quoted)
        assert slim["conversation"] == "@5511999999999 bom dia"
        assert slim["mentionedJid"] == ["5511999999999@s.whatsapp.net"]

    def test_mentions_nested_under_top_level_context_info_survive(self):
        quoted = {
            "conversation": "@111 bom dia",
            "contextInfo": {"mentionedJid": ["111@lid"]},
        }
        slim = _slim_quoted_message(quoted)
        assert slim["mentionedJid"] == ["111@lid"]

    def test_mentions_nested_under_extended_text_message_survive(self):
        """A Baileys-shaped quotedMessage proto: extendedTextMessage carries
        its own nested contextInfo."""
        quoted = {
            "extendedTextMessage": {
                "text": "@111 bom dia",
                "contextInfo": {"mentionedJid": ["111@lid"]},
            }
        }
        slim = _slim_quoted_message(quoted)
        assert slim["conversation"] == "@111 bom dia"
        assert slim["mentionedJid"] == ["111@lid"]

    def test_a_wid_dict_mention_is_normalized_to_a_plain_string(self):
        """mentionedJidList entries can arrive as a raw WPPConnect Wid object
        instead of a plain string."""
        quoted = {
            "conversation": "@111 oi",
            "mentionedJidList": [{"_serialized": "5511999999999@c.us"}],
        }
        slim = _slim_quoted_message(quoted)
        assert slim["mentionedJid"] == ["5511999999999@s.whatsapp.net"]

    def test_no_mentions_means_no_key_added(self):
        quoted = {"conversation": "oi, tudo bem?"}
        slim = _slim_quoted_message(quoted)
        assert "mentionedJid" not in slim

    def test_re_slimming_an_already_slim_dict_is_a_no_op(self):
        """prune_message_record() re-slims already-stored records — must not
        lose the mentions it already kept on a previous pass."""
        already_slim = {
            "conversation": "@111 bom dia",
            "mentionedJid": ["111@lid"],
        }
        assert _slim_quoted_message(already_slim) == already_slim

    def test_media_only_quote_is_unaffected(self):
        quoted = {"imageMessage": {"caption": ""}}
        slim = _slim_quoted_message(quoted)
        assert "mentionedJid" not in slim
        assert "imageMessage" in slim


# ── ConversationsPanel._get_quoted_preview resolves those mentions ──────────


class _FakeI18n:
    def t(self, key):
        return key


class _FakeMainWindow:
    def __init__(self, lid_to_phone=None, self_jid="", self_label="Me"):
        self._lid_to_phone = lid_to_phone or {}
        self.i18n = _FakeI18n()
        self._self_jid = self_jid
        self._self_label = self_label

    def _is_self_jid(self, jid):
        return bool(self._self_jid) and jid == self._self_jid

    def self_reference_label(self):
        return self._self_label


class _Stub:
    _get_quoted_preview = ConversationsPanel._get_quoted_preview
    _resolve_mentions_in_text = ConversationsPanel._resolve_mentions_in_text

    def __init__(self, main_window, names=None):
        self.main_window = main_window
        self._names = names or {}

    def _get_participant_name(self, jid):
        return self._names.get(jid, jid)


LID_TO_PHONE = {"111@lid": "5511111111111@s.whatsapp.net"}


class TestGetQuotedPreviewResolvesMentions:
    def test_the_slimmed_shape_resolves_a_phone_mention_to_a_name(self):
        """The shape _slim_quoted_message() actually produces for every quoted
        message that went through normal storage — this is the common case
        the bug report was about."""
        s = _Stub(
            _FakeMainWindow(),
            names={"5511111111111@s.whatsapp.net": "Ana"},
        )
        quoted = {
            "conversation": "oi @5511111111111 bom dia",
            "mentionedJid": ["5511111111111@s.whatsapp.net"],
        }
        assert s._get_quoted_preview(quoted) == "oi @Ana bom dia"

    def test_the_slimmed_shape_resolves_a_lid_mention_to_a_name(self):
        s = _Stub(
            _FakeMainWindow(LID_TO_PHONE),
            names={"111@lid": "Ana"},
        )
        quoted = {
            "conversation": "oi @111 bom dia",
            "mentionedJid": ["111@lid"],
        }
        assert s._get_quoted_preview(quoted) == "oi @Ana bom dia"

    def test_self_mention_uses_active_locale_self_reference_label(self):
        own_jid = "5511999999999@s.whatsapp.net"
        s = _Stub(_FakeMainWindow(self_jid=own_jid, self_label="Me"))
        assert s._resolve_mentions_in_text(
            "testing @5511999999999", [own_jid]
        ) == "testing @Me"

    def test_no_mentioned_jid_key_leaves_the_text_unchanged(self):
        s = _Stub(_FakeMainWindow())
        quoted = {"conversation": "oi, tudo bem?"}
        assert s._get_quoted_preview(quoted) == "oi, tudo bem?"

    def test_the_locally_composed_extended_text_message_shape_still_works(self):
        """Our own just-sent reply, before it round-trips through slimming,
        still uses the extendedTextMessage shape — must keep resolving."""
        s = _Stub(
            _FakeMainWindow(),
            names={"5511111111111@s.whatsapp.net": "Ana"},
        )
        quoted = {
            "extendedTextMessage": {
                "text": "oi @5511111111111 bom dia",
                "contextInfo": {"mentionedJid": ["5511111111111@s.whatsapp.net"]},
            }
        }
        assert s._get_quoted_preview(quoted) == "oi @Ana bom dia"
