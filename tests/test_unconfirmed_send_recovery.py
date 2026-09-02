"""Regression tests: a message WinZapp itself never confirmed sending could
get stuck forever with no way to retry or clear it.

_mark_message_unconfirmed() (see its own docstring) is deliberately not
"failed" — WhatsApp may still deliver the message, and the WebSocket echo is
supposed to replace the bubble if it does — but if that echo never arrives
the row just sits there marked "Envio não confirmado" indefinitely. Two
things were missing:

1. Deleting it. _on_menu_delete_message()'s cancelled_pending branch already
   treated a still-queued/in-flight send as "nothing to revoke, local
   delete only" — an unconfirmed send is the exact same situation (its
   key.id is still the local UUID, never a real WhatsApp id), but the
   condition only checked _local_pending, which _mark_message_unconfirmed
   sets to False. So deleting an unconfirmed row fell into the "for
   everyone"/"for me" API-revoke paths, which build a request around an id
   that was never real. The fix folds _send_unconfirmed into the same
   branch.

2. Retrying it. There was no way to resend one at all. The fix adds a
   "Resend" context-menu action for unconfirmed text messages
   (_on_menu_resend_message), which re-enqueues the same text as a fresh
   send and removes the old row.

ConversationsPanel is a wx.Panel and cannot be instantiated without a
running wx.App, so both methods are exercised against plain stubs — the
delete-dialog fixture mirrors tests/test_mass_selection.py's own
fake_delete_dialog (same comment there: "mirroring
_on_menu_delete_message()'s single-message dialog").
"""

import inspect

import wx

from ui.conversations import ConversationsPanel

REMOTE = "5511999999999@s.whatsapp.net"


# ── Dismiss: _on_menu_delete_message()'s cancelled_pending branch ───────────


class _FakeRadioButton:
    _all = []

    def __init__(self, parent, label="", style=0):
        self._value = False
        _FakeRadioButton._all.append(self)

    def SetValue(self, v):
        self._value = v

    def GetValue(self):
        return self._value


class _FakeSizer:
    def __init__(self, *a, **k):
        pass

    def Add(self, *a, **k):
        pass


class _FakePanel:
    def __init__(self, *a, **k):
        pass

    def SetSizer(self, *a, **k):
        pass


class _FakeButton:
    def __init__(self, parent, id=None, label=""):
        pass


class _FakeBtnSizer:
    def __init__(self, *a, **k):
        pass

    def AddButton(self, *a, **k):
        pass

    def Realize(self):
        pass


def _make_fake_dialog(result, everyone_selected):
    class _FakeDialog:
        def __init__(self, *a, **k):
            pass

        def SetSizer(self, *a, **k):
            pass

        def Fit(self):
            pass

        def CentreOnParent(self):
            pass

        def ShowModal(self):
            if len(_FakeRadioButton._all) > 1 and everyone_selected:
                _FakeRadioButton._all[1].SetValue(True)
                _FakeRadioButton._all[0].SetValue(False)
            return result

        def Destroy(self):
            pass

    return _FakeDialog


def _patch_delete_dialog(monkeypatch, result=wx.ID_OK, everyone_selected=False,
                         tmp_path=None):
    _FakeRadioButton._all = []
    if tmp_path is not None:
        # _cancel_pending_message() drops the pre-cached copies of a send that
        # was genuinely stopped, and data_path() refuses to answer without an
        # active account — same redirect tests/test_message_cancel_race.py uses.
        import ui.conversations as conversations
        monkeypatch.setattr(conversations, "data_path", lambda name: str(tmp_path / name))
    monkeypatch.setattr(wx, "Dialog", _make_fake_dialog(result, everyone_selected))
    monkeypatch.setattr(wx, "Panel", _FakePanel)
    monkeypatch.setattr(wx, "BoxSizer", _FakeSizer)
    monkeypatch.setattr(wx, "RadioButton", _FakeRadioButton)
    monkeypatch.setattr(wx, "Button", _FakeButton)
    monkeypatch.setattr(wx, "StdDialogButtonSizer", _FakeBtnSizer)


class _I18n:
    def t(self, key):
        return key


