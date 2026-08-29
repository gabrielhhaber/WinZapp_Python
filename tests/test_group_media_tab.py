"""Tests for the group data dialog's Media tab and its settings.

The tab existed as a placeholder: a StaticText that went from "loading" to a
count of media files whose bytes happened to already be cached on disk. On a
fresh install nothing is pre-cached, so it read "0" and looked broken — which
is what it was, not a deliberately empty tab.

It now lists the group's media messages, filtered by four checkboxes, and its
rows are rendered by ConversationsPanel._render_message_line() so a media row
reads exactly like the same row in the conversation. The filter itself is pure
and lives in core.utils, which is what these tests can reach — the tab is a
wx.Panel.
"""

import pytest

from core.utils import (
    GROUP_MEDIA_TYPES,
    filter_group_media,
    group_media_category,
    DEFAULT_SETTINGS,
)


def _msg(msg_type, mid="m1"):
    return {"key": {"id": mid}, "messageType": msg_type, "message": {}}


class TestTheCategories:
    @pytest.mark.parametrize("msg_type,expected", [
        ("imageMessage", "photos"),
        ("stickerMessage", "photos"),
        ("videoMessage", "videos"),
        ("audioMessage", "audios"),
        ("ptt", "audios"),
        ("documentMessage", "documents"),
    ])
    def test_media_types_map_to_a_category(self, msg_type, expected):
        assert group_media_category(_msg(msg_type)) == expected

    @pytest.mark.parametrize("msg_type", [
        "conversation", "extendedTextMessage", "groupNotification",
        "protocolMessage", "contactMessage", "",
    ])
    def test_non_media_has_no_category(self, msg_type):
        assert group_media_category(_msg(msg_type)) == ""

    def test_a_voice_note_counts_as_audio(self):
        """Voice notes and audio files are one thing to someone looking for
        "the audio someone sent" — splitting them would leave a voice note
        invisible under any single box."""
        ptt = _msg("audioMessage")
        ptt["message"] = {"audioMessage": {"ptt": True}}
        assert group_media_category(ptt) == "audios"

    def test_junk_records_do_not_raise(self):
        assert group_media_category(None) == ""
        assert group_media_category("not a dict") == ""
        assert group_media_category({}) == ""


class TestTheFilter:
    def test_only_the_checked_categories_come_back(self):
        records = [_msg("imageMessage"), _msg("videoMessage"), _msg("documentMessage")]
        assert len(filter_group_media(records, ("photos",))) == 1
        assert len(filter_group_media(records, ("photos", "videos"))) == 2

    def test_text_is_excluded_whatever_is_checked(self):
        """The tab is a media browser, not a filtered conversation."""
        records = [_msg("conversation"), _msg("extendedTextMessage"), _msg("imageMessage")]
        out = filter_group_media(records, GROUP_MEDIA_TYPES)
        assert len(out) == 1
        assert out[0]["messageType"] == "imageMessage"

    def test_nothing_checked_yields_nothing(self):
        records = [_msg("imageMessage"), _msg("videoMessage")]
        assert filter_group_media(records, ()) == []

    def test_order_is_preserved(self):
        """Rows must read in the same order the message list uses them."""
        records = [_msg("imageMessage", "a"), _msg("videoMessage", "b"),
                   _msg("imageMessage", "c")]
        out = filter_group_media(records, GROUP_MEDIA_TYPES)
        assert [m["key"]["id"] for m in out] == ["a", "b", "c"]

    def test_empty_and_none_inputs_are_safe(self):
        assert filter_group_media([], GROUP_MEDIA_TYPES) == []
        assert filter_group_media(None, GROUP_MEDIA_TYPES) == []
        assert filter_group_media([_msg("imageMessage")], None) == []


class TestTheDefaultSetting:
    def test_every_category_starts_enabled(self):
        saved = DEFAULT_SETTINGS["user_interface"]["group_media_default_types"]
        assert set(saved) == set(GROUP_MEDIA_TYPES)

    def test_the_default_is_not_shared_with_the_constant(self):
        """A shared list would let one account's edit mutate the template and
        leak into every other account backfilled afterwards."""
        saved = DEFAULT_SETTINGS["user_interface"]["group_media_default_types"]
        assert saved is not GROUP_MEDIA_TYPES


# ── The dialog's own pure-enough methods ─────────────────────────────────────
#
# ConversationDataDialog is a wx.Dialog and cannot be instantiated without a
# running wx.App, so these three are bound to a plain stub carrying only what
# each one reads — the pattern the rest of the suite uses.

from ui.dialogs.conversation_data_dialog import ConversationDataDialog


class _MW:
    def __init__(self, settings=None, panel=None):
        self.settings = settings if settings is not None else {}
        self.conversations_panel = panel

    def _normalize_jid(self, jid):
        return (jid or "").replace("@c.us", "@s.whatsapp.net")


