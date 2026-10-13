"""A missing quote before sending is a refusal, never an offline/unknown send."""

import copy
import json
import threading
import types

import pytest

from core.message_queue import MessageQueue, PendingMessage
from core.reply_failure import (REPLY_DRAFT_KEYS, clear_reply_draft, is_missing_quote_failure,
                                 mark_missing_reply_quote, quote_missing_before_send)
from main import MainWindow
from ui.conversations import ConversationsPanel
from tests.god_modules import patch_main_global
from tests.test_reply_with_mention_keeps_quote import _Stub as SendStub, QUOTED, GROUP, LID, MENTION
from tests import test_duplicate_message_echo_matching as echo


VERDICT = {"status": "error", "code": "quoted_message_not_found",
           "stage": "quote_resolution", "sendAttempted": False}


class Response:
    def __init__(self, body=VERDICT, status=500):
        self.body, self.status_code = body, status
        self.text = json.dumps(body)

    def json(self):
        return self.body


def test_complete_pre_send_verdict_is_recognized():
    assert quote_missing_before_send(Response())


@pytest.mark.parametrize("body", [None, [], "msg_not_found", {},
    {**VERDICT, "sendAttempted": True}, {**VERDICT, "sendAttempted": 0},
    {**VERDICT, "sendAttempted": "false"}, {**VERDICT, "stage": "send"},
    {**VERDICT, "status": "success"}, {**VERDICT, "code": "msg_not_found"},
    {"error": {"message": "Message false_chat@c.us_ID not found"}},
    *[{k: v for k, v in VERDICT.items() if k != absent} for absent in VERDICT],
])
def test_incomplete_or_legacy_error_does_not_claim_nothing_was_sent(body):
    assert not quote_missing_before_send(Response(body))


@pytest.mark.parametrize("status", [200, 201, 400, 404, 409, 502, 503])
def test_unrelated_status_does_not_take_the_new_contract(status):
    assert not quote_missing_before_send(Response(status=status))


def test_invalid_json_does_not_override_ambiguity():
    response = Response()
    response.json = lambda: json.loads("not json")
    assert not quote_missing_before_send(response)


@pytest.mark.parametrize("destination", [GROUP, LID, "5511999999999@s.whatsapp.net"])
@pytest.mark.parametrize("mentions", [None, [MENTION]])
def test_missing_quote_is_not_resent_plain_or_to_another_address(monkeypatch, destination, mentions):
    calls, flags = [], []
    stub = SendStub()
    stub._set_wa_connected = lambda *args: flags.append(args)
    patch_main_global(monkeypatch, "api_post", lambda url, **kw: calls.append(url) or Response())
    result = stub.send_text_message(destination, "hello", quoted=QUOTED, mentioned_jids=mentions)
    assert len(calls) == 1
    assert result == {"ok": False, "retry": False, "reason": "quoted_message_not_found"}
    assert flags == []


def test_a_refusal_on_the_legacy_retry_does_not_strip_the_quote(monkeypatch):
    replies = iter([Response({}, 400), Response()])
    calls = []
    patch_main_global(monkeypatch, "api_post", lambda url, **kw: calls.append(url) or next(replies))
    result = SendStub().send_text_message(LID, "hello", quoted=QUOTED)
    assert len(calls) == 2
    assert all(url.endswith('/send-reply') for url in calls)
    assert result['reason'] == 'quoted_message_not_found'


@pytest.mark.parametrize("quoted", [None, {"key": {"remoteJid": "status@broadcast", "id": "X"}}])
def test_plain_and_status_sends_do_not_use_the_ordinary_reply_contract(monkeypatch, quoted):
    patch_main_global(monkeypatch, "api_post", lambda *a, **kw: Response())
    result = SendStub().send_text_message(GROUP, "hello", quoted=quoted)
    assert result.get("ambiguous") is True
    assert result.get("reason") is None


def message():
    return {"_local_id": "local", "_local_pending": True,
            "key": {"id": "local", "remoteJid": GROUP, "fromMe": True},
            "messageType": "conversation", "message": {"conversation": "draft"},
            "_reply_draft_text": "> keep this\n@Name https://example.com",
            "_reply_draft_mentions": [MENTION], "_reply_draft_mention_names": {MENTION: "Name"}}


def chats(record):
    return {GROUP: {"messages": {"messages": {"records": [record]}}}}