class _FakeMainWindow:
    def __init__(self):
        self.everyone_calls = []
        self.for_me_calls = []
        self.cancel_calls = []
        self.i18n = _I18n()
        self.message_queue = self

    def _is_self_jid(self, jid):
        return False

    def delete_message_for_everyone(self, jid, key):
        self.everyone_calls.append((jid, key))
        return True

    def delete_message_for_me(self, jid, key):
        self.for_me_calls.append((jid, key))
        return True

    # main_window.message_queue.cancel(...)
    # cancel_result mirrors the real contract: True only when the message was
    # still queued and untouched. False covers BOTH "a worker owns it" and
    # "it is not in the queue any more" — the distinction _cancel_pending_message
    # can no longer infer, which is why the caller passes hold_for_echo.
    cancel_result = True

    def cancel(self, local_id):
        self.cancel_calls.append(local_id)
        return self.cancel_result

    def get_chat(self, jid):
        # _cancel_pending_message() looks the chat up to find the record's
        # position before removing the row. Nothing here depends on the
        # position, so an empty chat is enough.
        return {}


class _DeleteStub:
    _on_menu_delete_message     = ConversationsPanel._on_menu_delete_message
    _delete_message_for_me_only = ConversationsPanel._delete_message_for_me_only
    _delete_target_jid          = ConversationsPanel._delete_target_jid
    # The real helper, not a stand-in: the cancelled_pending branch delegates
    # to it, and what it does with a cancel() that could not stop the send is
    # exactly what these tests are about.
    _cancel_pending_message     = ConversationsPanel._cancel_pending_message
    _is_separator = ConversationsPanel._is_separator
    _is_system_event = lambda self, msg: False

    def __init__(self, msg):
        self.main_window = _FakeMainWindow()
        self._sorted_messages = [msg]
        self.conversation = {"remoteJid": REMOTE}
        self._outgoing_virtual_messages = {msg.get("_local_id"): msg} if msg.get("_local_id") else {}
        self._media_upload_progress = {}
        self._media_transfer_started = set()
        self.removed_ids = []
        self.gauge_hidden = 0

    def remove_messages_by_id(self, ids, focus_previous=False):
        self.removed_ids.append(set(ids))

    def _hide_media_transfer_gauge(self):
        self.gauge_hidden += 1

    def _record_position(self, chat, local_id):
        # -1 = "not in the chat's records", which is what an empty fake chat
        # means. _cancel_pending_message() reads it as "nothing to re-insert".
        return -1


def _unconfirmed_msg():
    return {
        "key": {"id": "loc-1", "fromMe": True, "remoteJid": REMOTE},
        "_local_id": "loc-1",
        "_local_pending": False,
        "_send_unconfirmed": True,
        "messageType": "conversation",
    }


def _still_pending_msg():
    return {
        "key": {"id": "loc-2", "fromMe": True, "remoteJid": REMOTE},
        "_local_id": "loc-2",
        "_local_pending": True,
        "messageType": "conversation",
    }


def _real_sent_msg():
    return {
        "key": {"id": "REAL_ID", "fromMe": True, "remoteJid": REMOTE},
        "messageType": "conversation",
    }


class TestDeletingAnUnconfirmedMessage:
    def test_deletes_locally_only_no_api_revoke_attempted(self, monkeypatch, tmp_path):
        _patch_delete_dialog(monkeypatch, result=wx.ID_OK, everyone_selected=False, tmp_path=tmp_path)
        stub = _DeleteStub(_unconfirmed_msg())

        stub._on_menu_delete_message(0)

        assert stub.removed_ids == [{"loc-1"}]
        assert stub.main_window.everyone_calls == []
        assert stub.main_window.for_me_calls == []
        assert stub.main_window.cancel_calls == ["loc-1"]

    def test_ignores_a_for_everyone_choice(self, monkeypatch, tmp_path):
        """The dialog still offers "for everyone" (from_me=True, not a
        group/self-chat), but there is no real id to revoke — whichever
        radio the user picked must not reach the API."""
        _patch_delete_dialog(monkeypatch, result=wx.ID_OK, everyone_selected=True, tmp_path=tmp_path)
        stub = _DeleteStub(_unconfirmed_msg())

        stub._on_menu_delete_message(0)

        assert stub.main_window.everyone_calls == []
        assert stub.removed_ids == [{"loc-1"}]

    def test_cleans_up_tracking_state(self, monkeypatch, tmp_path):
        _patch_delete_dialog(monkeypatch, tmp_path=tmp_path)
        msg = _unconfirmed_msg()
        stub = _DeleteStub(msg)
        stub._media_upload_progress["loc-1"] = 0.5
        stub._media_transfer_started.add("loc-1")

        stub._on_menu_delete_message(0)

        assert "loc-1" not in stub._outgoing_virtual_messages
        assert "loc-1" not in stub._media_upload_progress
        assert "loc-1" not in stub._media_transfer_started
        assert stub.gauge_hidden == 1


