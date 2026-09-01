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
    AUTO_DOWNLOAD_MEDIA_TYPES,
    GROUP_MEDIA_TYPES,
    VOICE_MEDIA_TYPE_MIGRATION_FLAG,
    auto_download_allows,
    filter_group_media,
    group_media_category,
    migrate_voice_messages_media_types,
    DEFAULT_SETTINGS,
)


def _msg(msg_type, mid="m1"):
    return {"key": {"id": mid}, "messageType": msg_type, "message": {}}


def _voice(mid="v1"):
    """A voice note as WPPConnect delivers it: an audioMessage carrying ptt."""
    return {"key": {"id": mid}, "messageType": "audioMessage",
            "message": {"audioMessage": {"ptt": True, "seconds": 7}}}


class TestTheCategories:
    @pytest.mark.parametrize("msg_type,expected", [
        ("imageMessage", "photos"),
        ("stickerMessage", "photos"),
        ("videoMessage", "videos"),
        ("audioMessage", "audios"),
        ("ptt", "voice_messages"),
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

    def test_a_voice_note_is_its_own_category(self):
        """Users asked to filter one without the other, so a voice note is
        never "audios" — its messageType is "audioMessage" like any audio
        file, which is why is_voice_message() has to be consulted before the
        message-type table."""
        assert group_media_category(_voice()) == "voice_messages"

    def test_an_audio_file_is_not_a_voice_note(self):
        """The other direction, and the one a wrong ptt test breaks: an audio
        file must stay under "Áudios" alone."""
        audio = _msg("audioMessage")
        audio["message"] = {"audioMessage": {"ptt": False, "seconds": 200}}
        assert group_media_category(audio) == "audios"
        assert group_media_category(_msg("audioMessage")) == "audios"

    def test_a_stray_ptt_flag_does_not_move_a_photo(self):
        """is_voice_message() answers a top-level ptt/isPtt flag before it
        looks at the message type at all — correct where it is asked about a
        known audio, and a silent reclassification here: a photo filed under
        voice notes disappears from "Fotos" and is judged against the wrong
        auto-download checkbox."""
        assert group_media_category(
            {"messageType": "imageMessage", "message": {"imageMessage": {}},
             "ptt": True}) == "photos"
        assert group_media_category(
            {"messageType": "documentMessage", "isPtt": True}) == "documents"

    def test_junk_records_do_not_raise(self):
        assert group_media_category(None) == ""
        assert group_media_category("not a dict") == ""
        assert group_media_category({}) == ""


class TestTheFilter:
    def test_only_the_checked_categories_come_back(self):
        records = [_msg("imageMessage"), _msg("videoMessage"), _msg("documentMessage")]
        assert len(filter_group_media(records, ("photos",))) == 1
        assert len(filter_group_media(records, ("photos", "videos"))) == 2

    def test_voice_notes_and_audio_files_filter_apart(self):
        """The reason for the split: checking one box must not drag the other
        category in. Both directions, because a category that matched
        everything would pass a one-sided test."""
        records = [_msg("audioMessage", "a"), _voice("v")]
        assert [m["key"]["id"] for m
                in filter_group_media(records, ("audios",))] == ["a"]
        assert [m["key"]["id"] for m
                in filter_group_media(records, ("voice_messages",))] == ["v"]
        assert len(filter_group_media(records, ("audios", "voice_messages"))) == 2

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


class TestScanningWhatIsOnDisk:
    """Which files count as "baixada".

    The scan only looked in media/<id>.wzmedia, so a voice message — cached as
    voice_messages/<id>.msv by the audio download, i.e. by simply playing it in
    the app — was never in the downloaded set and the "Nao baixadas" filter
    listed audios the user had already listened to.
    """

    @staticmethod
    def _dirs(tmp_path, monkeypatch):
        import ui.dialogs.conversation_data_dialog as dlg

        media = tmp_path / "media"
        voice = tmp_path / "voice_messages"
        media.mkdir()
        voice.mkdir()
        monkeypatch.setattr(dlg, "data_path", lambda *parts: str(tmp_path.joinpath(*parts)))
        return voice, media

    def test_a_played_voice_message_counts_as_downloaded(self, tmp_path, monkeypatch):
        voice, _ = self._dirs(tmp_path, monkeypatch)
        (voice / "AUDIO1.msv").write_bytes(b"x")
        found = ConversationDataDialog._scan_downloaded_media(
            [_msg("audioMessage", "AUDIO1")])
        assert found == {"AUDIO1"}

    def test_a_cached_photo_still_counts(self, tmp_path, monkeypatch):
        _, media = self._dirs(tmp_path, monkeypatch)
        (media / "PIC1.wzmedia").write_bytes(b"x")
        found = ConversationDataDialog._scan_downloaded_media(
            [_msg("imageMessage", "PIC1")])
        assert found == {"PIC1"}

    def test_the_compound_id_is_unpacked_the_same_way_the_writer_does(
            self, tmp_path, monkeypatch):
        """The file is written under media_cache_id(), not the raw key id."""
        voice, _ = self._dirs(tmp_path, monkeypatch)
        (voice / "REAL.msv").write_bytes(b"x")
        found = ConversationDataDialog._scan_downloaded_media(
            [_msg("audioMessage", "false_5511999999999@c.us_REAL")])
        assert found == {"REAL"}

    def test_nothing_on_disk_yields_an_empty_set(self, tmp_path, monkeypatch):
        self._dirs(tmp_path, monkeypatch)
        found = ConversationDataDialog._scan_downloaded_media(
            [_msg("audioMessage", "AUDIO1"), _msg("imageMessage", "PIC1")])
        assert found == set()

    def test_the_downloaded_filter_then_keeps_that_audio(self, tmp_path, monkeypatch):
        voice, _ = self._dirs(tmp_path, monkeypatch)
        (voice / "AUDIO1.msv").write_bytes(b"x")
        records = [_msg("audioMessage", "AUDIO1"), _msg("audioMessage", "AUDIO2")]
        downloaded = ConversationDataDialog._scan_downloaded_media(records)
        out = filter_group_media_by_download(
            records, GROUP_MEDIA_FILTER_NOT_DOWNLOADED, downloaded)
        assert [m["key"]["id"] for m in out] == ["AUDIO2"]


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


# ── Selection/focus, and the silence around it ───────────────────────────────


class _FakeListCtrl:
    def __init__(self, count=0):
        self._count = count
        self.focused = []
        self.selected = []
        self.set_focus_calls = 0

    def GetItemCount(self):
        return self._count

    def Focus(self, idx):
        self.focused.append(idx)

    def Select(self, idx, on=True):
        self.selected.append((idx, on))

    def SetFocus(self):
        self.set_focus_calls += 1


class TestTheFirstRowIsAlwaysSelected:
    """With nothing selected, the context menu and Enter have no row to act
    on, and a screen reader arriving by Tab lands on "no selection" instead of
    on a message."""

    def test_row_zero_is_focused_and_selected(self):
        lst = _FakeListCtrl(count=5)
        ConversationDataDialog._focus_first(lst)
        assert lst.focused == [0]
        assert lst.selected == [(0, True)]

    def test_keyboard_focus_is_not_taken(self):
        """The user must keep whatever they were on — the filter radio, a
        checkbox — after a refresh reorders the list under them. Select/Focus
        move the item cursor; only SetFocus would move the caret."""
        lst = _FakeListCtrl(count=5)
        ConversationDataDialog._focus_first(lst)
        assert lst.set_focus_calls == 0

    def test_an_empty_list_is_left_alone(self):
        lst = _FakeListCtrl(count=0)
        ConversationDataDialog._focus_first(lst)
        assert lst.focused == [] and lst.selected == []


class TestRefreshingAlwaysRefocuses:
    """Every path that changes what the list holds — both checkbox routes, the
    filter radio, and the history landing from the background thread — goes
    through _refresh_media_list, so one call there covers all of them."""

    @staticmethod
    def _src(fn):
        import inspect
        return inspect.getsource(fn)

    def test_refresh_focuses_the_first_row(self):
        assert "self._focus_first(self._media_list)" in self._src(
            ConversationDataDialog._refresh_media_list)

    @pytest.mark.parametrize("method", [
        "_on_media_type_activated",
        "_on_media_type_toggled",
        "_on_media_filter_changed",
        "_on_media_history_loaded",
    ])
    def test_every_changing_path_refreshes(self, method):
        assert "_refresh_media_list()" in self._src(
            getattr(ConversationDataDialog, method))

    def test_the_types_list_starts_on_its_first_row_too(self):
        assert "self._focus_first(self._media_types_list)" in self._src(
            ConversationDataDialog._build_media_tab)


class TestTheCountIsNotSpoken:
    def test_refresh_does_not_announce_through_speak_output(self):
        """Changing a filter is exactly when the screen reader should be
        reading the list; a spoken count talked over it every time."""
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(
            inspect.getsource(ConversationDataDialog._refresh_media_list)))
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef) and node.body
                    and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)):
                node.body.pop(0)
        code = ast.unparse(tree)
        assert "speak_output" not in code


