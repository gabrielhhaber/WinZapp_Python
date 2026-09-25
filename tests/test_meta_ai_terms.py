"""Meta AI refuses every message until its terms are accepted.

Measured on a live session (2026-09-25): a send to 13135550002@c.us came back
ack=-1 / ackErrorCode 488 -- also from WhatsApp Web's own sendTextMsgToChat --
while TosManager.getState('20250502') was NOT_ACCEPTED. WinZapp therefore asks
the user to accept first. Nothing here opens a window: the dialog class is
replaced by a stub, the way the other MainWindow tests bind unbound methods.
"""

from pathlib import Path

import pytest

from core.meta_ai import (
    META_AI_TERMS_URL,
    STATE_ACCEPTED,
    STATE_NOT_ACCEPTED,
    STATE_UNKNOWN,
    is_meta_ai_jid,
    terms_state,
)
from main import MainWindow
from ui.conversations import ConversationsPanel

ROOT = Path(__file__).resolve().parents[1]


class TestIsMetaAiJid:
    @pytest.mark.parametrize("jid", [
        "13135550002@s.whatsapp.net",
        "13135550002@c.us",
        "13135550002:5@c.us",
        "867051314767696@bot",
    ])
    def test_the_assistant(self, jid):
        assert is_meta_ai_jid(jid)

    @pytest.mark.parametrize("jid", [
        "5511999999999@s.whatsapp.net",
        "13135550002@g.us",
        "13135551234@s.whatsapp.net",
        "99913135550002@s.whatsapp.net",
        "68904344899801@lid",
        "", None, 5,
    ])
    def test_everyone_else(self, jid):
        assert not is_meta_ai_jid(jid)


class TestTermsState:
    def test_accepted(self):
        assert terms_state({"response": {"state": "ACCEPTED"}}) == STATE_ACCEPTED

    def test_not_accepted(self):
        assert terms_state({"response": {"state": "NOT_ACCEPTED"}}) == STATE_NOT_ACCEPTED

    @pytest.mark.parametrize("body", [
        {"response": {"state": "UNKNOWN"}}, {"response": None},
        {"status": "Disconnected"}, None, "oops", {"response": {"state": "SHOWN"}},
    ])
    def test_anything_else_never_blocks(self, body):
        assert terms_state(body) == STATE_UNKNOWN


class _Dialog:
    answer = None

    def __init__(self, parent, i18n):
        _Dialog.created += 1

    def ShowModal(self):
        import wx
        return wx.ID_OK if _Dialog.answer else wx.ID_CANCEL

    def Destroy(self):
        pass


class _Window:
    ensure_meta_ai_terms = MainWindow.ensure_meta_ai_terms

    def __init__(self, state, accept_ok=True):
        self._state = state
        self._accept_ok = accept_ok
        self.probes = 0
        self.accepts = 0
        self.i18n = type("I", (), {"t": staticmethod(lambda key: key)})()

    def meta_ai_terms_state(self):
        self.probes += 1
        return self._state

    def accept_meta_ai_terms(self):
        self.accepts += 1
        return self._accept_ok


@pytest.fixture
def dialog(monkeypatch):
    import ui.dialogs.meta_ai_terms as module
    import wx
    _Dialog.created = 0
    _Dialog.answer = True
    monkeypatch.setattr(module, "MetaAiTermsDialog", _Dialog)
    boxes = []
    monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: boxes.append(a))
    _Dialog.boxes = boxes
    return _Dialog


META_AI = "13135550002@s.whatsapp.net"


