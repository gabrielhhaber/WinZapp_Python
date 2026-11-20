"""Editing a caption from WinZapp, where WhatsApp Web itself allows it.

Read out of the running WhatsApp Web (WAWebMsgActionCapability): a caption is
editable under the same rules as text — own, not forwarded, delivered, inside
the edit window, created by this same client — plus two of its own: the
message is an image, video or document that ALREADY has a caption, and it is
not view-once. Its UI offers "Editar" for those. WinZapp offered edits for text
only.

Cross-device editing (a remote config, off for this account) is deliberately
not forced, so a caption on media sent from the phone is still refused by
WhatsApp; that refusal is reported and rolled back by the path pinned in
tests/test_edit_window_and_rollback.py.
"""

import copy
import time

import pytest

from core.message_edit import edit_kind
from ui.conversations import ConversationsPanel
from tests.god_modules import patch_conversations_global


def _media(kind="imageMessage", caption="legenda", from_me=True, age=60, **extra):
    media = {"caption": caption, "url": "https://mmg.example/x", "mediaKey": "KEY"}
    media.update(extra)
    return {
        "key": {"id": "m1", "fromMe": from_me},
        "messageType": kind,
        "message": {kind: media},
        "messageTimestamp": int(time.time()) - age,
    }


class TestEditKind:
    @pytest.mark.parametrize("kind", ["imageMessage", "videoMessage", "documentMessage"])
    def test_own_media_with_a_caption_is_a_caption_edit(self, kind):
        assert edit_kind(_media(kind)) == "caption"

    def test_text_is_a_text_edit(self):
        msg = {"key": {"fromMe": True}, "messageType": "conversation",
               "message": {"conversation": "oi"}}
        assert edit_kind(msg) == "text"

    @pytest.mark.parametrize("caption", ["", "   ", None])
    def test_a_caption_cannot_be_added_where_there_is_none(self, caption):
        assert edit_kind(_media(caption=caption)) is None

    def test_someone_elses_media_is_not_editable(self):
        assert edit_kind(_media(from_me=False)) is None

    def test_forwarded_media_is_not_editable(self):
        msg = _media(contextInfo={"isForwarded": True})
        assert edit_kind(msg) is None

    @pytest.mark.parametrize("kind", ["audioMessage", "stickerMessage"])
    def test_other_media_kinds_are_not_editable(self, kind):
        msg = {"key": {"fromMe": True}, "messageType": kind, "message": {kind: {"caption": "x"}}}
        assert edit_kind(msg) is None


# ── ConversationsPanel, bound onto a stub ───────────────────────────────────

class _I18n:
    def t(self, key):
        return key + ("{minutes}" if key == "edit_window_expired" else "")


class _List:
    def __init__(self):
        self.texts = {}

    def GetFirstSelected(self):
        return 0

    def GetFocusedItem(self):
        return -1

    def SetItemText(self, idx, text):
        self.texts[idx] = text


class _Field:
    def __init__(self):
        self.value = None

    def SetValue(self, value):
        self.value = value

    def SetInsertionPointEnd(self):
        pass

    def SetFocus(self):
        pass


class _Shown:
    def Show(self):
        pass

    def Layout(self):
        pass


class _MainWindow:
    def __init__(self, messages, edit_result=True):
        self.i18n = _I18n()
        self.spoken = []
        self.saves = []
        self.edit_calls = []
        self.mention_calls = []
        self.edit_result = edit_result
        self.records = messages

    def output(self, text, interrupt=False):
        self.spoken.append(text)

    def play_edit_saved_sound(self, remote_jid):
        pass

    def edit_message(self, remote_jid, message_id, new_text, mentioned_jids=None):
        self.edit_calls.append((message_id, new_text))
        self.mention_calls.append(mentioned_jids)
        return self.edit_result

    def get_chat(self, jid):
        return {"messages": {"messages": {"records": self.records}}}

    def _schedule_save(self, dirty_jid=None):
        self.saves.append(dirty_jid)

    def _schedule_set_chats(self):
        pass