class TestTheSettingsListStartsOnItsFirstRow:
    """Same reason as the tab's own lists: with nothing selected, Enter and
    Space have no row to toggle, and a screen reader arriving by Tab announces
    an empty selection instead of the first media type."""

    @staticmethod
    def _src():
        import inspect
        from ui.dialogs.settings_dialog import SettingsDialog
        return inspect.getsource(SettingsDialog._build_ui)

    def test_it_focuses_and_selects_row_zero(self):
        src = self._src()
        assert "self._group_media_types_list.Focus(0)" in src
        assert "self._group_media_types_list.Select(0)" in src

    def test_it_does_not_take_the_keyboard_caret(self):
        """Opening the tab must not yank focus out of wherever the user was."""
        src = self._src()
        assert "self._group_media_types_list.SetFocus()" not in src

    def test_it_happens_after_the_rows_exist(self):
        src = self._src()
        appended = src.index("self._group_media_types_list.Append(")
        focused = src.index("self._group_media_types_list.Focus(0)")
        assert appended < focused


# ── Space, shortcuts and the action buttons ──────────────────────────────────

import inspect as _inspect
import types

import wx

from ui.dialogs.conversation_data_dialog import _MEDIA_MENU_ACTIONS
from ui.dialogs.settings_dialog import SettingsDialog