def test_failure_marks_the_origin_record_and_preserves_all_text():
    record = message()
    original = copy.deepcopy(record)
    assert mark_missing_reply_quote(chats(record), 'local') == (GROUP, record)
    assert record['_send_failed'] and record['_reply_quote_missing']
    assert not record['_local_pending']
    assert record['message'] == original['message']
    assert record['_reply_draft_text'] == original['_reply_draft_text']
    assert mark_missing_reply_quote(chats(record), 'deleted') is None


def test_delayed_callback_updates_original_chat_without_touching_a_new_draft():
    record = message()
    events = []
    stub = types.SimpleNamespace(chats=chats(record), background_mode=False,
        db=types.SimpleNamespace(insert_message=lambda jid, msg: events.append(('message', jid, msg['_local_id']))),
        _is_cancelled_send=lambda local: False,
        _schedule_save=lambda **kw: events.append(('save', kw)),
        _on_message_failed=lambda local: events.append(('failed', local)),
        output=lambda *a, **kw: events.append(('speech', a)),
        i18n=types.SimpleNamespace(t=lambda key: key))
    MainWindow._on_reply_quote_missing(stub, 'local')
    assert events == [('message', GROUP, 'local'), ('save', {'dirty_jid': GROUP}), ('failed', 'local'),
                      ('speech', ('reply_quote_missing_error',))]


@pytest.mark.parametrize('cancelled', [True, False])
def test_deleted_or_cancelled_record_is_not_restored_or_announced(cancelled):
    dropped = []
    stub = types.SimpleNamespace(chats={},
        _is_cancelled_send=lambda local: cancelled,
        _on_cancelled_message_dropped=lambda local: dropped.append(local))
    MainWindow._on_reply_quote_missing(stub, 'local')
    assert dropped == (['local'] if cancelled else [])


class Field:
    def __init__(self, text=''):
        self.text, self.focused = text, False
    def GetValue(self):
        return self.text
    def SetValue(self, text):
        self.text = text
    def SetInsertionPointEnd(self):
        pass
    def SetFocus(self):
        self.focused = True


PHONE = "5511988887777@s.whatsapp.net"


class _Identity:
    """The real JID equivalence, with LID bridged to PHONE."""
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _jid_address_forms = MainWindow._jid_address_forms
    _chat_jids_equivalent = MainWindow._chat_jids_equivalent

    def __init__(self, output):
        self.output = lambda *a, **kw: output.append(a)
        self.i18n = types.SimpleNamespace(t=lambda key: key)
        self._lid_to_phone = {LID: PHONE}
        self._phone_to_lid = {PHONE: LID}


def panel():
    output = []
    return types.SimpleNamespace(conversation={'remoteJid': GROUP},
        message_field=Field(), _quoted_message=None, _editing_message_id=None,
        _pending_mentions=[], _pending_mention_display_names={},
        _rebuild_mention_pills=lambda: None,
        main_window=_Identity(output), output=output)


def test_explicit_recovery_restores_composed_text_and_mentions_without_sending():
    record = message()
    mark_missing_reply_quote(chats(record), 'local')
    stub = panel()
    ConversationsPanel._on_restore_unsent_reply(stub, record)
    assert stub.message_field.text.replace('\r\n', '\n') == record['_reply_draft_text']
    assert stub._pending_mentions == [MENTION]
    assert stub._pending_mention_display_names == {MENTION: 'Name'}
    assert stub._quoted_message is None
    assert stub._last_sent_signature is None
    assert stub.message_field.focused
    assert stub.output == []


@pytest.mark.parametrize('busy', ['text', 'quote', 'edit', 'mentions'])
def test_recovery_never_overwrites_other_composition_state(busy):
    record = message()
    mark_missing_reply_quote(chats(record), 'local')
    stub = panel()
    if busy == 'text': stub.message_field.text = 'new draft'
    if busy == 'quote': stub._quoted_message = {'key': {'id': 'another'}}
    if busy == 'edit': stub._editing_message_id = 'another'
    if busy == 'mentions': stub._pending_mentions = [LID]
    original = stub.message_field.text
    ConversationsPanel._on_restore_unsent_reply(stub, record)
    assert stub.message_field.text == original
    assert not stub.message_field.focused
    assert stub.output == [('reply_restore_draft_busy',)]


