"""Pinned messages include old history, never steal open focus, and reject stale reads.

All widgets and dialogs are plain recording stubs; no wx window is created.
"""

from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import wx

from main_window.message_pins import MessagePinsMixin
from ui.conversation_panel.pinned_messages import PinnedMessagesMixin
from ui.conversation_panel.history_loading import HistoryLoadingMixin
from ui.conversation_panel.message_list import MessageListMixin
from tests.test_forwarded_prefix_setting import _Stub as RenderStub

JID = "123@s.whatsapp.net"


def message(mid, pinned=True, timestamp=10):
    return {"key": {"id": mid, "remoteJid": JID}, "pinInChat": pinned,
            "messageTimestamp": timestamp, "messageType": "conversation",
            "message": {"conversation": f"Content {mid}"}}


class Panel(PinnedMessagesMixin):
    _find_index_by_msg_id = MessageListMixin._find_index_by_msg_id
    _history_window_for_rebuild = HistoryLoadingMixin._history_window_for_rebuild
    _load_older_messages = HistoryLoadingMixin._load_older_messages
    _history_storage_jid = HistoryLoadingMixin._history_storage_jid

    def __init__(self, records=()):
        self.main_window = SimpleNamespace(
            i18n=SimpleNamespace(t=lambda key: {
                "pinned_messages_count": "Pinned messages ({count})",
                "pinned_messages_notice": "Pinned messages: {count}",
            }.get(key, key)),
            output=Mock(), get_pinned_messages=Mock(return_value=[]),
            _msg_bg_executor=SimpleNamespace(submit=lambda fn: self.work.append(fn)),
        )
        self.conversation = {"remoteJid": JID, "messages": {"messages": {"records": list(records)}}}
        self.conversation_panel = SimpleNamespace(Layout=Mock(), IsShown=lambda: self.visible)
        self._pinned_messages_btn = Mock()
        self._pinned_messages_btn.GetLabel.return_value = "Pinned messages"
        self._pinned_writes = set()
        self.work = []
        self.visible = True
        self._sorted_messages = list(records)
        self._all_sorted_messages = list(records)
        self._unread_sep_idx = -1
        self._expanded_oldest_msg_id = ""
        self._expanded_visible_count = 0
        self._focus_message_row = Mock()
        self._repaint_or_repopulate = Mock()
        self._begin_pinned_messages_visit()

    def _is_separator(self, msg):
        return not (msg.get("key") or {}).get("id")

    def _render_message_line(self, msg):
        return "Author: " + msg["message"]["conversation"]

    def _merge_history_into_records(self, messages):
        self.conversation["messages"]["messages"]["records"].extend(messages)

    def populate_messages(self, preserve_focus=False):
        self._apply_pinned_message_flags()
        rows = sorted(self.conversation["messages"]["messages"]["records"],
                      key=lambda m: m["messageTimestamp"])
        self._all_sorted_messages = rows
        offset, _ = self._history_window_for_rebuild(rows, 2)
        self._sorted_messages = rows[offset:]


@pytest.fixture(autouse=True)
def inline_callbacks(monkeypatch):
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *args, **kwargs: fn(*args, **kwargs))