class _KeyEvent:
    def __init__(self, code, obj=None, ctrl=False, shift=False, alt=False):
        self._code, self._obj = code, obj
        self._ctrl, self._shift, self._alt = ctrl, shift, alt
        self.skipped = False

    def GetKeyCode(self):
        return self._code

    def GetEventObject(self):
        return self._obj

    def ControlDown(self):
        return self._ctrl

    def ShiftDown(self):
        return self._shift

    def AltDown(self):
        return self._alt

    def Skip(self):
        self.skipped = True


class _CheckList:
    def __init__(self, count=4, focused=1):
        self._count, self._focused = count, focused
        self.checked = {i: True for i in range(count)}

    def GetItemCount(self):
        return self._count

    def GetFocusedItem(self):
        return self._focused

    def IsItemChecked(self, i):
        return self.checked[i]

    def CheckItem(self, i, on=True):
        self.checked[i] = on


class TestSpaceTogglesTheCheckbox:
    """Space does NOT toggle a wx.ListCtrl checkbox on wxMSW — the native
    control treats it as a selection key and swallows it, so the box only ever
    moved with Enter. Reported from real use, not theory."""

    def _stub(self, dialog_cls, list_attr, refresh=None):
        stub = types.SimpleNamespace()
        lst = _CheckList()
        setattr(stub, list_attr, lst)
        stub._refresh_media_list = refresh or (lambda: None)
        stub._on_media_type_key_down = dialog_cls._on_media_type_key_down.__get__(stub)
        return stub, lst

    def test_space_flips_the_focused_row_in_the_media_tab(self):
        stub, lst = self._stub(ConversationDataDialog, "_media_types_list")
        stub._on_media_type_key_down(_KeyEvent(wx.WXK_SPACE, obj=lst))
        assert lst.checked[1] is False

    def test_space_flips_it_back(self):
        stub, lst = self._stub(ConversationDataDialog, "_media_types_list")
        stub._on_media_type_key_down(_KeyEvent(wx.WXK_SPACE, obj=lst))
        stub._on_media_type_key_down(_KeyEvent(wx.WXK_SPACE, obj=lst))
        assert lst.checked[1] is True

    def test_space_refreshes_the_list(self):
        calls = []
        stub, lst = self._stub(ConversationDataDialog, "_media_types_list",
                               refresh=lambda: calls.append(1))
        stub._on_media_type_key_down(_KeyEvent(wx.WXK_SPACE, obj=lst))
        assert calls == [1]

    def test_other_keys_are_passed_through(self):
        """Arrows must keep moving through the list."""
        stub, lst = self._stub(ConversationDataDialog, "_media_types_list")
        ev = _KeyEvent(wx.WXK_DOWN, obj=lst)
        stub._on_media_type_key_down(ev)
        assert ev.skipped is True
        assert lst.checked[1] is True

    def test_the_settings_list_gets_the_same_treatment(self):
        stub = types.SimpleNamespace()
        lst = _CheckList()
        stub._group_media_types_list = lst
        stub._on_media_type_key_down = SettingsDialog._on_media_type_key_down.__get__(stub)
        stub._on_media_type_key_down(_KeyEvent(wx.WXK_SPACE, obj=lst))
        assert lst.checked[1] is False


