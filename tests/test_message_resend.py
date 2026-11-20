"""Try sending again: an own message whose send failed or was never
confirmed, text or attachment, from the message menu or Ctrl+Shift+Y
(configurable as messages.ID_CTRL_SHIFT_Y). Never sent again by itself."""

import pytest
from types import SimpleNamespace
from unittest.mock import Mock

from core.message_resend import RESEND_PATH_KEY, needs_resend, resend_request
from core.shortcut_catalog import SHORTCUTS
from tests.test_unconfirmed_send_recovery import REMOTE, _ResendStub
from ui.conversations import ConversationsPanel


@pytest.fixture(autouse=True)
def _call_after_runs_now(monkeypatch):
    """The resend announcement goes through wx.CallAfter (after the focus
    move); there is no wx.App here, so run it inline."""
    import wx
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))


def _msg(msg_type="conversation", body=None, **state):
    msg = {"key": {"id": "loc-1", "fromMe": True, "remoteJid": REMOTE}, "_local_id": "loc-1",
           "messageType": msg_type, "message": body or {"conversation": "oi"}}
    msg.update(state)
    return msg


def _video(path, **state):
    return _msg("videoMessage", {"videoMessage": {"caption": "legenda", "fileName": "clip.mp4"}},
                **{RESEND_PATH_KEY: path}, **state)


class TestWhatCanBeSentAgain:
    def test_failed_and_unconfirmed_both_count(self):
        assert needs_resend(_msg(_send_failed=True))
        assert needs_resend(_msg(_send_unconfirmed=True))
        assert not needs_resend(_msg())
        assert not needs_resend(_msg(_local_pending=True))

    def test_never_someone_elses_message(self):
        received = _msg(_send_failed=True)
        received["key"]["fromMe"] = False
        assert resend_request(received) is None

    def test_a_text_sends_its_wire_text(self):
        assert resend_request(_msg(_send_failed=True)) == ("text", "oi")
        extended = _msg("extendedTextMessage", {"extendedTextMessage": {"text": "link"}},
                        _send_unconfirmed=True)
        assert resend_request(extended) == ("text", "link")

    def test_an_attachment_sends_its_file_while_it_is_there(self, tmp_path):
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x")
        assert resend_request(_video(str(clip), _send_failed=True)) == \
            ("media", str(clip), "video", "legenda")
        clip.unlink()
        assert resend_request(_video(str(clip), _send_failed=True)) is None

    def test_an_attachment_with_no_recorded_file_cannot(self):
        no_path = _msg("imageMessage", {"imageMessage": {}}, _send_failed=True)
        assert resend_request(no_path) is None


class _MediaStub(_ResendStub):
    def __init__(self, messages):
        super().__init__(messages)
        self.sent_files = []

    def _send_attachment_file(self, remote_jid, path, media_type, caption, quoted):
        self.sent_files.append((remote_jid, path, media_type, caption, quoted))
        return True


class TestSendingAgain:
    def test_a_failed_text_is_sent_again(self):
        stub = _ResendStub([_msg(_send_failed=True)])
        stub._on_menu_resend_message(stub._sorted_messages[0])
        [pending] = stub.main_window.message_queue.enqueued
        assert pending.text == "oi" and pending.local_id != "loc-1"
        assert stub.removed_ids == [{"loc-1"}]

    def test_an_attachment_goes_through_the_attachment_path(self, tmp_path):
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x")
        stub = _MediaStub([_video(str(clip), _send_unconfirmed=True)])
        stub._on_menu_resend_message(stub._sorted_messages[0])
        assert stub.sent_files == [(REMOTE, str(clip), "video", "legenda", None)]
        assert stub.removed_ids == [{"loc-1"}]
        assert stub.main_window.message_queue.enqueued == []

    def test_a_failure_whatsapp_reported_goes_under_its_real_id_too(self):
        """Reviewer: an ack -1 marks a row that already has WhatsApp's id in
        key.id; removing only the local id left it focused and offering the
        action, and each press sent another copy."""
        failed = _msg(_send_failed=True)
        failed["key"]["id"] = "3EB0REAL"
        stub = _ResendStub([failed])
        stub._on_menu_resend_message(failed)
        assert stub.removed_ids == [{"loc-1", "3EB0REAL"}]
        assert stub.main_window.spoken == ["resend_started"]

    def test_an_attachment_that_can_no_longer_be_sent_keeps_its_row(self, tmp_path):
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x")
        stub = _MediaStub([_video(str(clip), _send_failed=True)])
        stub._send_attachment_file = lambda *a: False
        stub._on_menu_resend_message(stub._sorted_messages[0])
        assert stub.removed_ids == [] and stub.main_window.spoken == []

    def test_nothing_to_send_again_sends_nothing(self):
        stub = _ResendStub([_msg()])
        stub._on_menu_resend_message(stub._sorted_messages[0])
        assert stub.main_window.message_queue.enqueued == [] and stub.removed_ids == []