class TestPinnedOverview:
    def test_open_window_follows_unpin_and_delayed_rollback(self):
        pin = message("old")
        panel = Panel([pin])
        panel._pinned_messages = [pin]
        snapshots = []
        panel._pinned_window = (SimpleNamespace(pins_changed=lambda: snapshots.append(
            [m["key"]["id"] for m in panel._pinned_messages])), JID)
        pin["pinInChat"] = False
        panel._pinned_message_state_changed(pin, JID)
        pin["pinInChat"] = True  # the background write was refused
        panel._pinned_message_state_changed(pin, JID)
        assert snapshots == [[], ["old"]]

    @pytest.mark.parametrize("same_chat", [True, False])
    def test_authoritative_read_refreshes_only_its_own_open_window(self, same_chat):
        panel = Panel()
        dialog = SimpleNamespace(pins_changed=Mock())
        panel._pinned_window = (dialog, JID if same_chat else "other@g.us")
        panel.main_window.get_pinned_messages.return_value = [message("old")]
        panel._load_pinned_messages()
        panel.work.pop()()
        assert dialog.pins_changed.call_count == int(same_chat)

    def test_open_updates_count_and_announces_without_moving_focus(self):
        old = message("old", False)
        removed = message("removed")
        panel = Panel([old, removed])
        panel.main_window.get_pinned_messages.return_value = [message("old"), message("ancient")]
        panel._load_pinned_messages(announce=True)
        assert not panel.main_window.get_pinned_messages.called  # worker-only I/O
        panel.work.pop()()
        panel._pinned_messages_btn.SetLabel.assert_called_with("Pinned messages (2)")
        panel.main_window.output.assert_called_once_with("Pinned messages: 2")
        assert old["pinInChat"] is True
        assert removed["pinInChat"] is False
        panel._focus_message_row.assert_not_called()

    def test_empty_chat_updates_count_and_stays_silent_on_open(self):
        panel = Panel()
        panel._load_pinned_messages(announce=True)
        panel.work.pop()()
        panel._pinned_messages_btn.SetLabel.assert_called_with("Pinned messages (0)")
        panel.main_window.output.assert_not_called()

    def test_the_count_is_not_announced_when_the_setting_is_off(self):
        """Issue #848: Settings > Spoken content can silence "Pinned messages
        in this chat: N" on opening a chat; the button still shows it."""
        panel = Panel()
        panel.main_window.settings = {"speech_content": {"announce_pinned_messages": False}}
        panel.main_window.get_pinned_messages.return_value = [message("old")]
        panel._load_pinned_messages(announce=True)
        panel.work.pop()()
        panel._pinned_messages_btn.SetLabel.assert_called_with("Pinned messages (1)")
        panel.main_window.output.assert_not_called()

    def test_background_open_can_suppress_the_notice(self):
        panel = Panel()
        panel.main_window.get_pinned_messages.return_value = [message("old")]
        panel._load_pinned_messages()
        panel.work.pop()()
        panel.main_window.output.assert_not_called()

    @pytest.mark.parametrize("transition", ["switch", "revisit", "close", "shutdown", "newer_read"])
    def test_stale_reads_do_not_update_or_announce(self, transition):
        panel = Panel()
        panel.main_window.get_pinned_messages.return_value = [message("old")]
        panel._load_pinned_messages(announce=True, show=True)
        if transition == "switch":
            panel.conversation = {"remoteJid": "456@s.whatsapp.net"}
        elif transition == "revisit":
            panel._begin_pinned_messages_visit()
        elif transition == "close":
            panel.conversation = None
        elif transition == "shutdown":
            panel.main_window._shutting_down = True
        else:
            panel._load_pinned_messages()
        panel.work[0]()
        assert panel._pinned_messages == []
        panel.main_window.output.assert_not_called()

    def test_hidden_chat_neither_announces_nor_opens_a_picker(self):
        panel = Panel()
        panel.visible = False
        panel._show_pinned_messages_picker = Mock()
        panel.main_window.get_pinned_messages.return_value = [message("old")]
        panel._load_pinned_messages(announce=True, show=True)
        panel.work.pop()()
        panel.main_window.output.assert_not_called()
        panel._show_pinned_messages_picker.assert_not_called()

    def test_failure_keeps_previous_list_and_reports_only_explicit_requests(self):
        panel = Panel()
        panel._pinned_messages = [message("old")]
        panel.main_window.get_pinned_messages.side_effect = ValueError("bad response")
        panel._load_pinned_messages(announce=True)
        panel.work.pop()()
        panel.main_window.output.assert_not_called()
        panel._load_pinned_messages(show=True)
        panel.work.pop()()
        assert len(panel._pinned_messages) == 1
        panel.main_window.output.assert_called_once_with("pinned_messages_failed")

    def test_pending_writes_defer_reads_until_the_last_write_finishes(self):
        panel = Panel()
        panel._load_pinned_messages()
        old_worker = panel.work.pop()
        panel.main_window.get_pinned_messages.return_value = [message("stale")]
        first = panel._begin_pinned_message_write(JID)
        second = panel._begin_pinned_message_write(JID)
        old_worker()
        assert panel._pinned_messages == []
        panel._load_pinned_messages(announce=True)
        assert panel.work == []
        panel._finish_pinned_message_write(first)
        assert panel.work == []
        panel._finish_pinned_message_write(second)
        assert len(panel.work) == 1
        panel._pinned_messages_btn.Enable.assert_called_with(True)
        panel.work.pop()()
        panel.main_window.output.assert_called_once_with("Pinned messages: 1")

    def test_local_toggle_and_rollback_update_the_overview(self):
        panel = Panel()
        panel._pinned_snapshot_known = True
        msg = message("new")
        panel._pinned_message_state_changed(msg)
        assert panel._pinned_messages == [msg]
        msg["pinInChat"] = False
        panel._pinned_message_state_changed(msg)
        assert panel._pinned_messages == []

    def test_bulk_toggle_updates_the_button_once(self):
        panel = Panel()
        panel._update_pinned_messages_button = Mock()
        panel._pinned_messages_state_changed([message("a"), message("b")], JID)
        assert len(panel._pinned_messages) == 2
        panel._update_pinned_messages_button.assert_called_once()

    def test_rollback_in_another_chat_does_not_change_current_pins(self):
        panel = Panel()
        panel._pinned_messages = [message("current")]
        panel._pinned_message_state_changed(message("other"), "other@g.us")
        assert panel._pinned_messages[0]["key"]["id"] == "current"

    def test_refresh_reapplies_snapshot_to_replaced_record_and_materialized_row(self):
        panel = Panel([message("old", False)])
        panel._pinned_snapshot_known = True
        panel._pinned_messages = [message("old")]
        replacement = message("old", False)
        panel.conversation["messages"]["messages"]["records"] = [replacement]
        assert panel._apply_pinned_message_flags() == ["old"]
        assert replacement["pinInChat"] is True
        assert panel._sorted_messages[0]["pinInChat"] is True


