"""Tests for a reaction that arrives for a chat the user is NOT looking at.

Reported live: "quando alguém reage a uma mensagem, o toast vem, mas não
consta a reação na mensagem".  The toast is the tell — MainWindow.
_maybe_notify_reaction() only raises a toast when the window is in the
background, i.e. exactly when the reacted-to conversation is very unlikely
to be the one open in ConversationsPanel.

Root cause: main.py's on_new_message() deliberately never appends a
reactionMessage to a chat's own `records` (a raw reaction record must not
become a chat-list preview), so the ONLY thing that files a live reaction
anywhere is ConversationsPanel — and its reaction handling sat *behind*
on_incoming_message()'s "is this conversation open?" early return.  A
reaction for any other chat was therefore applied nowhere at all: it
notified, and then simply did not exist.  Opening the conversation
afterwards rebuilds _reaction_map by scanning `records`, which never
received it.

Reactions are now handled before that guard (apply_incoming_reaction()):
persisting is unconditional, only the live redraw of the message row still
depends on the chat being the one on screen.

ConversationsPanel is a wx.Panel and cannot be instantiated without a
running wx.App, so the methods are exercised against a small stub — same
approach as tests/test_reaction_persisted_from_others.py.
"""

from ui.conversations import ConversationsPanel


class _FakeMessagesList:
    def __init__(self):
        self.items = []

    def Freeze(self):
        pass

    def Thaw(self):
        pass

    def InsertItem(self, pos, text):
        self.items.insert(pos, text)

    def GetFocusedItem(self):
        return -1

    def SetItemText(self, pos, text):
        self.items[pos] = text


class _FakeDB:
    def __init__(self):
        self.inserted = []

    def queue_message(self, jid, record):
        self.inserted.append((jid, record))


class _FakeMainWindow:
    def __init__(self, chats):
        self.chats = chats
        self.db = _FakeDB()
        self._lid_to_phone = {}
        self._phone_to_lid = {}

    def _allow_ui_focus_changes(self):
        return False

    def get_chat(self, jid):
        chat = self.chats.get(jid)
        if chat is not None:
            return chat
        alt = (self._lid_to_phone.get(jid, "") if jid.endswith("@lid")
               else self._phone_to_lid.get(jid, ""))
        return self.chats.get(alt) if alt else None


class _Stub:
    on_incoming_message        = ConversationsPanel.on_incoming_message
    apply_incoming_reaction    = ConversationsPanel.apply_incoming_reaction
    _matches_open_conversation = ConversationsPanel._matches_open_conversation
    _persist_reaction_record   = ConversationsPanel._persist_reaction_record
    _reactor_key_from_msg      = ConversationsPanel._reactor_key_from_msg
    _is_separator              = ConversationsPanel._is_separator
    _SELF_REACTOR_KEY          = ConversationsPanel._SELF_REACTOR_KEY

    def __init__(self, open_jid, chats):
        self.main_window = _FakeMainWindow(chats)
        self.messages_list = _FakeMessagesList()
        self._sorted_messages = []
        self.conversation = {"remoteJid": open_jid} if open_jid else None
        self._reaction_map = {}
        self._render_message_line = lambda msg, *a, **kw: msg["key"]["id"]


def _chat(jid, messages=None):
    return {
        "remoteJid": jid,
        "messages": {"messages": {"records": list(messages or [])}},
    }


def _orig_msg(mid="m1"):
    return {"key": {"id": mid, "fromMe": True},
            "messageType": "conversation", "message": {"conversation": "oi"}}


def _reaction(orig_id="m1", emoji="\U0001F44D", chat_jid="", reactor=""):
    key = {"id": "R" + orig_id, "fromMe": False, "remoteJid": chat_jid}
    if reactor:
        key["participant"] = reactor
    return {
        "key": key,
        "messageType": "reactionMessage",
        "message": {"reactionMessage": {
            "text": emoji,
            "key": {"id": orig_id, "fromMe": True, "remoteJid": chat_jid},
        }},
    }


OPEN_JID = "5511111111111@s.whatsapp.net"
OTHER_JID = "5522222222222@s.whatsapp.net"
THUMBS_UP = "\U0001F44D"


