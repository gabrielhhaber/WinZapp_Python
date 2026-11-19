"""The starred messages window (issue #708): its rows, search and the way
to the message. No window is created: pure logic and stubs."""
import types

from core.starred_list import StarredEntry, matching, one_line, row_text, without
import ui.dialogs.starred_messages as starred

TEXTS = {
    "starred_row": "{chat}: {text}",
    "starred_row_sender": "{chat}, {sender}: {text}",
    "starred_no_text": "(no text)",
}
I18N = types.SimpleNamespace(t=lambda key: TEXTS.get(key, key))
GROUP, PERSON = "120363000000000001@g.us", "5511999990000@s.whatsapp.net"


def msg(mid, text="hi", from_me=False, participant="", ts=1_700_000_000):
    return {"key": {"id": mid, "fromMe": from_me, "participant": participant},
            "messageTimestamp": ts, "message": {"conversation": text}}


def test_a_row_reads_chat_sender_text_and_date():
    assert row_text(I18N, "Family", "Ana", "See you\nat 8", "yesterday") == \
        "Family, Ana: See you at 8, yesterday"
    assert row_text(I18N, "Bruno", "", "Ok", "") == "Bruno: Ok"
    assert row_text(I18N, "Bruno", "", "  ", "10:00") == "Bruno: (no text), 10:00"


def test_the_search_finds_every_word_without_accents_or_case():
    rows = [StarredEntry(PERSON, msg("1"), "Bruno: Reunião às 10"),
            StarredEntry(GROUP, msg("2"), "Family, Ana: almoço")]
    assert [e.message_id for e in matching(rows, "reuniao 10")] == ["1"]
    assert [e.message_id for e in matching(rows, "ALMOCO")] == ["2"]
    assert matching(rows, "") == rows
    assert matching(rows, "reuniao almoco") == []


def test_removing_a_star_drops_only_that_message_of_that_chat():
    a, b = StarredEntry(PERSON, msg("1"), "a"), StarredEntry(GROUP, msg("1"), "b")
    assert without([a, b], a) == [b]


class MainWindow:
    def __init__(self):
        self.i18n = I18N
        names = {GROUP: "Family", PERSON: "Bruno"}
        self.chat_display_name = lambda jid: names.get(jid, jid)
        self.self_reference_label = lambda: "You"
        self.navigated, self.jumped, self.lookups = [], [], []
        self.vault_hidden = set()
        self.is_chat_hidden_by_vault = lambda jid: jid in self.vault_hidden
        panel = types.SimpleNamespace(
            conversation=None,
            _get_participant_name=lambda jid, m=None, resolve_missing=True: (
                self.lookups.append(resolve_missing) or {"ana@lid": "Ana"}.get(jid, jid)),
            _get_message_content=lambda m: m["message"]["conversation"],
            _format_date=lambda ts: "today",
            _jump_to_pinned_message=lambda m: self.jumped.append(m["key"]["id"]))
        self.conversations_panel = panel
        self._jid_address_forms = lambda jid: [jid, jid.replace("@s.whatsapp.net", "@lid")]

    def navigate_to_conversation_jid(self, jid, name):
        self.navigated.append((jid, name))
        self.conversations_panel.conversation = {"remoteJid": jid}


def test_rows_name_the_sender_only_in_a_group_or_for_the_account():
    mw = MainWindow()
    assert starred.entry_for(mw, GROUP, msg("1", "Hello", participant="ana@lid")).text == \
        "Family, Ana: Hello, today"
    assert starred.entry_for(mw, PERSON, msg("2", "Oi")).text == "Bruno: Oi, today"
    assert starred.entry_for(mw, PERSON, msg("3", "Sent", from_me=True)).text == \
        "Bruno, You: Sent, today"


def test_a_chat_the_closed_vault_hides_is_not_listed():
    mw = MainWindow()
    mw.vault_hidden = {GROUP}
    rows = [(GROUP, msg("1", "PIN 1234", participant="ana@lid")), (PERSON, msg("2", "Oi"))]
    assert [e.jid for e in starred.entries_for(mw, rows)] == [PERSON]
    mw.vault_hidden = set()          # the vault opened
    assert [e.jid for e in starred.entries_for(mw, rows)] == [GROUP, PERSON]


def test_a_participant_is_never_looked_up_per_row():
    mw = MainWindow()
    starred.entry_for(mw, GROUP, msg("1", participant="ana@lid"))
    assert mw.lookups == [False]


class _UnstarWindow:
    """What StarredMessagesDialog._on_unstar touches, with no window."""

    def __init__(self, mw, entries, started):
        self._mw, self._entries, self._shown = mw, list(entries), list(entries)
        self.shown_with = []
        self._list = types.SimpleNamespace(GetSelection=lambda: 0)
        mw.conversations_panel._sync_message_stars = lambda jid, ms, star: started

    def _selected(self):
        return self._shown[0] if self._shown else None

    def _show(self, keep=None):
        self.shown_with.append(keep)
        self._shown = list(self._entries)


def test_delete_drops_the_row_only_when_the_unstar_started():
    mw = MainWindow()
    entries = [starred.entry_for(mw, PERSON, msg("1")), starred.entry_for(mw, PERSON, msg("2"))]
    refused = _UnstarWindow(mw, entries, started=False)
    starred.StarredMessagesDialog._on_unstar(refused)
    assert [e.message_id for e in refused._entries] == ["1", "2"] and refused.shown_with == []
    started = _UnstarWindow(mw, entries, started=True)
    starred.StarredMessagesDialog._on_unstar(started)
    assert [e.message_id for e in started._entries] == ["2"]


def test_enter_opens_the_conversation_on_the_message(monkeypatch):
    mw = MainWindow()
    later = []
    monkeypatch.setattr(starred.wx, "CallAfter", lambda fn, *a: later.append((fn, a)))
    entry = starred.entry_for(mw, GROUP, msg("M1", participant="ana@lid"))
    starred.open_starred_message(mw, entry)
    assert mw.navigated == [(GROUP, "Family")]
    fn, args = later[0]
    assert fn(*args) is True and mw.jumped == ["M1"]


def test_a_conversation_that_did_not_open_is_left_alone():
    mw = MainWindow()
    mw.conversations_panel.conversation = {"remoteJid": "other@s.whatsapp.net"}
    entry = starred.entry_for(mw, PERSON, msg("M1"))
    assert starred.jump_when_open(mw, entry) is False and mw.jumped == []
    # the same person under the other address form is the same chat
    mw.conversations_panel.conversation = {"remoteJid": PERSON.replace("@s.whatsapp.net", "@lid")}
    assert starred.jump_when_open(mw, entry) is True


def test_one_line_flattens_whitespace():
    assert one_line(" a\r\n b\tc ") == "a b c"