class TestPinnedNavigation:
    def test_old_pin_is_revealed_outside_configured_page_without_losing_newer_rows(self):
        panel = Panel([message(str(i), False, i) for i in range(10, 14)])
        panel._pinned_messages = [message("ancient", timestamp=1)]
        panel._pinned_snapshot_known = True
        panel._jump_to_pinned_message(panel._pinned_messages[0])
        assert [m["key"]["id"] for m in panel._sorted_messages] == ["ancient", "10", "11", "12", "13"]
        panel._focus_message_row.assert_called_once_with(0)
        assert panel._pinned_jump_id == ""

    def test_jump_uses_current_message_identity_after_rows_move(self):
        panel = Panel([message("new"), message("old")])
        panel._jump_to_pinned_message(message("old"))
        panel._focus_message_row.assert_called_once_with(1)

    def test_loading_more_after_an_old_pin_does_not_skip_a_db_row_or_prepend_out_of_order(self):
        panel = Panel([message(str(i), False, i) for i in range(10, 14)])
        pin = message("ancient", timestamp=1)
        panel._pinned_messages = [pin]
        panel._pinned_snapshot_known = True
        panel._jump_to_pinned_message(pin)
        panel.main_window.settings = {"user_interface": {"messages_page_size": 2}}
        panel.main_window.db = SimpleNamespace(get_messages=Mock(return_value=[
            message("9", False, 9), message("8", False, 8)]))
        panel._is_displayable_message = lambda msg: True
        panel._remember_expanded_window = Mock()
        panel._load_older_messages()
        panel.main_window.db.get_messages.assert_called_once_with(JID, limit=2, offset=4)
        assert [m["key"]["id"] for m in panel._all_sorted_messages] == ["ancient", "8", "9", "10", "11", "12", "13"]
        assert panel._is_loading_more is False

    def test_reopening_the_same_chat_retains_detached_pin_pagination(self):
        panel = Panel()
        panel._pinned_extra_history_ids.add("ancient")
        panel._begin_pinned_messages_visit(reset_history=False)
        assert panel._pinned_extra_history_ids == {"ancient"}
        panel._begin_pinned_messages_visit()
        assert not panel._pinned_extra_history_ids

    @staticmethod
    def fake_dialog(monkeypatch, result, target=None, then=None):
        import ui.dialogs.pinned_messages_dialog as module
        made = []

        class Dialog:
            def __init__(self, panel, main_window):
                made.append(self)
                self.target, self.then = target, then
                self.Destroy = Mock()

            def ShowModal(self):
                return result

        monkeypatch.setattr(module, "PinnedMessagesDialog", Dialog)
        return made

    @pytest.mark.parametrize("result", [wx.ID_OK, wx.ID_CANCEL])
    def test_window_enter_jumps_and_escape_preserves_focus(self, monkeypatch, result):
        panel = Panel([message("old")])
        panel._pinned_messages = [message("old")]
        made = self.fake_dialog(monkeypatch, result, target=message("old"))
        panel._show_pinned_messages_picker()
        made[0].Destroy.assert_called_once()
        assert panel._focus_message_row.called == (result == wx.ID_OK)

    def test_an_action_of_the_conversation_runs_after_the_jump(self, monkeypatch):
        """Reply, edit...: the window closes on the message, the panel
        focuses it and only then runs the action there."""
        panel = Panel([message("old")])
        panel._pinned_messages = [message("old")]
        order = []
        panel._focus_message_row = lambda index: order.append(("focus", index))
        self.fake_dialog(monkeypatch, wx.ID_OK, target=message("old"),
                         then=lambda: order.append("reply"))
        panel._show_pinned_messages_picker()
        assert order == [("focus", 0), "reply"]

    def test_a_window_closed_after_the_visit_changed_does_nothing(self, monkeypatch):
        panel = Panel([message("old")])
        panel._pinned_messages = [message("old")]
        then = Mock()
        import ui.dialogs.pinned_messages_dialog as module

        class Dialog:
            def __init__(self, panel_, main_window):
                self.target, self.then = message("old"), then
                self.Destroy = Mock()

            def ShowModal(self):
                panel._begin_pinned_messages_visit()
                return wx.ID_OK

        monkeypatch.setattr(module, "PinnedMessagesDialog", Dialog)
        panel._show_pinned_messages_picker()
        panel._focus_message_row.assert_not_called()
        then.assert_not_called()

    def test_empty_picker_does_not_create_a_dialog(self, monkeypatch):
        panel = Panel()
        made = self.fake_dialog(monkeypatch, wx.ID_OK)
        panel._show_pinned_messages_picker()
        assert made == []
        panel.main_window.output.assert_called_once_with("pinned_messages_empty")

    def test_resident_index_adds_an_old_pin_without_moving_focus(self):
        panel = Panel([message(str(i), False, i) for i in range(10, 14)])
        assert panel._resident_message_index(message("ancient", timestamp=1)) == 0
        panel._focus_message_row.assert_not_called()
        assert panel._resident_message_index({"key": {}}) == -1

    def test_the_window_rows_leave_out_the_pin_marker(self):
        panel = RenderStub()
        panel.main_window.i18n._STRINGS = {**panel.main_window.i18n._STRINGS, "message_pinned": "Fixada"}
        line = panel._render_message_line(message("old"), pin_marker=False)
        assert "Fixada" not in line and line.startswith("Fulano:")

    @pytest.mark.parametrize("mode", ["classic", "listbox"])
    def test_history_has_a_readable_pin_label_in_both_list_modes(self, mode):
        panel = RenderStub()
        panel._message_list_mode = mode
        panel.main_window.i18n._STRINGS = {**panel.main_window.i18n._STRINGS, "message_pinned": "Fixada"}
        assert panel._render_message_line(message("old")).startswith("📌 Fixada, Fulano:")