def _rebuild_reaction_map(records):
    """Mirror of the _reaction_map rebuild populate_messages() runs over
    `records` every time it repopulates the message list."""
    rmap: dict = {}
    for m in records:
        if m.get("messageType") != "reactionMessage":
            continue
        reaction = (m.get("message") or {}).get("reactionMessage") or {}
        orig_id = (reaction.get("key") or {}).get("id", "")
        key = m.get("key", {})
        sender = ("_me_" if key.get("fromMe")
                  else key.get("participant") or key.get("remoteJid") or "")
        if not orig_id or not sender:
            continue
        if reaction.get("text", ""):
            rmap.setdefault(orig_id, {})[sender] = reaction["text"]
        else:
            rmap.setdefault(orig_id, {}).pop(sender, None)
    return rmap


class TestReactionForAChatThatIsNotOpen:
    def _panel(self):
        chats = {OPEN_JID: _chat(OPEN_JID),
                 OTHER_JID: _chat(OTHER_JID, [_orig_msg()])}
        return _Stub(OPEN_JID, chats), chats

    def test_reaction_is_persisted_into_the_closed_chats_records(self):
        panel, chats = self._panel()
        panel.on_incoming_message(
            OTHER_JID, _reaction(chat_jid=OTHER_JID, reactor=OTHER_JID))
        records = chats[OTHER_JID]["messages"]["messages"]["records"]
        reactions = [r for r in records if r["messageType"] == "reactionMessage"]
        assert len(reactions) == 1
        assert reactions[0]["message"]["reactionMessage"]["text"] == THUMBS_UP

    def test_reaction_is_written_to_the_database(self):
        panel, _ = self._panel()
        panel.on_incoming_message(
            OTHER_JID, _reaction(chat_jid=OTHER_JID, reactor=OTHER_JID))
        assert [jid for jid, _ in panel.main_window.db.inserted] == [OTHER_JID]

    def test_the_persisted_record_shows_up_when_that_chat_is_opened(self):
        panel, chats = self._panel()
        panel.on_incoming_message(
            OTHER_JID, _reaction(chat_jid=OTHER_JID, reactor=OTHER_JID))
        rmap = _rebuild_reaction_map(
            chats[OTHER_JID]["messages"]["messages"]["records"])
        assert rmap == {"m1": {OTHER_JID: THUMBS_UP}}

    def test_the_open_conversations_reaction_map_is_left_alone(self):
        panel, _ = self._panel()
        panel.on_incoming_message(
            OTHER_JID, _reaction(chat_jid=OTHER_JID, reactor=OTHER_JID))
        # _reaction_map only ever describes the conversation on screen —
        # populate_messages() rebuilds it per conversation.
        assert panel._reaction_map == {}

    def test_reaction_is_persisted_even_with_no_conversation_open_at_all(self):
        chats = {OTHER_JID: _chat(OTHER_JID, [_orig_msg()])}
        panel = _Stub(None, chats)
        panel.on_incoming_message(
            OTHER_JID, _reaction(chat_jid=OTHER_JID, reactor=OTHER_JID))
        records = chats[OTHER_JID]["messages"]["messages"]["records"]
        assert any(r["messageType"] == "reactionMessage" for r in records)

    def test_a_reaction_for_the_open_chat_still_updates_the_live_map(self):
        chats = {OPEN_JID: _chat(OPEN_JID, [_orig_msg()])}
        panel = _Stub(OPEN_JID, chats)
        panel._sorted_messages = [_orig_msg()]
        panel.messages_list.items = ["m1"]
        panel.on_incoming_message(
            OPEN_JID, _reaction(chat_jid=OPEN_JID, reactor=OPEN_JID))
        assert panel._reaction_map == {"m1": {OPEN_JID: THUMBS_UP}}
        assert panel.messages_list.items == ["m1"]  # row re-rendered in place

    def test_reaction_arriving_under_the_lid_form_is_filed_under_the_phone_jid(self):
        lid = "187707351400583@lid"
        chats = {OTHER_JID: _chat(OTHER_JID, [_orig_msg()])}
        panel = _Stub(OPEN_JID, chats)
        panel.main_window._lid_to_phone = {lid: OTHER_JID}
        panel.on_incoming_message(lid, _reaction(chat_jid=lid, reactor=lid))
        records = chats[OTHER_JID]["messages"]["messages"]["records"]
        rxn = next(r for r in records if r["messageType"] == "reactionMessage")
        # Filed under the chat's own canonical JID, not the @lid the event
        # happened to arrive under — otherwise the DB row lands in a bucket
        # the conversation never reads back.
        assert rxn["key"]["remoteJid"] == OTHER_JID
        assert [jid for jid, _ in panel.main_window.db.inserted] == [OTHER_JID]