class TestEnsureTerms:
    def test_other_chats_are_never_probed(self, dialog):
        window = _Window(STATE_NOT_ACCEPTED)
        assert window.ensure_meta_ai_terms("5511999999999@s.whatsapp.net")
        assert window.probes == 0 and dialog.created == 0

    def test_already_accepted_sends_without_asking(self, dialog):
        window = _Window(STATE_ACCEPTED)
        assert window.ensure_meta_ai_terms(META_AI)
        assert dialog.created == 0
        window.ensure_meta_ai_terms(META_AI)
        assert window.probes == 1  # remembered

    def test_an_unreadable_state_does_not_block(self, dialog):
        window = _Window(STATE_UNKNOWN)
        assert window.ensure_meta_ai_terms(META_AI)
        assert dialog.created == 0

    def test_not_accepted_asks_then_records_the_acceptance(self, dialog):
        window = _Window(STATE_NOT_ACCEPTED)
        assert window.ensure_meta_ai_terms(META_AI)
        assert dialog.created == 1 and window.accepts == 1
        window.ensure_meta_ai_terms(META_AI)
        assert dialog.created == 1  # not asked again this session

    def test_declining_sends_nothing_and_records_nothing(self, dialog):
        dialog.answer = False
        window = _Window(STATE_NOT_ACCEPTED)
        assert not window.ensure_meta_ai_terms(META_AI)
        assert window.accepts == 0

    def test_a_failed_acceptance_says_so_and_does_not_send(self, dialog):
        window = _Window(STATE_NOT_ACCEPTED, accept_ok=False)
        assert not window.ensure_meta_ai_terms(META_AI)
        assert dialog.boxes and dialog.boxes[0][0] == "meta_ai_terms_failed"
        assert not getattr(window, "_meta_ai_terms_accepted", False)


class _Field:
    def __init__(self, text):
        self.text = text

    def GetValue(self):
        return self.text


class _Panel:
    on_send_message = ConversationsPanel.on_send_message

    def __init__(self, allow):
        self.conversation = {"remoteJid": META_AI}
        self.message_field = _Field("oi")
        self._editing_message_id = None
        self.main_window = type("W", (), {
            "ensure_meta_ai_terms": staticmethod(lambda jid: allow)})()
        self.sent = []

    def _send_new_text_message(self, text, remote_jid):
        self.sent.append((text, remote_jid))


class TestComposerGate:
    def test_accepted_terms_send_normally(self):
        panel = _Panel(allow=True)
        panel.on_send_message(None)
        assert panel.sent == [("oi", META_AI)]

    def test_declined_terms_send_nothing_and_allow_a_retry(self):
        panel = _Panel(allow=False)
        panel.on_send_message(None)
        assert panel.sent == []
        # The duplicate-press guard must not swallow the retry.
        panel.main_window = type("W", (), {
            "ensure_meta_ai_terms": staticmethod(lambda jid: True)})()
        panel.on_send_message(None)
        assert panel.sent == [("oi", META_AI)]


class TestNodeSide:
    def test_routes_and_controller_exist(self):
        routes = (ROOT / "client/api_patches/src/routes/index.ts").read_text(encoding="utf-8")
        controller = (ROOT / "client/api_patches/src/controller/deviceController.ts").read_text(encoding="utf-8")
        assert "'/api/:session/meta-ai-terms'" in routes
        assert "'/api/:session/meta-ai-terms/accept'" in routes
        assert "export async function getMetaAiTerms" in controller
        assert "export async function acceptMetaAiTerms" in controller
        assert "maybeUpdateServer" in controller

    def test_the_link_is_metas_own_terms_page(self):
        assert META_AI_TERMS_URL == "https://www.facebook.com/legal/ai-terms"


# ── Meta AI's replies: rich_response, no body ───────────────────────────────
#
# Measured over CDP (2026-09-25): the reply to "Teste" was type "rich_response"
# with richResponse.fragments[Text] and unifiedResponse, and no body at all --
# WinZapp listed it as "Meta AI:  , 19:27" with nothing after the name.

from core.meta_ai import rich_response_text
from core.websocket_client import WebSocketClient

REPLY = "Teste recebido loud and clear! 🔊\n\nTô 100% por aqui. O que você quer testar hoje?"


def _rich_reply(**overrides):
    raw = {
        "id": "false_13135550002@c.us_B0C0EFC18D4D0F88EFE8149B61AF7197",
        "type": "rich_response", "t": 1790375191, "from": "13135550002@c.us",
        "to": "555195510189@c.us", "fromMe": False, "notifyName": "Meta AI",
        "richResponse": {"parseState": "Parsed", "type": "Standard",
                         "fragments": [{"type": "Text", "text": REPLY}]},
        "unifiedResponse": {"sections": [{"view_model": {"primitive": {
            "__typename": "GenAIMarkdownTextUXPrimitive", "text": REPLY}}}]},
    }
    raw.update(overrides)
    return raw