class _Panel:
    _on_accel_edit_message = ConversationsPanel._on_accel_edit_message
    _on_menu_edit_message = ConversationsPanel._on_menu_edit_message
    _apply_message_edit = ConversationsPanel._apply_message_edit
    _send_message_edit = ConversationsPanel._send_message_edit
    _rollback_message_edit = ConversationsPanel._rollback_message_edit
    _message_own_links = ConversationsPanel._message_own_links

    def __init__(self, messages, edit_result=True):
        self._sorted_messages = messages
        self.main_window = _MainWindow(messages, edit_result)
        self.messages_list = _List()
        self.message_field = _Field()
        self._cancel_edit_btn = _Shown()
        self.conversation_panel = _Shown()
        self._editing_message_id = None
        self._editing_message_index = -1
        self._pending_mentions = []
        self._pending_mention_display_names = {}

    def _is_separator(self, msg):
        return False

    def _get_message_content(self, msg):
        return "Foto, legenda, 20 KB"  # what the LIST reads — must not be pre-filled

    def _raw_mentioned_jids(self, msg):
        return []

    def _rebuild_mention_pills(self):
        pass

    def _build_mention_payload(self, text):
        return text, None

    def _render_message_line(self, msg, index=None, total=None, include_quoted_preview=True):
        body = msg.get("message") or {}
        return (body.get("imageMessage") or {}).get("caption", "")

    def _extract_links(self, line):
        return []

    def _extract_mentions(self, msg):
        return []

    def _update_links_panel(self, links):
        pass

    def _update_mentions_panel(self, mentions):
        pass

    def _on_cancel_edit(self):
        pass


class _InlineThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._run = lambda: target(*args, **(kwargs or {}))

    def start(self):
        self._run()