class TestStillPendingMessageIsUnaffected:
    """Regression guard: the pre-existing cancelled_pending behavior for a
    still-queued/in-flight send must keep working exactly as before."""

    def test_still_uses_the_local_only_path(self, monkeypatch, tmp_path):
        _patch_delete_dialog(monkeypatch, tmp_path=tmp_path)
        stub = _DeleteStub(_still_pending_msg())

        stub._on_menu_delete_message(0)

        assert stub.removed_ids == [{"loc-2"}]
        assert stub.main_window.everyone_calls == []


class TestANormalSentMessageIsUnaffected:
    """Regression guard: a message with a real WhatsApp id must still go
    through the actual revoke API, not the local-only shortcut."""

    def test_for_everyone_still_calls_the_api(self, monkeypatch, tmp_path):
        _patch_delete_dialog(monkeypatch, result=wx.ID_OK, everyone_selected=True, tmp_path=tmp_path)
        stub = _DeleteStub(_real_sent_msg())

        stub._on_menu_delete_message(0)
        import threading
        for t in threading.enumerate():
            if t is not threading.current_thread():
                t.join(timeout=2)

        assert stub.main_window.everyone_calls == [(REMOTE, {"id": "REAL_ID", "fromMe": True, "remoteJid": REMOTE})]

    def test_plain_delete_still_calls_delete_for_me(self, monkeypatch, tmp_path):
        _patch_delete_dialog(monkeypatch, result=wx.ID_OK, everyone_selected=False, tmp_path=tmp_path)
        stub = _DeleteStub(_real_sent_msg())

        stub._on_menu_delete_message(0)
        import threading
        for t in threading.enumerate():
            if t is not threading.current_thread():
                t.join(timeout=2)

        assert stub.main_window.for_me_calls == [(REMOTE, {"id": "REAL_ID", "fromMe": True, "remoteJid": REMOTE})]


# ── Resend: _on_menu_resend_message() ────────────────────────────────────────


class _FakeMessagesList:
    def __init__(self):
        self.appended = []

    def Append(self, row):
        self.appended.append(row)

    def GetItemCount(self):
        return len(self.appended)

    def EnsureVisible(self, idx):
        pass


class _FakeQueue:
    def __init__(self):
        self.cancelled = []
        self.enqueued = []

    def cancel(self, local_id):
        self.cancelled.append(local_id)
        return True

    def enqueue(self, pending_message):
        self.enqueued.append(pending_message)


class _FakeMainWindowForResend:
    def __init__(self):
        self.message_queue = _FakeQueue()
        self.set_chats_calls = 0

    def _schedule_set_chats(self):
        self.set_chats_calls += 1


class _ResendStub:
    _on_menu_resend_message = ConversationsPanel._on_menu_resend_message

    def __init__(self, sorted_messages):
        self.main_window = _FakeMainWindowForResend()
        self._sorted_messages = list(sorted_messages)
        self.conversation = {"remoteJid": REMOTE}
        self.messages_list = _FakeMessagesList()
        self._outgoing_virtual_messages = {}
        self.removed_ids = []
        self.registered = []

    # Deliberately NOT stubbing _get_message_content here. It used to be
    # stubbed with a raw-body reader — which is the fix, not the production
    # method — so the resend path was never actually exercised against the
    # display text the real method returns, and the link-preview/mention
    # corruption below went unnoticed. _on_menu_resend_message must not call
    # it at all; if it starts again, these tests fail with AttributeError,
    # which is the point.

    def _clear_empty_placeholder(self):
        pass

    def remove_messages_by_id(self, ids, focus_previous=False):
        self.removed_ids.append(set(ids))

    def _render_message_line(self, msg, **kw):
        return "RENDERED"

    def _register_virtual_msg(self, virtual_msg):
        self.registered.append(virtual_msg)