def test_recovery_does_not_insert_text_into_another_chat():
    record = message()
    mark_missing_reply_quote(chats(record), 'local')
    stub = panel()
    stub.conversation = {'remoteJid': LID}
    ConversationsPanel._on_restore_unsent_reply(stub, record)
    assert stub.message_field.text == '' and stub.output == []


@pytest.mark.parametrize('sent_to, open_chat', [(LID, PHONE), (PHONE, LID),
                                                (PHONE.replace('@s.whatsapp.net', '@c.us'), PHONE)])
def test_recovery_survives_the_chat_being_rekeyed(sent_to, open_chat):
    """Sent while the chat was keyed by its @lid, restored after
    deduplicate_chats() re-keyed it to the phone JID (or the reverse): the
    record keeps the old address, and the recovery must still work."""
    record = message()
    record['key']['remoteJid'] = sent_to
    mark_missing_reply_quote({sent_to: {"messages": {"messages": {"records": [record]}}}}, 'local')
    stub = panel()
    stub.conversation = {'remoteJid': open_chat}
    ConversationsPanel._on_restore_unsent_reply(stub, record)
    assert stub.message_field.text.replace('\r\n', '\n') == record['_reply_draft_text']


def test_recovery_needs_an_open_conversation():
    record = message()
    mark_missing_reply_quote(chats(record), 'local')
    stub = panel()
    stub.conversation = None
    ConversationsPanel._on_restore_unsent_reply(stub, record)
    assert stub.message_field.text == ''


def test_composition_snapshot_keeps_display_text_and_wire_mentions(monkeypatch):
    import ui.conversation_panel.text_sending as module
    monkeypatch.setattr(module, 'append_message_row', lambda *a: None)
    monkeypatch.setattr(module, 'message_row_count', lambda *a: 0)
    saved, queued = [], []
    stub = panel()
    stub._pending_mentions = [MENTION]
    stub._pending_mention_display_names = {MENTION: 'Name'}
    stub._quoted_message = QUOTED
    stub._pending_link_preview = {'title': 'do not inject this', 'description': '', 'canonicalUrl': 'https://example.com'}
    stub._build_mention_payload = lambda text: ('@5511888888888 hello', [MENTION])
    stub._sorted_messages = []
    stub._clear_empty_placeholder = lambda: None
    stub._render_message_line = lambda msg: 'rendered'
    stub._hide_mention_suggestions = lambda: None
    stub._on_cancel_reply = lambda: None
    stub._clear_link_preview = lambda: None
    stub._register_virtual_msg = lambda msg: saved.append(msg)
    stub.main_window.message_queue = types.SimpleNamespace(enqueue=queued.append)
    stub.main_window.mark_conversation_as_read = lambda jid: None
    stub.main_window._schedule_set_chats = lambda: None
    ConversationsPanel._send_new_text_message(stub, '@Name hello', GROUP)
    assert saved[0]['_reply_draft_text'] == '@Name hello'
    assert saved[0]['_reply_draft_mentions'] == [MENTION]
    assert saved[0]['_reply_draft_mention_names'] == {MENTION: 'Name'}
    assert queued[0].text == '@5511888888888 hello'
    assert queued[0].mentioned_jids == [MENTION]
    assert saved[0]['_reply_draft_mention_names'] is not stub._pending_mention_display_names


def test_empty_local_id_cannot_mark_a_real_message_failed():
    record = {'key': {'id': 'REAL'}, 'message': {'conversation': 'real'}}
    assert mark_missing_reply_quote(chats(record), None) is None
    assert '_send_failed' not in record


def test_queue_reports_the_special_failure_once_and_drops_it(monkeypatch):
    import core.message_queue as module
    monkeypatch.setattr(module.wx, 'CallAfter', lambda fn, *a: fn(*a))
    complete = threading.Event()
    calls = []
    stub = types.SimpleNamespace(offline_mode=False, _wa_connected=True,
        send_text_message=lambda *a, **kw: calls.append('send') or
            {'ok': False, 'retry': False, 'reason': 'quoted_message_not_found'},
        _on_reply_quote_missing=lambda local: calls.append(local) or complete.set())
    queue = MessageQueue(stub)
    try:
        queue.enqueue(PendingMessage('local', GROUP, text='draft', quoted=QUOTED))
        assert complete.wait(timeout=2)
    finally:
        queue.stop()
    assert calls == ['send', 'local']
    assert queue._pending == {}