class TestPinnedApi:
    def make_window(self):
        class Window(MessagePinsMixin):
            _normalize_jid = staticmethod(lambda jid: jid.replace("@c.us", "@s.whatsapp.net"))
            _normalize_fetched_messages = staticmethod(lambda raw, jid: [message(m["id"]) for m in raw])
            _chat_jids_equivalent = staticmethod(lambda a, b: a == b)
        window = Window()
        window.wpp_server, window.wpp_port, window.token = "http://127.0.0.1", 6300, "test-token"
        window._phone_to_lid = {JID: "999@lid"}
        window._lid_to_phone = {"999@lid": JID}
        return window

    def test_phone_and_lid_aliases_are_sent_and_old_messages_are_normalized(self, monkeypatch):
        response = Mock()
        response.json.return_value = {"status": "success", "response": [{"id": "ancient"}]}
        post = Mock(return_value=response)
        monkeypatch.setattr("main_window.message_pins.api_post", post)
        window = self.make_window()
        pins = window.get_pinned_messages(JID)
        assert post.call_args.kwargs["json"] == {"chatIds": ["123@c.us", "999@lid"]}
        assert pins[0]["key"]["id"] == "ancient"
        assert pins[0]["pinInChat"] is True

    @pytest.mark.parametrize("body", [{"status": "success"}, {"status": "error", "response": []},
                                      {"status": "success", "response": {}}, [], None])
    def test_malformed_response_is_an_error_not_an_empty_pin_list(self, monkeypatch, body):
        response = Mock()
        response.json.return_value = body
        monkeypatch.setattr("main_window.message_pins.api_post", Mock(return_value=response))
        with pytest.raises(ValueError):
            self.make_window().get_pinned_messages(JID)

    def test_message_from_another_chat_is_rejected(self, monkeypatch):
        response = Mock()
        response.json.return_value = {"status": "success", "response": [{"id": "other"}]}
        monkeypatch.setattr("main_window.message_pins.api_post", Mock(return_value=response))
        window = self.make_window()
        window._chat_jids_equivalent = lambda a, b: False
        with pytest.raises(ValueError, match="Incomplete"):
            window.get_pinned_messages(JID)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not available")
