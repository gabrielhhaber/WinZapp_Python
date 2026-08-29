"""Enter in the Media tab acts on the message, never on a row index.

Reported live: some audios in the Media tab simply do not play — no sound, no
announcement, no visible change in the panel, not even the "baixando..." the
download path would have said. The suspicion was a codec problem, but the log
showed playback was never reached at all:

    [conversation_data] media message is no longer in the panel's list —
    action skipped rather than applied to another row

That tab lists the conversation's whole history straight from the database,
while ConversationsPanel keeps roughly the last 200 messages in memory. So for
any older row the index lookup returned -1 and the action was dropped in
silence. Audio is simply the type Enter is the natural gesture for; documents,
images, videos and links in the same tab were affected identically.

The fix is a message-based entry point (ConversationsPanel.activate_message),
so no index has to exist. These tests bind the unbound methods onto a stub —
ConversationsPanel is a wx.Panel and cannot be instantiated without a wx.App.
"""

import types

import pytest

import app_paths
from ui.conversations import ConversationsPanel


@pytest.fixture(autouse=True)
def _account_dir(tmp_path):
    """The audio branch builds a path through data_path(), which refuses to
    answer without an active account."""
    app_paths.set_active_account(None)
    app_paths.set_allow_legacy_flat(True)
    yield
    app_paths.set_allow_legacy_flat(False)


def _audio(mid="a1", seconds=7):
    return {"key": {"id": mid}, "messageType": "audioMessage",
            "message": {"audioMessage": {"seconds": seconds}}}


def _document(mid="d1"):
    return {"key": {"id": mid}, "messageType": "documentMessage",
            "message": {"documentMessage": {"fileName": "contrato.pdf"}}}


def _image(mid="i1"):
    return {"key": {"id": mid}, "messageType": "imageMessage",
            "message": {"imageMessage": {}}}


def _text(mid="t1", body="veja https://exemplo.com"):
    return {"key": {"id": mid}, "messageType": "conversation",
            "message": {"conversation": body}}


class _Panel:
    """Only what activate_message() touches on the branches under test."""

    activate_message = ConversationsPanel.activate_message
    _do_activate_message = ConversationsPanel._do_activate_message
    _extract_links = ConversationsPanel._extract_links

    def __init__(self, in_list=()):
        # Deliberately NOT the messages under test: the whole point is that
        # activation works for a message this panel has never heard of.
        self._sorted_messages = list(in_list)
        self.played = []
        self.opened = []
        self.viewed = []
        self.popups = []
        self.conversation = {"remoteJid": "5511900000000@s.whatsapp.net"}

    def _is_separator(self, msg):
        return False

    def _toggle_playback(self, msg_id, duration, msg, file_path=None, audio_ext=None):
        self.played.append((msg_id, duration, file_path))

    def open_media_message(self, msg):
        self.opened.append(msg)

    def open_media_viewer_for_message(self, msg, restore_index=None):
        self.viewed.append((msg, restore_index))

    def _use_conversation_video_media_viewer_dialog(self):
        return True

    def _show_message_text_popup(self, msg):
        self.popups.append(msg)

    def _render_message_line(self, msg):
        return (msg.get("message") or {}).get("conversation", "")


class TestAMessageOutsideThePanelsListStillActivates:
    """The reported bug, one case per media category the tab lists."""

    def test_an_audio_reaches_playback(self):
        panel = _Panel(in_list=[])
        msg = _audio("3EB0E20679B80526F00EB3", seconds=12)

        panel.activate_message(msg)

        assert len(panel.played) == 1
        msg_id, duration, file_path = panel.played[0]
        assert msg_id == "3EB0E20679B80526F00EB3"
        assert duration == 12
        assert file_path.endswith("3EB0E20679B80526F00EB3.msv")

    def test_a_document_reaches_the_open_path(self):
        panel = _Panel(in_list=[])
        msg = _document()
        panel.activate_message(msg)
        assert panel.opened == [msg]

    def test_an_image_reaches_the_viewer(self):
        panel = _Panel(in_list=[])
        msg = _image()
        panel.activate_message(msg)
        assert panel.viewed == [(msg, None)]

    def test_a_link_still_opens_its_text_handling(self):
        """Links are one of the tab's categories too."""
        panel = _Panel(in_list=[])
        opened = []
        panel._extract_links = lambda rendered: ["https://exemplo.com"]
        import ui.conversations as mod
        original = mod.os.startfile if hasattr(mod.os, "startfile") else None
        mod.os.startfile = lambda url: opened.append(url)
        try:
            panel.activate_message(_text())
        finally:
            if original is not None:
                mod.os.startfile = original
        assert opened == ["https://exemplo.com"]


class TestTheMessageIdIsCleanedTheSameWay:
    """A group message id arrives as false_<jid>_<id>_<participant>; the voice
    file on disk is named after the middle part only."""

    def test_a_group_style_id_resolves_to_the_bare_id(self):
        panel = _Panel(in_list=[])
        panel.activate_message(
            _audio("false_120363422192569688@g.us_3EB0E2_115358660870276@lid")
        )
        _msg_id, _duration, file_path = panel.played[0]
        assert file_path.endswith("3EB0E2.msv")


class TestActivationFromTheListStillWorks:
    """The panel's own Enter path goes through the same method now."""

    def test_the_row_index_resolves_to_its_message(self):
        msg = _audio("a1")
        panel = _Panel(in_list=[msg])
        panel._do_activate_message(0)
        assert [c[0] for c in panel.played] == ["a1"]

    def test_the_viewer_still_gets_an_index_to_restore_focus_to(self):
        """Opened from a real row, the viewer has somewhere to put focus back."""
        msg = _image()
        panel = _Panel(in_list=[msg])
        panel._do_activate_message(0)
        assert panel.viewed == [(msg, 0)]

    @pytest.mark.parametrize("index", [-1, 1, 99])
    def test_an_impossible_index_is_still_refused(self, index):
        panel = _Panel(in_list=[_audio()])
        panel._do_activate_message(index)
        assert panel.played == []


class TestJunkIsRefusedRatherThanCrashing:
    @pytest.mark.parametrize("msg", [None, "texto", 42, []])
    def test_a_non_message_does_nothing(self, msg):
        panel = _Panel(in_list=[])
        panel.activate_message(msg)
        assert panel.played == [] and panel.opened == [] and panel.viewed == []