class _KeyStub:
    _on_accel_resend_message = ConversationsPanel._on_accel_resend_message
    _is_separator = ConversationsPanel._is_separator

    def __init__(self, msg):
        self._sorted_messages = [msg]
        self.messages_list = SimpleNamespace(GetFirstSelected=lambda: 0)
        self.main_window = SimpleNamespace(output=Mock(), i18n=SimpleNamespace(t=lambda key: key))
        self._on_menu_resend_message = Mock()


class TestTheKey:
    def test_the_key_sends_the_focused_message_again(self):
        stub = _KeyStub(_msg(_send_failed=True))
        stub._on_accel_resend_message()
        stub._on_menu_resend_message.assert_called_once_with(stub._sorted_messages[0])

    def test_the_key_on_a_message_with_nothing_to_send_says_so(self):
        stub = _KeyStub(_msg())
        stub._on_accel_resend_message()
        stub._on_menu_resend_message.assert_not_called()
        stub.main_window.output.assert_called_once_with("resend_not_available", interrupt=True)

    def test_it_is_catalogued_and_configurable(self):
        [shortcut] = [s for s in SHORTCUTS if s.id == "messages.ID_CTRL_SHIFT_Y"]
        assert (shortcut.scope, shortcut.mod, shortcut.key) == ("messages", 6, ord("Y"))

    def test_the_attachment_row_records_its_file(self, tmp_path, monkeypatch):
        """The real _send_attachment_file on a MagicMock panel: the pending
        row it adds carries the file, which is what makes it resendable."""
        import wx
        from unittest.mock import MagicMock
        monkeypatch.setattr(wx, "CallAfter", lambda *a, **k: None)
        doc = tmp_path / "a.txt"
        doc.write_text("x")
        panel = MagicMock()
        panel.main_window.settings = {}
        panel.messages_list.GetItemCount.return_value = 0
        panel._typing_row_list = None
        panel._sorted_messages = []
        assert ConversationsPanel._send_attachment_file(panel, REMOTE, str(doc), "document", "", None)
        assert [row.get(RESEND_PATH_KEY) for row in panel._sorted_messages] == [str(doc)]


def test_a_delivered_attachment_forgets_its_local_path():
    """Reviewer: _resend_path stayed in messages.db for every sent file."""
    from core.reply_failure import clear_reply_draft
    record = _video("C:/Users/ana/Desktop/clip.mp4")
    clear_reply_draft(record)
    assert RESEND_PATH_KEY not in record


def test_raw_send_is_offered_only_on_a_kept_pending_voice_note():
    from core.message_resend import offers_raw_send
    kept = {"key": {"fromMe": True}, "_send_failed": True,
            "_nr_pending_retry": True, "messageType": "audioMessage",
            "message": {"audioMessage": {}}, RESEND_PATH_KEY: "k.resend.wav"}
    assert offers_raw_send(kept, file_exists=lambda p: True)
    # Its kept original is gone: nothing to send as recorded.
    assert not offers_raw_send(kept, file_exists=lambda p: False)
    # An ordinary failed audio attachment is not a kept-pending note.
    plain = dict(kept)
    plain.pop("_nr_pending_retry")
    assert not offers_raw_send(plain, file_exists=lambda p: True)
    assert not offers_raw_send(None)
