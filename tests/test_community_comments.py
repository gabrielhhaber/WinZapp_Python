"""Community comments keep parent identity, do not retry sends, and never open wx windows."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import wx

from core.community_comments import (CommunityCommentsError, can_open_comments,
                                     comment_send_confirmed, parse_comments)
from main_window.community_comments import CommunityCommentsMixin
from ui.dialogs.community_comments import CommunityCommentsDialog

JID = "123@g.us"
PARENT = "false_123@g.us_PARENT_456@lid"

def row(mid="reply", stamp=10, kind="comment"):
    return {"id": mid, "parentMsgId": PARENT, "chatId": JID, "author": "456@lid",
            "timestamp": stamp, "type": kind, "body": "private text", "messageSecret": "secret"}

class API(CommunityCommentsMixin):
    wpp_server, wpp_port, token = "http://localhost", 6300, "account:secret"
    def _serialize_msg_id(self, jid, key, message):
        return key.get("id")

class Widget:
    def __init__(self, value=""):
        self.value, self.items, self.selection = value, [], -1
        self.Enable = Mock()
        self.SetEditable = Mock()
        self.SetFocus = Mock()
        self.SetLabel = Mock()
        self.Freeze, self.Thaw = Mock(), Mock()
    def GetValue(self): return self.value
    def ChangeValue(self, value): self.value = value
    def GetItems(self): return self.items
    def SetItems(self, items): self.items = items
    def GetSelection(self): return self.selection
    def SetSelection(self, index): self.selection = index

class Dialog:
    # Bind real methods to a plain Python object; no wx dialog is constructed.
    _active = CommunityCommentsDialog._active
    _update_buttons = CommunityCommentsDialog._update_buttons
    _on_refresh = CommunityCommentsDialog._on_refresh
    _finish_refresh = CommunityCommentsDialog._finish_refresh
    _on_send = CommunityCommentsDialog._on_send
    _finish_send = CommunityCommentsDialog._finish_send
    _row_label = CommunityCommentsDialog._row_label
    close_comments = CommunityCommentsDialog.close_comments
    _on_text_key = CommunityCommentsDialog._on_text_key
    def __init__(self):
        self.work = []
        self._mw = SimpleNamespace(token="session", _shutting_down=False,
            i18n=SimpleNamespace(t=lambda key: {"community_comments_count":"Replies: {count}",
                                               "datetime_fmt":"%Y-%m-%d %H:%M"}.get(key, key)),
            speak_output=SimpleNamespace(output=Mock()),
            _msg_bg_executor=SimpleNamespace(submit=lambda fn: self.work.append(fn)),
            get_announcement_comments=Mock(return_value=[row()]),
            send_announcement_comment=Mock(return_value=True),
            _resolve_jid_name=Mock(return_value="Author"), _normalize_jid=lambda value:value)
        self._jid, self._message, self._token = JID, {"key":{"id":PARENT}}, "session"
        self._closed = self._busy = self._loaded = self._uncertain = False
        self._rows = []
        self._list, self._text = Widget(), Widget(" exact text ")
        self._send, self._reload, self._status = Widget(), Widget(), Widget()

@pytest.fixture(autouse=True)
def callbacks_inline(monkeypatch):
    monkeypatch.setattr(wx, "CallAfter", lambda fn,*args: fn(*args))

class TestCommentContracts:
    def test_group_candidate_and_cached_metadata(self):
        msg={"key":{"id":"parent"}}
        assert can_open_comments({"remoteJid":JID},msg)
        assert can_open_comments({"remoteJid":JID,"groupMetadata":{"defaultSubgroup":True}},msg)
        assert not can_open_comments({"remoteJid":JID,"groupMetadata":{"defaultSubgroup":"false"}},msg)
        assert not can_open_comments({"remoteJid":"123@lid"},msg)
        assert not can_open_comments({"remoteJid":JID},{"key":"bad"})
    def test_safe_sorted_rows_and_no_stale_deleted_text(self):
        result=parse_comments({"status":"success","response":[row("b",2),row("a",1,"revoked")]},PARENT)
        assert [r["id"] for r in result]==["a","b"]
        assert result[0]["body"]==""
        assert "messageSecret" not in result[1]
        assert result[1]["author"]=="456@lid"
    @pytest.mark.parametrize("changes",[{"parentMsgId":"wrong"},{"chatId":"999@g.us"},
        {"timestamp":float("nan")},{"timestamp":True},{"type":"other"},{"author":{}},{"body":12}])
    def test_invalid_payload_is_not_a_false_empty_success(self,changes):
        with pytest.raises(CommunityCommentsError):
            parse_comments({"status":"success","response":[{**row(),**changes}]},PARENT)
    def test_only_native_ok_confirms_a_comment_without_requiring_normal_message_id(self):
        assert comment_send_confirmed(201,{"status":"success","response":{"messageSendResult":"OK"}})
        for code,result in [(200,{"messageSendResult":"OK"}),(201,{}),(201,{"messageSendResult":"ERROR_UNKNOWN"})]:
            assert not comment_send_confirmed(code,{"status":"success","response":result})
    def test_read_keeps_complete_parent_key_and_url_encodes_it(self,monkeypatch):
        get=Mock(return_value=SimpleNamespace(status_code=200,raise_for_status=Mock(),
            json=lambda:{"status":"success","response":[row()]}))
        monkeypatch.setattr("main_window.community_comments.api_get",get)
        assert API().get_announcement_comments(JID,{"key":{"id":PARENT}})[0]["parentMsgId"]==PARENT
        assert "false_123%40g.us_PARENT_456%40lid" in get.call_args.args[0]
    @pytest.mark.parametrize("result",[{}, {"status":"success","response":{"messageSendResult":"ERROR_UNKNOWN"}}])
    def test_send_calls_once_and_never_uses_normal_outbox(self,monkeypatch,result):
        post=Mock(return_value=SimpleNamespace(status_code=201,json=lambda:result))
        monkeypatch.setattr("main_window.community_comments.api_post",post)
        with pytest.raises(CommunityCommentsError,match="unconfirmed"):
            API().send_announcement_comment(JID,{"key":{"id":PARENT}}," exact text ")
        post.assert_called_once()
        assert post.call_args.kwargs["json"]=={"text":" exact text "}
    def test_timeout_does_not_resend_or_expose_private_exception(self,monkeypatch):
        post=Mock(side_effect=TimeoutError("private token/text"))
        monkeypatch.setattr("main_window.community_comments.api_post",post)
        with pytest.raises(CommunityCommentsError,match="^unconfirmed$"):
            API().send_announcement_comment(JID,{"key":{"id":PARENT}},"text")
        post.assert_called_once()
    def test_foreign_parent_refused_before_http(self,monkeypatch):
        post=Mock();monkeypatch.setattr("main_window.community_comments.api_post",post)
        with pytest.raises(CommunityCommentsError):
            API().send_announcement_comment(JID,{"key":{"id":"false_999@g.us_X_456@lid"}},"text")
        post.assert_not_called()

class TestCommentWindow:
    def test_read_failure_logs_only_a_static_code(self, caplog):
        dlg=Dialog()
        dlg._finish_refresh(None, "private token/name/text")
        assert caplog.messages == ["[community_comments] read_failed code=unavailable"]

    def test_send_failure_logs_only_a_static_code(self, caplog):
        dlg=Dialog();dlg._loaded=True
        dlg._mw.send_announcement_comment.side_effect=TimeoutError("private token/name/text")
        dlg._on_send(None);dlg.work.pop()()
        assert caplog.messages == ["[community_comments] send_failed code=unconfirmed"]
        assert dlg._text.value==" exact text " and dlg._uncertain

    def test_initial_read_is_background_and_preserves_focus(self):
        dlg=Dialog();dlg._on_refresh(None)
        dlg._mw.get_announcement_comments.assert_not_called()
        assert not dlg._list.SetFocus.called
        dlg.work.pop()()
        assert dlg._loaded and len(dlg._rows)==1
        dlg._list.Freeze.assert_called_once();dlg._list.Thaw.assert_called_once()
        dlg._list.SetFocus.assert_not_called()
    def test_selection_survives_inserted_history_and_deleted_body(self):
        dlg=Dialog();dlg._finish_refresh([row("selected",10)],None)
        dlg._list.selection=0
        dlg._finish_refresh([row("older",1),row("selected",10,"revoked")],None)
        assert dlg._list.selection==1
        assert "private text" not in dlg._list.items[1]
    @pytest.mark.parametrize("stale",["closed","account","shutdown"])
    def test_stale_completion_never_touches_widgets_or_speech(self,stale):
        dlg=Dialog();dlg._on_refresh(None)
        if stale=="closed": dlg.close_comments()
        elif stale=="account": dlg._mw.token="new"
        else: dlg._mw._shutting_down=True
        dlg.work.pop()()
        assert not dlg._loaded
        dlg._list.Freeze.assert_not_called()
        dlg._mw.speak_output.output.assert_not_called()
    def test_send_requires_successful_read_and_prevents_double_clicks(self):
        dlg=Dialog();dlg._on_send(None);assert not dlg.work
        dlg._loaded=True;dlg._on_send(None);dlg._on_send(None)
        assert len(dlg.work)==1
        dlg.work.pop()()
        dlg._mw.send_announcement_comment.assert_called_once_with(JID,dlg._message," exact text ")
        assert dlg._text.value=="" and len(dlg.work)==1  # background refresh
    def test_ambiguous_send_keeps_draft_and_blocks_retry_until_explicit_refresh(self):
        dlg=Dialog();dlg._loaded=True;dlg._mw.send_announcement_comment.side_effect=TimeoutError()
        dlg._on_send(None);dlg.work.pop()()
        assert dlg._text.value==" exact text " and dlg._uncertain
        dlg._on_send(None);assert not dlg.work
        dlg._on_refresh(None);dlg.work.pop()()
        assert not dlg._uncertain and dlg._text.value==" exact text "
    def test_unavailable_read_does_not_enable_sending(self):
        dlg=Dialog();dlg._finish_refresh(None,"not_announcement")
        assert not dlg._loaded
        dlg._send.Enable.assert_called_with(False)
    def test_unknown_author_is_never_displayed_as_raw_jid(self):
        dlg=Dialog();dlg._mw._resolve_jid_name.return_value="456@lid"
        assert "456@lid" not in dlg._row_label(row())


def test_native_community_comments_adapter():
    from pathlib import Path
    import shutil
    import subprocess
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node") or str(root / "client/node/node.exe")
    if not (root / "client/api/node_modules/typescript").is_dir():
        pytest.skip("Node API not installed in this checkout")
    completed = subprocess.run([node, str(root / "tests/community_comments_runtime.cjs")],
                               capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_refresh_does_not_disable_its_focused_button():
    dlg=Dialog();dlg._on_refresh(None)
    dlg._reload.Enable.assert_called_with(True)
    dlg._text.SetEditable.assert_called_with(False)
    assert not dlg._text.SetFocus.called


def test_closed_dialog_ignores_in_flight_send_completion():
    dlg=Dialog();dlg._loaded=True;dlg._on_send(None);dlg.close_comments();dlg.work.pop()()
    assert dlg._text.value==" exact text "
    dlg._mw.speak_output.output.assert_not_called()
    assert not dlg.work


def test_malformed_parent_is_refused_before_http(monkeypatch):
    post=Mock();monkeypatch.setattr("main_window.community_comments.api_post",post)
    with pytest.raises(CommunityCommentsError):
        API().send_announcement_comment(JID,{"key":{"id":"broken"}},"text")
    post.assert_not_called()


def test_ctrl_enter_sends_but_enter_keeps_multiline_editing():
    dlg=Dialog();dlg._loaded=True
    event=SimpleNamespace(ControlDown=lambda:False,GetKeyCode=lambda:wx.WXK_RETURN,Skip=Mock())
    dlg._on_text_key(event);event.Skip.assert_called_once();assert not dlg.work
    event.ControlDown=lambda:True
    dlg._on_text_key(event);assert len(dlg.work)==1


class TestReplyNamesAndCounts:
    def test_profile_name_fills_missing_local_contact(self):
        from core.community_comments import comment_author_label
        dlg=Dialog();dlg._mw._resolve_jid_name.return_value="unnamed_participant"
        assert comment_author_label({**row(),"authorName":"Profile name"},dlg._mw,JID)=="Profile name"
    def test_saved_name_has_priority_over_profile_name(self):
        from core.community_comments import comment_author_label
        dlg=Dialog();dlg._mw._resolve_jid_name.return_value="Saved contact"
        assert comment_author_label({**row(),"authorName":"Profile name"},dlg._mw,JID)=="Saved contact"
    @pytest.mark.parametrize("native_self",[True,False])
    def test_own_comment_uses_personal_self_reference(self,native_self):
        from core.community_comments import comment_author_label
        dlg=Dialog();dlg._mw._is_self_jid=lambda _jid: not native_self
        dlg._mw.self_reference_label=lambda:"Custom self label"
        assert comment_author_label({**row(),"fromMe":native_self},dlg._mw,JID)=="Custom self label"
        dlg._mw._resolve_jid_name.assert_not_called()
    def test_wire_parser_retains_name_and_own_flag(self):
        result=parse_comments({"status":"success","response":[{**row(),"authorName":"Profile name","fromMe":True}]},PARENT)
        assert result[0]["authorName"]=="Profile name" and result[0]["fromMe"] is True
    def test_count_is_normalized_and_spoken_after_the_body(self):
        from tests.test_edited_marker_survives_sync import _Normalizer
        from tests.test_forwarded_prefix_setting import _Stub as Renderer
        raw={"id":PARENT,"from":JID,"type":"chat","body":"text","timestamp":10,"replyCount":7}
        normalized=_Normalizer()._normalize_wpp_message(raw)
        assert normalized["replyCount"]==7
        panel=Renderer()
        panel._get_message_content=lambda msg: msg["message"]["conversation"]
        panel.main_window.i18n=SimpleNamespace(t=lambda key: "Replies: {count}" if key=="community_comments_count" else key)
        rendered=panel._render_message_line(normalized)
        assert ", Replies: 7" in rendered
        assert rendered.index("text") < rendered.index("Replies: 7")
        for bad in [None,True,-1,"7",float("inf")]:
            normalized=_Normalizer()._normalize_wpp_message({**raw,"replyCount":bad})
            assert "replyCount" not in normalized
            assert "Replies:" not in panel._render_message_line(normalized)
    def test_open_thread_updates_parent_count_without_changing_selection(self):
        from ui.conversation_panel.community_comments import CommunityCommentsPanelMixin
        original={"key":{"id":"parent"}}
        resident={"key":{"id":"parent"}}
        panel=SimpleNamespace(conversation={"remoteJid":JID,"messages":{"messages":{"records":[resident]}}},
            _sorted_messages=[resident],_repaint_or_repopulate=Mock())
        CommunityCommentsPanelMixin._update_announcement_reply_count(panel,JID,original,7)
        assert original["replyCount"]==resident["replyCount"]==7
        panel._repaint_or_repopulate.assert_called_once_with(["parent"])
        CommunityCommentsPanelMixin._update_announcement_reply_count(panel,JID,original,7)
        panel._repaint_or_repopulate.assert_called_once()
