"""Deleting a media message: keep its file on the phone, or remove it too.

WhatsApp Web's delete dialog has a "Delete file from your phone" box for media
messages, checked by default, and passes it on as clearMedia. Read from the
page itself over CDP (2026-10): the dialog's state is ``useWAWebToggle(!0)``,
the box is shown when a message of the selection is a media message that is
not a sticker, and WPP.chat.deleteMessage(chat, ids, deleteMediaInDevice,
revoke) hands it to Cmd.sendDeleteMsgs / Cmd.sendRevokeMsgs.

WinZapp never sent the field, and the library underneath defaults it to true
(``deleteMessage(chatId, messageId, onlyLocal = false, deleteMediaInDevice =
true)``), so every delete also removed the file from the phone with no way to
keep it. The box now exists, starts checked (nothing changes for someone who
never touches it), and its value is always sent explicitly.

ConversationsPanel and MainWindow cannot be built without a wx.App, so their
methods are bound onto stubs.
"""

import inspect
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import wx

from main_window import message_actions as window_actions
from main_window.message_actions import MessageActionsMixin
from ui.conversation_panel import bulk_messages
from ui.conversation_panel.media_paths import any_phone_media
from ui.conversations import ConversationsPanel

JID = "5511999999999@s.whatsapp.net"
KEY = {"id": "ABC", "fromMe": True, "remoteJid": JID}


def _msg(message_type, msg_id="m1"):
    return {"key": {"id": msg_id, "fromMe": True, "remoteJid": JID}, "messageType": message_type}


# ── which messages have a file on the phone ──────────────────────────────────


@pytest.mark.parametrize("message_type", ["imageMessage", "videoMessage", "audioMessage",
                                          "documentMessage"])
def test_a_photo_video_audio_or_document_has_a_file_on_the_phone(message_type):
    assert any_phone_media([_msg(message_type)]) is True


@pytest.mark.parametrize("message_type", ["conversation", "extendedTextMessage",
                                          "stickerMessage", "contactMessage",
                                          "pollCreationMessage", "protocolMessage", ""])
def test_text_stickers_and_the_rest_do_not(message_type):
    """A sticker is the one media type WhatsApp Web leaves out too."""
    assert any_phone_media([_msg(message_type)]) is False


def test_one_media_message_in_a_selection_is_enough():
    assert any_phone_media([_msg("conversation"), _msg("imageMessage", "m2")]) is True
    assert any_phone_media([]) is False
    assert any_phone_media([None, "x", {}]) is False


# ── what reaches the server ──────────────────────────────────────────────────


class _Window:
    delete_message_for_me = MessageActionsMixin.delete_message_for_me
    delete_message_for_everyone = MessageActionsMixin.delete_message_for_everyone
    wpp_server, wpp_port, token = "http://localhost", 6300, "tok"
    _phone_to_lid = {}

    def _serialize_msg_id(self, chat_jid, key):
        return f"true_{chat_jid}_{key['id']}"


@pytest.fixture
def post(monkeypatch):
    sent = Mock(return_value=SimpleNamespace(status_code=200, text=""))
    monkeypatch.setattr(window_actions, "api_post", sent)
    return sent


@pytest.mark.parametrize("method,only_local", [("delete_message_for_me", True),
                                               ("delete_message_for_everyone", False)])
def test_the_choice_is_always_sent_and_defaults_to_removing_the_file(post, method, only_local):
    """Explicit even when true: left out, the field would be decided by a
    default two layers down that the user never sees."""
    window = _Window()

    assert getattr(window, method)(JID, KEY) is True
    payload = post.call_args.kwargs["json"]
    assert payload["deleteMediaInDevice"] is True
    assert payload["onlyLocal"] is only_local

    assert getattr(window, method)(JID, KEY, delete_media=False) is True
    assert post.call_args.kwargs["json"]["deleteMediaInDevice"] is False