class TestTheMenuAdvertisesWorkingShortcuts:
    """A menu that shows an accelerator which does nothing is worse than one
    that shows none — and that is what this tab shipped with: the conversation
    panel reaches these through its own accelerator table, which this dialog is
    not part of."""

    def test_every_action_carries_a_shortcut(self):
        for label_key, shortcut, method in _MEDIA_MENU_ACTIONS:
            assert shortcut, label_key

    def test_the_shortcuts_match_the_conversation_menu(self):
        """Same keys the message list already uses, so nothing has to be
        relearned for the same action in a different place."""
        expected = {
            "reply_message": "Alt+R",
            "forward_message": "Ctrl+Shift+E",
            "react_to_message": "Ctrl+Shift+R",
            "copy_message_text": "Ctrl+C",
            "star_message": "Ctrl+Shift+O",
            "message_data": "Alt+Shift+D",
        }
        assert {k: s for k, s, _ in _MEDIA_MENU_ACTIONS} == expected

    def test_each_one_names_a_real_panel_handler(self):
        from ui.conversations import ConversationsPanel
        for _label, _shortcut, method in _MEDIA_MENU_ACTIONS:
            assert hasattr(ConversationsPanel, method), method

    def test_the_menu_builds_its_labels_from_that_table(self):
        src = _inspect.getsource(ConversationDataDialog._on_media_context_menu)
        assert "_MEDIA_MENU_ACTIONS" in src

    def test_the_key_handler_covers_every_advertised_shortcut(self):
        src = _inspect.getsource(ConversationDataDialog._on_media_list_key_down)
        for _label, _shortcut, method in _MEDIA_MENU_ACTIONS:
            assert method in src, f"{method} is advertised but not bound"