@pytest.mark.parametrize('for_everyone', [False, True])
def test_deleting_a_missing_quote_failure_is_local_only(monkeypatch, tmp_path, for_everyone):
    from tests.test_unconfirmed_send_recovery import _DeleteStub, _patch_delete_dialog
    import wx
    record = message()
    mark_missing_reply_quote(chats(record), 'local')
    _patch_delete_dialog(monkeypatch, result=wx.ID_OK, everyone_selected=for_everyone, tmp_path=tmp_path)
    stub = _DeleteStub(record)
    stub._on_menu_delete_message(0)
    assert stub.removed_ids == [{'local'}]
    assert stub.main_window.everyone_calls == []
    assert stub.main_window.for_me_calls == []


def test_failed_reply_and_mentions_survive_database_close_and_reopen(tmp_path, fernet_key):
    from core.database_bridge import DatabaseBridge
    path = str(tmp_path / 'reply.db')
    record = message()
    record['messageTimestamp'] = 1234
    db = DatabaseBridge(path, fernet_key)
    try:
        db.upsert_chat(GROUP, {'remoteJid': GROUP})
        stub = types.SimpleNamespace(chats=chats(record), background_mode=True, db=db,
            _is_cancelled_send=lambda local: False, _schedule_save=lambda **kw: None,
            _on_message_failed=lambda local: None)
        MainWindow._on_reply_quote_missing(stub, 'local')
    finally:
        db.close()
    db = DatabaseBridge(path, fernet_key)
    try:
        [restored] = db.get_messages(GROUP)
    finally:
        db.close()
    for field in ('_local_id', '_send_failed', '_reply_quote_missing', '_local_pending',
                  '_reply_draft_text', '_reply_draft_mentions', '_reply_draft_mention_names', 'message'):
        assert restored[field] == record[field]
    # The persisted record itself is sufficient for recovery after restart.
    stub = panel()
    ConversationsPanel._on_restore_unsent_reply(stub, restored)
    assert stub.message_field.text.replace('\r\n', '\n') == record['_reply_draft_text']


def test_database_failure_keeps_memory_text_and_reports_that_it_was_not_saved():
    record = message()
    spoken = []
    def fail(*a):
        raise OSError('simulated write failure')
    stub = types.SimpleNamespace(chats=chats(record), background_mode=False,
        db=types.SimpleNamespace(insert_message=fail),
        _is_cancelled_send=lambda local: False, _schedule_save=lambda **kw: None,
        _on_message_failed=lambda local: None, output=lambda text, **kw: spoken.append(text),
        i18n=types.SimpleNamespace(t=lambda key: key))
    MainWindow._on_reply_quote_missing(stub, 'local')
    assert record['_reply_draft_text']
    assert spoken == ['reply_quote_missing_error', 'reply_quote_save_failed']


@pytest.mark.parametrize('entry', ['shortcut', 'stale_menu'])
def test_remote_edit_entry_points_use_guarded_recovery(monkeypatch, entry):
    from core.message_edit import edit_kind
    record = message()
    mark_missing_reply_quote(chats(record), 'local')
    assert edit_kind(record) is None
    stub = panel()
    stub.message_field.text = 'another draft'
    stub._sorted_messages = [record]
    stub.messages_list = types.SimpleNamespace(GetFirstSelected=lambda: 0)
    stub._is_separator = lambda msg: False
    stub._on_restore_unsent_reply = lambda msg: ConversationsPanel._on_restore_unsent_reply(stub, msg)
    if entry == 'shortcut':
        ConversationsPanel._on_accel_edit_message(stub, None)
    else:
        ConversationsPanel._on_menu_edit_message(stub, 0, record)
    assert stub.message_field.text == 'another draft'
    assert stub._editing_message_id is None
    assert stub.output == [('reply_restore_draft_busy',)]


@pytest.mark.parametrize('record, expected', [(None, False), ({}, False),
    ({'_reply_quote_missing': True}, False), ({'_send_failed': True}, False),
    ({'_reply_quote_missing': True, '_send_failed': True}, True)])
def test_only_the_definite_missing_quote_state_uses_local_recovery(record, expected):
    assert is_missing_quote_failure(record) is expected