@pytest.mark.parametrize("store_shape", ["array", "collection"])
def test_native_pin_store_reads_parent_keys_filters_expired_and_unpinned_and_deduplicates(store_shape):
    runtime = Path(__file__).resolve().parents[1] / "client/api_patches/src/util/pinnedMessagesRuntime.ts"
    script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const entries = [
  {pinType:1, parentMsgKey:{_serialized:'old'}, msgKey:{_serialized:'notification'}},
  {pinType:1, parentMsgKey:{_serialized:'expired'}, t:1, pinExpiryDuration:86400},
  {pinType:2, parentMsgKey:{_serialized:'unpin'}},
  {pinType:1, parentMsgKey:'old'},
  {pinType:1, parentMsgKey:'new', t:Date.now()/1000, pinExpiryDuration:86400}
];
const collection = process.argv[2] === 'collection' ? {getModelsArray:()=>entries} : entries;
const wpp = {whatsapp:{enums:{PIN_STATE:{PIN:1}}, PinInChatStore:{byChatId:id=>collection}},
  chat:{get:id=>id==='absent'?null:{id}}};
const ctx = vm.createContext({WPP:wpp});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').replaceAll('export ',''),ctx);
(async()=>{
assert.deepStrictEqual(Array.from(await ctx.readPinnedMessageIds(['phone','lid'])),['old','new']);
await assert.rejects(()=>ctx.readPinnedMessageIds(['absent']),/pinned_chat_not_found/);
wpp.whatsapp.PinInChatStore.byChatId = () => ({length:0});
await assert.rejects(()=>ctx.readPinnedMessageIds(['phone']),/pinned_messages_invalid/);
wpp.whatsapp.PinInChatStore.byChatId = () => ({getModelsArray:()=>[]});
assert.deepStrictEqual(Array.from(await ctx.readPinnedMessageIds(['phone'])),[]);
wpp.whatsapp.PinInChatStore = null;
await assert.rejects(()=>ctx.readPinnedMessageIds(['phone']),/pinned_messages_unavailable/);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run(["node", "-e", script, str(runtime), store_shape], capture_output=True,
                            text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not available")
def test_meta_pin_store_reads_persisted_pins_when_memory_collection_is_empty():
    runtime = Path(__file__).resolve().parents[1] / "client/api_patches/src/util/pinnedMessagesRuntime.ts"
    script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const queries = [];
let rows = [
  {pinType:1, parentMsgKey:'old', valid:true},
  {pinType:1, parentMsgKey:'expired', valid:false},
  {pinType:2, parentMsgKey:'unpin', valid:false},
];
const table = {anyOf:async(index,ids)=>{queries.push([index,ids]);return rows;}};
const modules = {
  WAWebModelStorageUtils:{getStorage:()=>({table:name=>{
    assert.strictEqual(name,'pinned-messages');return table;
  }})},
  WAWebPinsDbSerialization:{deserializePinInChat:row=>({
    ...row,parentMsgKey:{toString:()=>row.parentMsgKey}
  })},
  WAWebPinInChatCollection:{isPinValid:entry=>entry.valid},
};
const wpp = {
  whatsapp:{enums:{PIN_STATE:{PIN:1}},PinInChatStore:{byChatId:()=>{
    throw new Error('Memory snapshot must not be used for Meta');
  }}},
  loader:{loaderType:'meta',loadModule:name=>modules[name]},
  chat:{get:id=>({id:{toString:()=>id}})},
};
const ctx = vm.createContext({WPP:wpp});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').replaceAll('export ',''),ctx);
(async()=>{
assert.deepStrictEqual(Array.from(await ctx.readPinnedMessageIds(['phone','lid'])),['old']);
assert.deepStrictEqual(JSON.parse(JSON.stringify(queries)),[
  [['chatId'],['phone']],[['chatId'],['lid']]
]);
rows=[];
assert.deepStrictEqual(Array.from(await ctx.readPinnedMessageIds(['phone'])),[]);
table.anyOf=async()=>{throw new Error('Database read failed');};
await assert.rejects(()=>ctx.readPinnedMessageIds(['phone']),/Database read failed/);
table.anyOf=async()=>({});
await assert.rejects(()=>ctx.readPinnedMessageIds(['phone']),/pinned_messages_invalid/);
delete modules.WAWebPinsDbSerialization;
await assert.rejects(()=>ctx.readPinnedMessageIds(['phone']),/pinned_messages_unavailable/);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run(["node", "-e", script, str(runtime)], capture_output=True,
                            text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not available")
def test_pin_write_rejects_resolved_failure_and_accepts_only_requested_state():
    runtime = Path(__file__).resolve().parents[1] / "client/api_patches/src/util/pinnedMessagesRuntime.ts"
    script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
let result;
const calls=[];
const wpp={chat:{pinMsg:async(...args)=>{calls.push(args);return result;}}};
const ctx=vm.createContext({WPP:wpp});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').replaceAll('export ',''),ctx);
(async()=>{
for(const pin of [true,false]) {
  for(const rejected of [null,{}, {pinned:!pin}]) {
    result=rejected;
    const r=await ctx.writePinnedMessage({messageId:'target',pin});
    assert.strictEqual(r.ok,false);
    assert.strictEqual(r.error,'pin_message_unconfirmed');
  }
  result={pinned:pin};
  const r=await ctx.writePinnedMessage({messageId:'target',pin});
  assert.strictEqual(r.ok,true);
  assert.strictEqual(r.pinned,pin);
  assert.deepStrictEqual(calls.at(-1),['target',pin]);
}
const before=calls.length;
assert.strictEqual((await ctx.writePinnedMessage({messageId:'target',pin:'true'})).ok,false);
assert.strictEqual(calls.length,before);
wpp.chat.pinMsg=async()=>{throw new Error('private contact and message');};
assert.strictEqual((await ctx.writePinnedMessage({messageId:'target',pin:true})).error,'pin_message_failed');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run(["node", "-e", script, str(runtime)], capture_output=True,
                            text=True, timeout=20)
    assert result.returncode == 0, result.stderr