def _unconfirmed_text_msg(local_id="loc-1", text="oi, tudo bem?"):
    return {
        "key": {"id": local_id, "fromMe": True, "remoteJid": REMOTE},
        "_local_id": local_id,
        "_local_pending": False,
        "_send_unconfirmed": True,
        "messageType": "conversation",
        "message": {"conversation": text},
    }


class TestResendMessage:
    def test_enqueues_a_new_pending_message_with_the_same_text(self):
        stub = _ResendStub([_unconfirmed_text_msg(text="oi, tudo bem?")])

        stub._on_menu_resend_message(stub._sorted_messages[0])

        assert len(stub.main_window.message_queue.enqueued) == 1
        pm = stub.main_window.message_queue.enqueued[0]
        assert pm.text == "oi, tudo bem?"
        assert pm.jid == REMOTE

    def test_removes_the_old_unconfirmed_row(self):
        stub = _ResendStub([_unconfirmed_text_msg(local_id="loc-1")])

        stub._on_menu_resend_message(stub._sorted_messages[0])

        assert stub.removed_ids == [{"loc-1"}]
        assert stub.main_window.message_queue.cancelled == ["loc-1"]

    def test_the_new_message_gets_a_different_local_id(self):
        stub = _ResendStub([_unconfirmed_text_msg(local_id="loc-1")])

        stub._on_menu_resend_message(stub._sorted_messages[0])

        pm = stub.main_window.message_queue.enqueued[0]
        assert pm.local_id != "loc-1"

    def test_a_new_virtual_row_is_shown_immediately(self):
        stub = _ResendStub([_unconfirmed_text_msg()])

        stub._on_menu_resend_message(stub._sorted_messages[0])

        assert len(stub.messages_list.appended) == 1
        assert len(stub.registered) == 1
        new_row = stub.registered[0]
        assert new_row["_local_pending"] is True
        assert new_row["message"]["conversation"] == "oi, tudo bem?"

    def test_extended_text_message_content_is_recovered(self):
        msg = {
            "key": {"id": "loc-3", "fromMe": True, "remoteJid": REMOTE},
            "_local_id": "loc-3",
            "_send_unconfirmed": True,
            "messageType": "extendedTextMessage",
            "message": {"extendedTextMessage": {"text": "com link"}},
        }
        stub = _ResendStub([msg])

        stub._on_menu_resend_message(msg)

        pm = stub.main_window.message_queue.enqueued[0]
        assert pm.text == "com link"

    def test_does_nothing_for_empty_content(self):
        msg = _unconfirmed_text_msg(text="")

        stub = _ResendStub([msg])
        stub._on_menu_resend_message(msg)

        assert stub.main_window.message_queue.enqueued == []
        assert stub.removed_ids == []

    def test_does_nothing_without_a_resolvable_jid(self):
        msg = _unconfirmed_text_msg()
        msg["key"]["remoteJid"] = ""
        stub = _ResendStub([msg])
        stub.conversation = None

        stub._on_menu_resend_message(msg)

        assert stub.main_window.message_queue.enqueued == []


class TestResendMenuItemIsGatedCorrectly:
    """on_messages_context_menu() builds a whole wx.Menu tree (submenus,
    reactions, save-as, ...) that isn't practical to construct here — see
    CLAUDE.md on avoiding bulk UI tests. This pins the gating condition
    itself in the source instead, same style as
    tests/test_lid_merge_keeps_messages.py's own structural test for a
    method too large to exercise every branch of directly."""

    def test_resend_is_gated_on_text_type_and_unconfirmed_flag(self):
        src = inspect.getsource(ConversationsPanel.on_messages_context_menu)
        assert 'if _is_text and msg.get("_send_unconfirmed"):' in src
        assert "resend_message" in src