@pytest.fixture(autouse=True)
def _inline(monkeypatch):
    monkeypatch.setattr("ui.conversations.threading.Thread", _InlineThread)
    monkeypatch.setattr("ui.conversations.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    patch_conversations_global(monkeypatch, "to_editor_line_endings", lambda s: s)


class TestAltEOnACaption:
    def test_enters_edit_mode_with_the_raw_caption(self):
        panel = _Panel([_media(caption="livro de matemática")])

        panel._on_accel_edit_message(None)

        assert panel._editing_message_id == "m1"
        assert panel.message_field.value == "livro de matemática"
        assert panel.main_window.spoken == []

    def test_past_the_window_says_so(self):
        panel = _Panel([_media(age=59 * 60)])
        panel._on_accel_edit_message(None)
        assert panel._editing_message_id is None
        assert panel.main_window.spoken == ["edit_window_expired15"]

    def test_media_without_a_caption_stays_silent(self):
        panel = _Panel([_media(caption="")])
        panel._on_accel_edit_message(None)
        assert panel._editing_message_id is None and panel.main_window.spoken == []


class TestApplyingACaptionEdit:
    def test_only_the_caption_changes(self):
        msg = _media(caption="antes", seconds=12)
        panel = _Panel([msg])
        panel._editing_message_id = "m1"

        panel._apply_message_edit("depois", "grupo@g.us")

        media = msg["message"]["imageMessage"]
        assert media["caption"] == "depois"
        assert (media["url"], media["mediaKey"], media["seconds"]) == ("https://mmg.example/x", "KEY", 12)
        assert msg["messageType"] == "imageMessage"
        assert msg["_edited"] is True
        assert panel.main_window.edit_calls == [("m1", "depois")]

    def test_a_refusal_puts_the_old_caption_back(self):
        """Media sent from the phone: WhatsApp refuses, WinZapp restores."""
        msg = _media(caption="antes")
        original = copy.deepcopy(msg)
        panel = _Panel([msg], edit_result=False)
        panel._editing_message_id = "m1"

        panel._apply_message_edit("depois", "grupo@g.us")

        assert msg == original
        assert panel.main_window.spoken == ["edit_message_failed"]

    def test_a_mention_never_puts_a_phone_number_in_the_caption(self):
        """Only text rows resolve "@<phone>" to a name, so a caption stored
        with the mention payload read the raw number aloud. The caption is sent
        and kept exactly as typed, with no mention list."""
        # A stale mention list in both places one can live: inside the media
        # body (normalised from sync) and top-level (local sends).
        msg = _media(caption="antes", contextInfo={"mentionedJid": ["5511888888888@s.whatsapp.net"]})
        msg["contextInfo"] = {"mentionedJid": ["5511888888888@s.whatsapp.net"]}
        panel = _Panel([msg])
        panel._editing_message_id = "m1"
        panel._build_mention_payload = lambda text: (
            "olha @5511999999999", ["5511999999999@s.whatsapp.net"])

        panel._apply_message_edit("olha @Ana", "grupo@g.us")

        assert msg["message"]["imageMessage"]["caption"] == "olha @Ana"
        assert "mentionedJid" not in msg["contextInfo"]
        assert "mentionedJid" not in msg["message"]["imageMessage"]["contextInfo"]
        assert panel.main_window.edit_calls == [("m1", "olha @Ana")]
        assert "5511999999999" not in panel.messages_list.texts[0]

    def test_a_video_keeps_its_measured_duration(self):
        from core.utils import MEASURED_SECONDS_KEY

        msg = _media("videoMessage", caption="antes", **{MEASURED_SECONDS_KEY: 37})
        panel = _Panel([msg])
        panel._editing_message_id = "m1"

        panel._apply_message_edit("depois", "grupo@g.us")

        video = msg["message"]["videoMessage"]
        assert video["caption"] == "depois"
        assert video[MEASURED_SECONDS_KEY] == 37
        assert msg["messageType"] == "videoMessage"

    def test_a_document_keeps_its_file_fields(self):
        msg = _media("documentMessage", caption="antes", fileName="livro.pdf", fileLength=1234)
        panel = _Panel([msg])
        panel._editing_message_id = "m1"

        panel._apply_message_edit("depois", "grupo@g.us")

        doc = msg["message"]["documentMessage"]
        assert (doc["caption"], doc["fileName"], doc["fileLength"]) == ("depois", "livro.pdf", 1234)


class TestRowPaginatedOutWhileTyping:
    def test_a_caption_edit_is_still_sent_without_mentions(self):
        """A sync can rebuild the list while the user types; the caption rule
        must not depend on the row still being on the page."""
        msg = _media(caption="antes")
        panel = _Panel([msg])
        panel._build_mention_payload = lambda text: (
            "olha @5511999999999", ["5511999999999@s.whatsapp.net"])

        panel._on_menu_edit_message(0, msg)
        panel._sorted_messages = []          # the row left the page
        panel._apply_message_edit("olha @Ana", "grupo@g.us")

        assert panel.main_window.edit_calls == [("m1", "olha @Ana")]
        assert panel.main_window.mention_calls == [None]

    def test_a_text_edit_still_keeps_its_mentions(self):
        msg = {"key": {"id": "m1", "fromMe": True}, "messageType": "conversation",
               "message": {"conversation": "oi"}, "messageTimestamp": int(time.time()) - 60}
        panel = _Panel([msg])
        panel._build_mention_payload = lambda text: (
            "oi @5511999999999", ["5511999999999@s.whatsapp.net"])

        panel._on_menu_edit_message(0, msg)
        panel._sorted_messages = []
        panel._apply_message_edit("oi @Ana", "grupo@g.us")

        assert panel.main_window.edit_calls == [("m1", "oi @5511999999999")]
        assert panel.main_window.mention_calls == [["5511999999999@s.whatsapp.net"]]


class TestGif:
    def test_a_gif_is_not_offered_for_editing(self):
        """Its row reads as a sticker and never speaks the caption."""
        assert edit_kind(_media("videoMessage", caption="legenda", gifPlayback=True)) is None

    def test_an_ordinary_video_still_is(self):
        assert edit_kind(_media("videoMessage", caption="legenda", gifPlayback=False)) == "caption"