class TestTheActionButtons:
    """Open / Save As as real buttons, in the Tab order right after the list —
    the same affordance the conversation panel gives a media message. A context
    menu is not equivalent: it needs a right-click or the menu key, and never
    appears in the Tab order."""

    @staticmethod
    def _build_src():
        return _inspect.getsource(ConversationDataDialog._build_media_tab)

    def test_both_buttons_exist(self):
        src = self._build_src()
        assert "self._media_open_btn = wx.Button" in src
        assert "self._media_save_btn = wx.Button" in src

    def test_they_sit_between_the_list_and_the_type_checkboxes(self):
        src = self._build_src()
        assert src.index("self._media_list = wx.ListCtrl") < src.index(
            "self._media_open_btn")
        assert src.index("self._media_save_btn") < src.index(
            "self._media_types_list = wx.ListCtrl")

    def test_they_are_disabled_for_a_link(self):
        """A link has no file to open or save; a dead control in the Tab order
        is worse than an absent one."""
        src = _inspect.getsource(ConversationDataDialog._on_media_row_focused)
        assert 'group_media_category(msg) != "links"' in src
        assert "btn.Enable(actionable)" in src

    def test_open_uses_the_panels_by_message_handler(self):
        """By message, not by row index: this tab reads the group's whole
        history from the database, so most of what it lists is outside the
        ~200 messages the panel keeps — resolving those through a list index
        found nothing and the button silently did nothing."""
        src = _inspect.getsource(ConversationDataDialog._on_media_open_btn)
        assert "open_media_message" in src
        assert "_invoke_with_index" not in src

    def test_save_uses_the_panels_by_message_handler(self):
        src = _inspect.getsource(ConversationDataDialog._on_media_save_btn)
        assert "save_media_message" in src
        assert "_invoke_with_selection" not in src

    def test_save_as_announces_its_shortcut(self):
        """The panel's Save-As button reports Ctrl+Shift+S to the screen
        reader; the shortcut works here too, so it is announced here too."""
        src = _inspect.getsource(ConversationDataDialog._build_media_tab)
        assert "self._media_save_btn.SetAccessible(AccessibleSaveAs())" in src


class TestDeletingFromTheMediaTab:
    """Browsing a group's media is exactly when someone finds the thing they
    want gone, so delete belongs here — unlike bulk-select and "read more",
    which do not.

    It runs the panel's own _on_menu_delete_message, so the scope dialog (for
    me / for everyone) and every rule behind it — no "for everyone" on a system
    event or in the self-chat, the group-admin path — behave exactly as they do
    in the conversation.
    """

    @staticmethod
    def _src(fn):
        return _inspect.getsource(fn)

    def test_it_delegates_to_the_panels_handler(self):
        src = self._src(ConversationDataDialog._delete_selected_media)
        assert "_on_menu_delete_message" in src

    def test_the_menu_offers_it_with_the_same_shortcut(self):
        src = self._src(ConversationDataDialog._on_media_context_menu)
        # Raw string: the source carries a literal backslash-t, the same way
        # the conversation menu writes its accelerators.
        assert r"i18n.t('delete_message')" in src
        # Built from the conversation menu's own label instead of a
        # hand-escaped literal — the point is that the two agree, and an
        # escaped tab in the test itself is how this assertion got written
        # wrong twice.
        expected = chr(92) + 'tDelete'
        assert expected in src, 'media menu is missing the Delete accelerator'

    def test_the_delete_key_triggers_it(self):
        src = self._src(ConversationDataDialog._on_media_list_key_down)
        assert "wx.WXK_DELETE" in src
        assert "_delete_selected_media" in src

    def test_the_delete_key_is_unmodified(self):
        """Ctrl+Delete and friends belong to whatever else may claim them."""
        src = self._src(ConversationDataDialog._on_media_list_key_down)
        assert "code == wx.WXK_DELETE and not (ctrl or shift or alt)" in src

    def test_a_cancelled_delete_does_not_refresh(self):
        """The scope dialog can be dismissed; rebuilding the list then would
        throw the user's place in it away for nothing."""
        src = self._src(ConversationDataDialog._delete_selected_media)
        assert "if not was_listed or _still_listed():" in src
        assert src.index("_still_listed()") < src.index("_refresh_media_list()")

    def test_the_deleted_message_is_dropped_from_the_history_snapshot(self):
        """_media_history is a DB read from when the tab loaded — it has no
        idea a message was deleted, so the row would come back on the next
        filter change and read as "the delete did not work"."""
        src = self._src(ConversationDataDialog._delete_selected_media)
        assert "self._media_history = [" in src
        assert 'if (m.get("key") or {}).get("id", "") != msg_id' in src

    def test_it_is_withheld_when_the_panel_is_on_another_chat(self):
        """Index-based, like Open — it would delete from whatever conversation
        the panel currently holds."""
        src = self._src(ConversationDataDialog._on_media_context_menu)
        # Anchored on the guard itself, not on a position relative to the
        # label — the first "delete_message" in this source is the handler's
        # own name, which sits after the guard, not before it.
        assert 'hasattr(panel, "_on_menu_delete_message") and on_this_chat' in src

    def test_the_guard_is_also_in_the_method_itself(self):
        """The keyboard path does not go through the menu's guard."""
        src = self._src(ConversationDataDialog._delete_selected_media)
        assert "self._panel_is_on_this_chat()" in src