class TestTheResendCarriesTheWireTextNotTheDisplayText:
    """A resend must put back on the wire exactly what was sent, not what the
    message list shows.

    _get_message_content() is the LIST's renderer. For an extendedTextMessage
    it ends in link_preview_text(), which PREPENDS the title/description
    WhatsApp resolved for the URL ("<title>. <description>. <text>"), and it
    runs _resolve_mentions_in_text(), which turns the stored "@5548..." back
    into "@João". Both are display affordances. Sending either one delivers
    characters the user never typed.
    """

    def test_a_link_preview_is_not_injected_into_the_body(self):
        msg = _unconfirmed_text_msg()
        msg["messageType"] = "extendedTextMessage"
        msg["message"] = {
            "extendedTextMessage": {
                "text": "https://noticias.exemplo.com/materia",
                "title": "Título da matéria",
                "description": "Primeiro parágrafo da matéria",
            }
        }
        panel = _ResendStub([msg])

        ConversationsPanel._on_menu_resend_message(panel, msg)

        sent = panel.main_window.message_queue.enqueued[-1]
        assert sent.text == "https://noticias.exemplo.com/materia"
        assert "Título da matéria" not in sent.text
        assert "Primeiro parágrafo" not in sent.text

    def test_a_mention_keeps_its_phone_form(self):
        msg = _unconfirmed_text_msg()
        msg["messageType"] = "extendedTextMessage"
        msg["message"] = {
            "extendedTextMessage": {
                "text": "@5548999999999 bom dia",
                "contextInfo": {"mentionedJid": ["5548999999999@s.whatsapp.net"]},
            }
        }
        panel = _ResendStub([msg])

        ConversationsPanel._on_menu_resend_message(panel, msg)

        sent = panel.main_window.message_queue.enqueued[-1]
        assert sent.text == "@5548999999999 bom dia"

    def test_a_message_that_legitimately_starts_with_a_quote_marker_is_intact(self):
        """The edit path strips a leading "> " because it drops the text into
        the composer for the user to read first. A resend fires straight at
        message_queue, so the same strip silently truncates a message from
        anyone who types quote-style lines."""
        msg = _unconfirmed_text_msg()
        msg["message"] = {"conversation": "> importante\nvê isso aqui"}
        panel = _ResendStub([msg])

        ConversationsPanel._on_menu_resend_message(panel, msg)

        sent = panel.main_window.message_queue.enqueued[-1]
        assert sent.text == "> importante\nvê isso aqui"


class TestDeletingAnUnconfirmedMessageDoesNotWaitForAnEcho:
    """An unconfirmed send is already OVER — the worker finished it and
    reported the outcome. Deleting it must dispose of the record, not park it.

    MessageQueue.cancel() answers False for two different situations: "a worker
    owns this message right now" and "this message is not in the queue at all
    any more". Only the first justifies _cancel_pending_message()'s
    hold-for-echo tail (mark _cancelled_awaiting_id, stash it, re-insert the
    record at its old position, wait for the queue to report). An unconfirmed
    message is the second, and its report already came and went — so parking it
    strands the record in the chat permanently: invisible to the list and the
    preview, re-persisted on every save, holding a slot in the 50-entry stash,
    and unreachable by the echo matcher, which only looks at _local_pending
    records.
    """

    def test_the_record_is_not_marked_awaiting_an_id(self, monkeypatch, tmp_path):
        _patch_delete_dialog(monkeypatch, tmp_path=tmp_path)
        msg = _unconfirmed_msg()
        stub = _DeleteStub(msg)

        stub._on_menu_delete_message(0)

        assert "_cancelled_awaiting_id" not in msg

    def test_it_is_not_stashed_waiting_for_an_outcome_report(self, monkeypatch, tmp_path):
        _patch_delete_dialog(monkeypatch, tmp_path=tmp_path)
        stub = _DeleteStub(_unconfirmed_msg())
        stashed = []
        stub._remember_cancelled_pending = lambda lid, rec: stashed.append(lid)

        stub._on_menu_delete_message(0)

        assert stashed == [], (
            "nothing will ever release it — the outcome report it would be "
            "waiting for already happened"
        )

    def test_a_still_pending_send_IS_held(self, monkeypatch, tmp_path):
        """The other half: a genuinely in-flight send must keep the hold, or
        its echo gets handed to the next unrelated message of the same type."""
        _patch_delete_dialog(monkeypatch, tmp_path=tmp_path)
        msg = _still_pending_msg()
        stub = _DeleteStub(msg)
        stub.main_window.cancel_result = False
        stashed = []
        stub._remember_cancelled_pending = lambda lid, rec: stashed.append(lid)

        stub._on_menu_delete_message(0)

        assert msg.get("_cancelled_awaiting_id") is True
        assert stashed == ["loc-2"]