class _Normalizer:
    _normalize_wpp_message = WebSocketClient._normalize_wpp_message
    _clean_jid = WebSocketClient._clean_jid


class TestRichResponseText:
    def test_fragments(self):
        assert rich_response_text(_rich_reply()) == REPLY

    def test_several_blocks_are_separated(self):
        raw = _rich_reply(richResponse={"fragments": [
            {"type": "Text", "text": "um"}, {"type": "Code", "code": "x"},
            {"type": "Text", "text": "dois"}]})
        assert rich_response_text(raw) == "um\n\ndois"

    def test_the_unified_layout_is_the_fallback(self):
        assert rich_response_text(_rich_reply(richResponse=None)) == REPLY

    @pytest.mark.parametrize("raw", [{}, None, {"richResponse": {"fragments": []}},
                                     {"richResponse": "x"}, {"unifiedResponse": {"sections": [{}]}}])
    def test_nothing_readable_is_empty(self, raw):
        assert rich_response_text(raw) == ""


class TestNormalizedReply:
    def test_the_reply_carries_its_text(self):
        result = _Normalizer()._normalize_wpp_message(_rich_reply())
        assert result["message"] == {"conversation": REPLY}
        assert result["messageType"] == "conversation"
        assert result["key"]["remoteJid"] == "13135550002@s.whatsapp.net"
        assert result["key"]["fromMe"] is False
        assert result["pushName"] == "Meta AI"

    def test_a_reply_with_no_text_is_left_as_it_was(self):
        raw = _rich_reply(richResponse=None, unifiedResponse=None)
        result = _Normalizer()._normalize_wpp_message(raw)
        assert result["message"] == {}
        assert result["messageType"] == "rich_response"


# ── A reply stored before its words arrived is completed, not "edited" ──────

class _Editor:
    _apply_possible_edit = MainWindow._apply_possible_edit

    def __init__(self):
        self.persisted = []
        self.db = type("DB", (), {"insert_message": lambda *a, **k: None})()
        self._msg_bg_executor = type("Ex", (), {"submit": lambda self, fn: fn()})()

    def _schedule_set_chats(self):
        pass

    def _apply_remote_revoke(self, existing, incoming, remote_jid):
        return False

    def _persist_and_repaint_edit(self, existing, remote_jid):
        self.persisted.append((existing["key"]["id"], remote_jid))


def _stored(message_type, message):
    return {"key": {"id": "B0C0", "remoteJid": META_AI, "fromMe": False},
            "messageType": message_type, "message": message}


def _arrived(text):
    return {"key": {"id": "B0C0", "remoteJid": META_AI, "fromMe": False},
            "messageType": "conversation", "message": {"conversation": text}}


class TestStreamedReplyCompletes:
    def test_an_empty_stored_reply_takes_the_filled_copy(self):
        editor, existing = _Editor(), _stored("rich_response", {})
        editor._apply_possible_edit(existing, _arrived(REPLY), META_AI)
        assert existing["message"] == {"conversation": REPLY}
        assert existing["messageType"] == "conversation"
        assert "_edited" not in existing
        assert editor.persisted == [("B0C0", META_AI)]

    def test_a_reply_that_still_has_no_text_changes_nothing(self):
        editor, existing = _Editor(), _stored("rich_response", {})
        editor._apply_possible_edit(existing, _stored("rich_response", {}), META_AI)
        assert existing["message"] == {} and editor.persisted == []

    def test_an_ordinary_message_is_still_an_edit(self):
        editor = _Editor()
        existing = _stored("conversation", {"conversation": "antes"})
        editor._apply_possible_edit(existing, _arrived("depois"), META_AI)
        assert existing["message"] == {"conversation": "depois"}
        assert existing["_edited"] is True


class TestUnknownStateIsNotReprobedEveryMessage:
    def test_a_failed_probe_is_not_repeated_within_a_minute(self, dialog):
        window = _Window(STATE_UNKNOWN)
        assert window.ensure_meta_ai_terms(META_AI)
        assert window.ensure_meta_ai_terms(META_AI)
        assert window.probes == 1