class TestAutoDownloadAllows:
    """core.utils.auto_download_allows() — the pure half of Configuracoes >
    Armazenamento > "Tipos de midia a serem baixados automaticamente".

    Built on group_media_category() so the Media tab and this setting group
    messages identically: unchecking "Fotos" has to mean the same thing in
    both places, stickers included.
    """

    @staticmethod
    def _settings(*allowed):
        return {"storage": {"auto_download_media_types": list(allowed)}}

    @staticmethod
    def _media(message_type):
        return {"key": {"id": "x"}, "messageType": message_type}

    def test_a_checked_category_is_allowed(self):
        assert auto_download_allows(
            self._settings("photos"), self._media("imageMessage")) is True

    def test_an_unchecked_category_is_not(self):
        assert auto_download_allows(
            self._settings("videos"), self._media("imageMessage")) is False

    def test_voice_notes_and_audio_files_are_chosen_separately(self):
        """A user who wants voice notes fetched but not the 30 MB music file
        someone forwarded — the reason "audios" was split in the first place."""
        voice = _voice()
        audio = self._media("audioMessage")
        only_voice = self._settings("voice_messages")
        only_audio = self._settings("audios")
        assert auto_download_allows(only_voice, voice) is True
        assert auto_download_allows(only_voice, audio) is False
        assert auto_download_allows(only_audio, audio) is True
        assert auto_download_allows(only_audio, voice) is False

    def test_no_setting_at_all_allows_everything(self):
        """What a settings.json predating the option looks like. Reading it as
        "nothing selected" would stop every media download for existing
        installs."""
        for settings in ({}, {"storage": {}}, None, "texto",
                         {"storage": {"auto_download_media_types": None}}):
            assert auto_download_allows(
                settings, self._media("imageMessage")) is True

    def test_an_explicitly_empty_list_blocks_everything(self):
        assert auto_download_allows(
            self._settings(), self._media("videoMessage")) is False

    def test_a_link_is_never_blocked_by_this_setting(self):
        """Links are deliberately not a category here: a link is a text
        message with no file behind it and never reaches the download path at
        all. This check must not be the thing that skips it."""
        link_msg = {"key": {"id": "x"}, "messageType": "conversation",
                    "message": {"conversation": "veja https://exemplo.com"}}
        assert auto_download_allows(self._settings(), link_msg) is True

    def test_a_message_in_no_category_is_left_alone(self):
        plain = {"key": {"id": "x"}, "messageType": "conversation",
                 "message": {"conversation": "oi"}}
        assert auto_download_allows(self._settings(), plain) is True

    def test_links_are_not_offered_as_a_category(self):
        assert "links" not in AUTO_DOWNLOAD_MEDIA_TYPES

    def test_the_categories_track_the_media_tab(self):
        """Derived from GROUP_MEDIA_TYPES rather than written out, so a new
        category cannot silently become undownloadable."""
        assert set(AUTO_DOWNLOAD_MEDIA_TYPES) == set(GROUP_MEDIA_TYPES) - {"links"}