class _Panel:
    def __init__(self, conversation=None, sorted_messages=()):
        self.conversation = conversation
        self._sorted_messages = list(sorted_messages)


class _DialogStub:
    _default_media_types  = ConversationDataDialog._default_media_types
    _panel_index_for      = ConversationDataDialog._panel_index_for
    _panel_is_on_this_chat = ConversationDataDialog._panel_is_on_this_chat
    _conversation_panel   = ConversationDataDialog._conversation_panel

    def __init__(self, mw, jid="123@g.us"):
        self._mw = mw
        self._jid = jid


class TestTheDefaultsComeFromSettings:
    def test_a_saved_subset_is_honoured(self):
        mw = _MW({"user_interface": {"group_media_default_types": ["videos", "photos"]}})
        # Returned in GROUP_MEDIA_TYPES order, not the order they were saved in.
        assert _DialogStub(mw)._default_media_types() == ["photos", "videos"]

    def test_a_missing_key_means_everything(self):
        """The migration case, and the one that hits every existing install at
        once: a settings.json written before this option existed has no key.
        Reading that as "nothing" would open the tab permanently empty."""
        assert _DialogStub(_MW({}))._default_media_types() == list(GROUP_MEDIA_TYPES)
        assert _DialogStub(_MW({"user_interface": {}}))._default_media_types() == \
            list(GROUP_MEDIA_TYPES)

    def test_a_corrupt_value_means_everything(self):
        mw = _MW({"user_interface": {"group_media_default_types": "photos"}})
        assert _DialogStub(mw)._default_media_types() == list(GROUP_MEDIA_TYPES)

    def test_an_explicitly_empty_list_is_honoured(self):
        """Unchecking everything is a choice the user is allowed to make, and
        must not be silently read back as "all"."""
        mw = _MW({"user_interface": {"group_media_default_types": []}})
        assert _DialogStub(mw)._default_media_types() == []

    def test_unknown_categories_are_dropped(self):
        mw = _MW({"user_interface": {"group_media_default_types": ["photos", "bogus"]}})
        assert _DialogStub(mw)._default_media_types() == ["photos"]


class TestLocatingTheMessageInThePanel:
    """The index handed to the panel's index-based handlers. Getting this wrong
    means acting on somebody else's message."""

    def test_the_same_dict_object_is_found_by_identity(self):
        a, b = _msg("imageMessage", "a"), _msg("videoMessage", "b")
        mw = _MW(panel=_Panel(sorted_messages=[a, b]))
        assert _DialogStub(mw)._panel_index_for(b) == 1

    def test_a_rebuilt_list_is_matched_by_message_id(self):
        """populate_messages() replaces the dicts, so identity stops holding
        while the row is still the same message."""
        original = _msg("imageMessage", "same-id")
        rebuilt = _msg("imageMessage", "same-id")
        mw = _MW(panel=_Panel(sorted_messages=[_msg("videoMessage", "x"), rebuilt]))
        assert _DialogStub(mw)._panel_index_for(original) == 1

    def test_a_message_that_is_gone_reports_minus_one(self):
        mw = _MW(panel=_Panel(sorted_messages=[_msg("videoMessage", "x")]))
        assert _DialogStub(mw)._panel_index_for(_msg("imageMessage", "y")) == -1

    def test_a_message_with_no_id_is_not_guessed_at(self):
        orphan = {"messageType": "imageMessage", "key": {}}
        mw = _MW(panel=_Panel(sorted_messages=[_msg("imageMessage", "x")]))
        assert _DialogStub(mw)._panel_index_for(orphan) == -1

    def test_no_panel_reports_minus_one(self):
        assert _DialogStub(_MW())._panel_index_for(_msg("imageMessage")) == -1


class TestWhetherThePanelIsOnThisChat:
    """Index-based actions run against the panel's own list, so they are only
    offered when that list is this chat's."""

    def test_true_when_the_panel_holds_this_chat(self):
        mw = _MW(panel=_Panel(conversation={"remoteJid": "123@g.us"}))
        assert _DialogStub(mw, "123@g.us")._panel_is_on_this_chat() is True

    def test_false_when_the_panel_holds_another_chat(self):
        """The dialog is also reachable from the conversation LIST, for a chat
        the user never opened."""
        mw = _MW(panel=_Panel(conversation={"remoteJid": "999@g.us"}))
        assert _DialogStub(mw, "123@g.us")._panel_is_on_this_chat() is False

    def test_false_when_no_conversation_is_open(self):
        mw = _MW(panel=_Panel(conversation=None))
        assert _DialogStub(mw, "123@g.us")._panel_is_on_this_chat() is False

    def test_false_when_there_is_no_panel_at_all(self):
        assert _DialogStub(_MW(), "123@g.us")._panel_is_on_this_chat() is False

    def test_jid_forms_are_normalized_before_comparing(self):
        """@c.us and @s.whatsapp.net are the same chat — comparing raw would
        withhold every action for a chat that IS open."""
        mw = _MW(panel=_Panel(conversation={"remoteJid": "5511999999999@c.us"}))
        stub = _DialogStub(mw, "5511999999999@s.whatsapp.net")
        assert stub._panel_is_on_this_chat() is True