def test_self_chat_quote_failure_deletes_without_a_remote_uuid_request(monkeypatch, tmp_path):
    from tests.test_unconfirmed_send_recovery import _DeleteStub, _patch_delete_dialog
    record = message()
    mark_missing_reply_quote(chats(record), 'local')
    _patch_delete_dialog(monkeypatch, tmp_path=tmp_path)
    stub = _DeleteStub(record)
    stub.main_window._is_self_jid = lambda jid: True
    stub._confirm_local_only_delete = lambda count: True
    stub._on_menu_delete_message(0)
    assert stub.removed_ids == [{'local'}]
    assert stub.main_window.everyone_calls == []
    assert stub.main_window.for_me_calls == []


@pytest.mark.parametrize('for_everyone', [False, True])
@pytest.mark.parametrize('mixed', [False, True])
def test_bulk_delete_removes_failed_reply_locally_and_keeps_real_message_scope(monkeypatch, tmp_path, for_everyone, mixed):
    from tests.test_unconfirmed_send_recovery import _DeleteStub, _patch_delete_dialog
    import ui.conversation_panel.bulk_messages as module
    _patch_delete_dialog(monkeypatch, everyone_selected=for_everyone, tmp_path=tmp_path)
    monkeypatch.setattr(module.wx, 'CallAfter', lambda fn, *a: fn(*a))
    monkeypatch.setattr(module.threading, 'Thread', lambda target, **kw: types.SimpleNamespace(start=target))
    record = message()
    mark_missing_reply_quote(chats(record), 'local')
    stub = _DeleteStub(record)
    real = {'key': {'id': 'REAL', 'remoteJid': GROUP, 'fromMe': True}, 'messageType': 'conversation'}
    stub._sorted_messages = [record, real] if mixed else [record]
    stub.selected_messages = {m['key']['id'] for m in stub._sorted_messages}
    stub._group_admin_delete_override = lambda: False
    stub._refresh_message_rows_by_ids = lambda ids: None
    stub._on_bulk_delete_for_everyone_done = lambda failed: None
    stub._apply_confirmed_revoke = lambda *a: None
    stub._delete_target_jid = lambda key: key['remoteJid']
    ConversationsPanel._on_mass_delete_messages(stub, None)
    expected = {'local', 'REAL'} if mixed and not for_everyone else {'local'}
    assert stub.removed_ids == [expected]
    every = stub.main_window.everyone_calls
    local = stub.main_window.for_me_calls
    assert len(every) == int(mixed and for_everyone)
    assert len(local) == int(mixed and not for_everyone)
    assert all(key['id'] == 'REAL' for jid, key in every + local)
    assert not stub.selected_messages


# ── the recovery copy goes once the reply was delivered ──────────────────────

def _draft(**record):
    return {**record, "_reply_draft_text": "text", "_reply_draft_mentions": [MENTION],
            "_reply_draft_mention_names": {MENTION: "Name"}}


def test_clear_reply_draft_drops_only_the_recovery_copy():
    record = _draft(message={"conversation": "text"})
    clear_reply_draft(record)
    assert record == {"message": {"conversation": "text"}}
    clear_reply_draft(record)   # idempotent
    clear_reply_draft(None)     # and tolerant


def test_a_reply_marked_sent_no_longer_carries_its_draft():
    msg = echo._make_msg(**_draft())
    panel = echo._make_panel(msg)
    # The tracked copy and the row are separate dicts after a list rebuild.
    tracked = copy.deepcopy(msg)
    panel._outgoing_virtual_messages = {"loc-1": tracked}
    ConversationsPanel._mark_message_sent(panel, "loc-1", real_id="REAL")
    for record in (msg, tracked):
        assert not any(key in record for key in REPLY_DRAFT_KEYS)


def test_a_reply_whose_send_is_still_unconfirmed_keeps_its_draft():
    """No real id yet: the row stays pending for the echo, draft included."""
    msg = echo._make_msg(**_draft())
    panel = echo._make_panel(msg)
    ConversationsPanel._mark_message_sent(panel, "loc-1", real_id=None)
    assert all(key in msg for key in REPLY_DRAFT_KEYS)


def test_the_echo_of_a_reply_drops_its_draft_before_it_is_saved():
    saved = []
    record = _draft(**echo._pending_record("locB"))
    stub = echo._Stub(echo._chat_with([record]))
    stub._msg_bg_executor = types.SimpleNamespace(submit=lambda fn: fn())
    stub.db = types.SimpleNamespace(insert_message=lambda jid, msg: saved.append(dict(msg)))
    stub.on_new_message(echo._echo("REAL_B"))
    assert record["_local_pending"] is False
    assert saved and not any(key in saved[0] for key in REPLY_DRAFT_KEYS)