class TestTheVoiceMessagesMigration:
    """core.utils.migrate_voice_messages_media_types().

    "audios" used to mean audio files AND voice notes. Splitting it in two
    would, read literally, leave every existing settings.json with the new box
    unchecked — voice notes silently gone from the Media tab and never
    auto-downloaded, on the first launch after an update nobody asked for.
    """

    @staticmethod
    def _settings(ui_types=..., storage_types=..., **general):
        settings = {"user_interface": {}, "storage": {}, "general": dict(general)}
        if ui_types is not ...:
            settings["user_interface"]["group_media_default_types"] = ui_types
        if storage_types is not ...:
            settings["storage"]["auto_download_media_types"] = storage_types
        return settings

    def test_a_checked_audios_box_ticks_voice_messages_too(self):
        settings = self._settings(["photos", "audios"], ["audios", "documents"])
        assert migrate_voice_messages_media_types(settings) is True
        assert settings["user_interface"]["group_media_default_types"] == [
            "photos", "audios", "voice_messages"]
        assert settings["storage"]["auto_download_media_types"] == [
            "audios", "voice_messages", "documents"]

    def test_the_rebuilt_list_is_in_category_order(self):
        """So a migrated file is indistinguishable from one the dialogs saved."""
        settings = self._settings(["links", "audios", "photos"])
        migrate_voice_messages_media_types(settings)
        saved = settings["user_interface"]["group_media_default_types"]
        assert saved == [k for k in GROUP_MEDIA_TYPES if k in saved]

    def test_an_empty_list_stays_empty(self):
        """Unchecking everything is a real choice, and "nothing" never meant
        "voice notes as well"."""
        settings = self._settings([], [])
        migrate_voice_messages_media_types(settings)
        assert settings["user_interface"]["group_media_default_types"] == []
        assert settings["storage"]["auto_download_media_types"] == []

    def test_a_list_without_audios_does_not_gain_voice_messages(self):
        settings = self._settings(["photos", "videos"])
        migrate_voice_messages_media_types(settings)
        assert settings["user_interface"]["group_media_default_types"] == [
            "photos", "videos"]

    def test_a_missing_or_corrupt_value_is_left_alone(self):
        """Every reader treats those as "all categories" already; writing a
        list here would turn an un-chosen default into a saved choice."""
        settings = self._settings()
        assert migrate_voice_messages_media_types(settings) is True
        assert settings["user_interface"] == {}
        corrupt = self._settings("audios")
        migrate_voice_messages_media_types(corrupt)
        assert corrupt["user_interface"]["group_media_default_types"] == "audios"

    def test_it_only_runs_once(self):
        """A list holding "audios" without "voice_messages" is byte-identical
        before the migration and after the user unchecks "Mensagens de voz" —
        only the flag tells them apart, so without it every launch would
        re-tick the box the user just unticked."""
        settings = self._settings(["audios"])
        migrate_voice_messages_media_types(settings)
        settings["user_interface"]["group_media_default_types"] = ["audios"]
        assert migrate_voice_messages_media_types(settings) is False
        assert settings["user_interface"]["group_media_default_types"] == ["audios"]

    def test_it_records_that_it_ran(self):
        settings = self._settings(["audios"])
        migrate_voice_messages_media_types(settings)
        assert settings["general"][VOICE_MEDIA_TYPE_MIGRATION_FLAG] is True

    def test_junk_settings_are_refused(self):
        assert migrate_voice_messages_media_types(None) is False
        assert migrate_voice_messages_media_types("texto") is False
        assert migrate_voice_messages_media_types({}) is True

    def test_the_loader_runs_it_and_saves(self):
        """A migration whose result never reaches disk runs again next launch,
        undoing whatever the user changed in between."""
        import inspect
        from main import MainWindow
        src = inspect.getsource(MainWindow._migrate_settings)
        assert "migrate_voice_messages_media_types(self.settings)" in src
        assert "self.save_settings()" in src