# ── Links, and the downloaded/not-downloaded filter ──────────────────────────

from core.utils import (
    GROUP_MEDIA_FILTER_ALL,
    GROUP_MEDIA_FILTER_DOWNLOADED,
    GROUP_MEDIA_FILTER_NOT_DOWNLOADED,
    filter_group_media_by_download,
    media_cache_id,
    message_has_link,
)


def _text(body, mid="t1", msg_type="conversation"):
    inner = ({"conversation": body} if msg_type == "conversation"
             else {"extendedTextMessage": {"text": body}})
    return {"key": {"id": mid}, "messageType": msg_type, "message": inner}


class TestLinksAsACategory:
    @pytest.mark.parametrize("body", [
        "olha isso https://exemplo.com/a",
        "www.exemplo.com",
        "http://exemplo.com",
        "texto antes https://exemplo.com texto depois",
    ])
    def test_a_text_message_with_a_url_is_a_link(self, body):
        assert group_media_category(_text(body)) == "links"

    def test_an_extended_text_message_counts_too(self):
        assert group_media_category(
            _text("veja https://exemplo.com", msg_type="extendedTextMessage")
        ) == "links"

    def test_plain_text_is_still_not_media(self):
        assert group_media_category(_text("bom dia, tudo certo?")) == ""

    def test_a_photo_whose_caption_has_a_url_stays_a_photo(self):
        """Its own media type decides first. Counting it under both boxes
        would make them overlap, and someone looking for "the link" is looking
        for the text message."""
        photo = _msg("imageMessage", "p1")
        photo["message"] = {"imageMessage": {"caption": "veja https://exemplo.com"}}
        assert group_media_category(photo) == "photos"

    def test_links_is_one_of_the_categories(self):
        assert "links" in GROUP_MEDIA_TYPES

    def test_the_filter_can_select_links_alone(self):
        records = [_text("https://a.com"), _msg("imageMessage"), _text("sem url")]
        out = filter_group_media(records, ("links",))
        assert len(out) == 1

    def test_message_has_link_reads_the_wire_text_only(self):
        assert message_has_link(_text("https://a.com")) is True
        assert message_has_link(_text("nada aqui")) is False
        assert message_has_link({}) is False
        assert message_has_link(None) is False


class TestTheCacheId:
    def test_a_plain_id_is_used_as_is(self):
        assert media_cache_id(_msg("imageMessage", "abc")) == "abc"

    def test_a_compound_id_yields_the_third_part(self):
        assert media_cache_id(_msg("imageMessage", "true_5511@g.us_REAL")) == "REAL"

    def test_a_two_part_id_yields_the_last(self):
        assert media_cache_id(_msg("imageMessage", "true_REAL")) == "REAL"

    def test_junk_is_safe(self):
        assert media_cache_id(None) == ""
        assert media_cache_id({}) == ""


class TestTheDownloadFilter:
    def _records(self):
        return [
            _msg("imageMessage", "have"),      # on disk
            _msg("videoMessage", "missing"),   # not on disk
            _text("https://a.com", "link1"),   # a link: nothing to download
        ]

    def test_all_keeps_everything(self):
        out = filter_group_media_by_download(
            self._records(), GROUP_MEDIA_FILTER_ALL, {"have"})
        assert len(out) == 3

    def test_downloaded_keeps_only_what_is_on_disk(self):
        out = filter_group_media_by_download(
            self._records(), GROUP_MEDIA_FILTER_DOWNLOADED, {"have"})
        assert [m["key"]["id"] for m in out] == ["have"]

    def test_not_downloaded_includes_links(self):
        """The third option is named "nao baixadas / links" — a link has no
        file to download, so it belongs on that side."""
        out = filter_group_media_by_download(
            self._records(), GROUP_MEDIA_FILTER_NOT_DOWNLOADED, {"have"})
        assert [m["key"]["id"] for m in out] == ["missing", "link1"]

    def test_a_link_is_never_counted_as_downloaded(self):
        """Even if its id happens to collide with a cached file's name."""
        out = filter_group_media_by_download(
            [_text("https://a.com", "link1")],
            GROUP_MEDIA_FILTER_DOWNLOADED, {"link1"})
        assert out == []

    def test_an_empty_downloaded_set_is_safe(self):
        out = filter_group_media_by_download(
            self._records(), GROUP_MEDIA_FILTER_DOWNLOADED, None)
        assert out == []