def test_the_server_route_hands_the_field_to_whatsapp():
    """Structural, on the patched route: deleteMediaInDevice goes to
    client.deleteMessage() as its fourth argument, after onlyLocal."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "client" / "api_patches" / "src"
              / "controller" / "deviceController.ts").read_text(encoding="utf-8")
    call = source[source.index("req.client.deleteMessage("):]
    call = call[:call.index(");")]
    assert [part.strip() for part in call.split("(", 1)[1].split(",") if part.strip()] == [
        "`${phone}`", "messageId", "onlyLocal", "deleteMediaInDevice"]


# ── the panel passing the choice on ──────────────────────────────────────────


class _Panel:
    _delete_message_for_me_only = ConversationsPanel._delete_message_for_me_only
    _delete_message_for_everyone_keep_row = ConversationsPanel._delete_message_for_everyone_keep_row
    _confirm_local_delete = ConversationsPanel._confirm_local_delete
    _add_delete_media_checkbox = ConversationsPanel._add_delete_media_checkbox
    _delete_media_default = ConversationsPanel._delete_media_default
    _delete_target_jid = ConversationsPanel._delete_target_jid

    def __init__(self, settings=None):
        self.calls = []
        self.conversation = {"remoteJid": JID}
        self.main_window = SimpleNamespace(
            settings=settings if settings is not None else {},
            i18n=SimpleNamespace(t=lambda key: key),
            _is_self_jid=lambda jid: False,
            delete_message_for_me=lambda j, k, delete_media=True: self.calls.append(
                ("me", delete_media)) or True,
            delete_message_for_everyone=lambda j, k, delete_media=True: self.calls.append(
                ("everyone", delete_media)) or True,
        )
        self.plain_confirms = []

    def remove_messages_by_id(self, ids, focus_previous=False):
        pass

    def _apply_confirmed_revoke(self, record, jid):
        pass

    def _confirm_local_only_delete(self, count):
        self.plain_confirms.append(count)
        return True


def _run(fn):
    before = set(threading.enumerate())
    fn()
    for thread in set(threading.enumerate()) - before:
        thread.join(timeout=2)


@pytest.mark.parametrize("delete_media", [True, False])
def test_delete_for_me_carries_the_choice(delete_media):
    panel = _Panel()
    _run(lambda: panel._delete_message_for_me_only(_msg("imageMessage"), "m1", 0, delete_media))
    assert panel.calls == [("me", delete_media)]


@pytest.mark.parametrize("delete_media", [True, False])
def test_delete_for_everyone_carries_the_choice(delete_media, monkeypatch):
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *args: None)
    panel = _Panel()
    _run(lambda: panel._delete_message_for_everyone_keep_row(_msg("imageMessage"), JID,
                                                             delete_media))
    assert panel.calls == [("everyone", delete_media)]


def test_a_caller_that_says_nothing_keeps_the_old_behaviour():
    """The paths that never ask (a cancelled send being revoked, for one)."""
    panel = _Panel()
    _run(lambda: panel._delete_message_for_me_only(_msg("imageMessage"), "m1", 0))
    assert panel.calls == [("me", True)]


# ── the box in the confirmations ─────────────────────────────────────────────


class _FakeRichDialog:
    def __init__(self, result, checked):
        self._result, self._checked = result, checked
        self.box = None
        self.labels = None

    def SetOKCancelLabels(self, ok, cancel):
        self.labels = (ok, cancel)

    def ShowCheckBox(self, label, checked):
        self.box = (label, checked)

    def IsCheckBoxChecked(self):
        return self._checked

    def ShowModal(self):
        return self._result

    def Destroy(self):
        pass


def test_a_local_only_delete_of_text_asks_the_plain_question():
    panel = _Panel()
    assert panel._confirm_local_delete([_msg("conversation")]) == (True, True)
    assert panel.plain_confirms == [1]


@pytest.mark.parametrize("result,checked,expected", [
    (wx.ID_OK, True, (True, True)),
    (wx.ID_OK, False, (True, False)),
    (wx.ID_CANCEL, True, (False, True)),
])
def test_a_local_only_delete_of_media_offers_the_box_checked(monkeypatch, result, checked,
                                                             expected):
    made = []

    def rich(parent, prompt, title, style):
        dialog = _FakeRichDialog(result, checked)
        dialog.style = style
        made.append(dialog)
        return dialog

    monkeypatch.setattr(wx, "RichMessageDialog", rich)
    panel = _Panel()

    assert panel._confirm_local_delete([_msg("videoMessage")]) == expected
    assert panel.plain_confirms == []
    dialog = made[0]
    assert dialog.box == ("delete_media_from_phone", True)
    assert dialog.labels == ("delete_msg_confirm_yes", "cancel")
    # Escape cancels and Enter does not carry into the delete, as in the plain
    # prompt (see _confirm_local_only_delete()).
    assert dialog.style & wx.CANCEL and dialog.style & wx.CANCEL_DEFAULT


def test_the_scope_dialog_gets_the_box_only_for_media(monkeypatch):
    boxes = []

    class _Box:
        def __init__(self, parent, label):
            self.label, self.value = label, None
            boxes.append(self)

        def SetValue(self, value):
            self.value = value

    monkeypatch.setattr(wx, "CheckBox", _Box)
    panel, sizer = _Panel(), Mock()

    assert panel._add_delete_media_checkbox(object(), sizer, [_msg("conversation")]) is None
    assert boxes == [] and not sizer.Add.called

    box = panel._add_delete_media_checkbox(object(), sizer, [_msg("audioMessage")])
    assert box is boxes[0]
    assert (box.label, box.value) == ("delete_media_from_phone", True)
    sizer.Add.assert_called_once()


# ── how the box starts: Settings > Interface ─────────────────────────────────

KEEP = {"user_interface": {"delete_media_from_phone": False}}


def test_the_box_starts_checked_unless_the_setting_says_otherwise():
    assert _Panel()._delete_media_default() is True
    assert _Panel({"user_interface": {}})._delete_media_default() is True
    assert _Panel(KEEP)._delete_media_default() is False


def test_the_setting_decides_how_both_confirmations_start(monkeypatch):
    made, boxes = [], []

    def rich(parent, prompt, title, style):
        dialog = _FakeRichDialog(wx.ID_OK, False)
        made.append(dialog)
        return dialog

    class _Box:
        def __init__(self, parent, label):
            boxes.append(self)

        def SetValue(self, value):
            self.value = value

    monkeypatch.setattr(wx, "RichMessageDialog", rich)
    monkeypatch.setattr(wx, "CheckBox", _Box)
    panel = _Panel(KEEP)

    panel._confirm_local_delete([_msg("imageMessage")])
    panel._add_delete_media_checkbox(object(), Mock(), [_msg("imageMessage")])

    assert made[0].box == ("delete_media_from_phone", False)
    assert boxes[0].value is False


def test_the_setting_is_a_shipped_default_that_is_on():
    """On is what WinZapp did before the box existed, so an existing
    installation that never opens Settings keeps its behaviour."""
    import json
    from pathlib import Path

    from core.utils import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["user_interface"]["delete_media_from_phone"] is True
    shipped = json.loads((Path(__file__).resolve().parents[1] / "client" / "data"
                          / "settings_default.json").read_text(encoding="utf-8"))
    assert shipped["user_interface"]["delete_media_from_phone"] is True


def test_both_scope_dialogs_read_the_box_and_pass_it_on():
    """Structural: the dialogs are built with real wx controls. The box sits
    after the scope radios and before the buttons (Tab order is creation
    order); with no box the answer is the default; and the value reaches both
    delete calls, single and bulk."""
    single = inspect.getsource(ConversationsPanel._on_menu_delete_message)
    assert single.index('i18n.t("delete_for_everyone")') \
        < single.index("self._add_delete_media_checkbox(panel, sizer, [msg])") \
        < single.index("btn_sizer = wx.StdDialogButtonSizer()")
    assert "delete_media = media_box.GetValue() if media_box else True" in single
    assert "self._delete_message_for_everyone_keep_row(msg, jid, delete_media)" in single
    assert "self._delete_message_for_me_only(msg, msg_id, index, delete_media)" in single

    bulk = inspect.getsource(bulk_messages)
    assert "self._add_delete_media_checkbox(panel, sizer, msgs_to_delete)" in bulk
    assert "delete_media = media_box.GetValue() if media_box else True" in bulk
    assert "confirmed, delete_media = self._confirm_local_delete(msgs_to_delete)" in bulk
    assert bulk.count("delete_media=delete_media") == 2
