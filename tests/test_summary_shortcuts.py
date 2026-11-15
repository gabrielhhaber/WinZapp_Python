"""Alt+Shift+I and Ctrl+Alt+Shift+I: the AI summary from the focused message to
the end and of the selected messages, shown on their menu items."""
from pathlib import Path
from types import SimpleNamespace as NS

from core.keyboard_shortcuts import ALT, CTRL, SHIFT, default_binding, scopes_overlap
from core.shortcut_catalog import SHORTCUTS
from ui.conversation_panel.summary_actions import SummaryActionsMixin

ROOT = Path(__file__).resolve().parents[1]
SHORTCUT_IDS = {
    "messages.ID_SUMMARIZE_FROM_HERE": (ALT | SHIFT, "Alt+Shift+I"),
    "messages.ID_SUMMARIZE_SELECTED": (CTRL | ALT | SHIFT, "Ctrl+Alt+Shift+I"),
}


def _own(shortcut_id):
    entries = [s for s in SHORTCUTS if s.id == shortcut_id]
    assert len(entries) == 1
    return entries[0]


def test_each_is_the_same_letter_with_its_own_modifiers():
    for shortcut_id, (mod, _name) in SHORTCUT_IDS.items():
        assert default_binding(_own(shortcut_id), lambda key: key) == (mod, ord("I"))


def test_the_only_shared_chord_is_the_chat_list_filter_that_the_message_pane_shadows():
    """Alt+Shift+I is also the chat list's "individual" filter. Both tables are
    shipped defaults, the legal parent/child case (like Ctrl+Shift+M): the
    messages table is on the conversation pane, a child of the panel that holds
    the chats table, so with focus in the messages the summary wins and the
    filter is untouched; in the chat list only the filter exists."""
    own = _own("messages.ID_SUMMARIZE_FROM_HERE")
    taken = default_binding(own, lambda key: key)
    clashes = [s.id for s in SHORTCUTS
               if s.id != own.id and scopes_overlap(own.scope, s.scope)
               and default_binding(s, lambda key: key) == taken]
    assert clashes == ["chats.ID_FILTER_INDIVIDUAL"]
    assert _own("chats.ID_FILTER_INDIVIDUAL").scope == "chats"
    source = (ROOT / "client" / "ui" / "conversation_panel" / "accelerators.py").read_text(encoding="utf-8")
    assert "self.SetAcceleratorTable(accel_tbl)" in source
    assert "self.conversation_panel.SetAcceleratorTable(accel_tbl)" in source


def test_the_shortcuts_tab_raises_no_false_alert_for_the_defaults():
    from core.keyboard_shortcuts import conflicts
    for shortcut_id in SHORTCUT_IDS:
        own = _own(shortcut_id)
        binding = default_binding(own, lambda key: key)
        assert conflicts(own, binding, {}, SHORTCUTS, lambda key: key) == [], shortcut_id
    assert conflicts(_own("chats.ID_FILTER_INDIVIDUAL"), (ALT | SHIFT, ord("I")), {}, SHORTCUTS,
                     lambda key: key) == []


def test_the_selection_chord_is_free_everywhere():
    own = _own("messages.ID_SUMMARIZE_SELECTED")
    taken = default_binding(own, lambda key: key)
    assert [s.id for s in SHORTCUTS if s.id != own.id and default_binding(s, lambda key: key) == taken] == []


def test_alt_shift_r_stays_reply_privately():
    own = [s.id for s in SHORTCUTS if default_binding(s, lambda key: key) == (ALT | SHIFT, ord("R"))]
    assert own == ["messages.ID_ALT_SHIFT_R"]


def test_the_labels_name_the_key_in_every_locale():
    import polib
    for po_path in sorted((ROOT / "translations").glob("*/LC_MESSAGES/winzapp.po")):
        entries = {e.msgctxt: e for e in polib.pofile(str(po_path))}
        assert entries["shortcut_summarize_from_here_label"].msgstr.startswith("Alt+Shift+I: "), po_path
        assert entries["shortcut_summarize_selected_label"].msgstr.startswith("Ctrl+Alt+Shift+I: "), po_path


def test_the_accelerators_are_registered_and_bound():
    source = (ROOT / "client" / "ui" / "conversation_panel" / "accelerators.py").read_text(encoding="utf-8")
    assert 'ord("I"),          self.ID_SUMMARIZE_FROM_HERE)' in source
    assert 'ord("I"),          self.ID_SUMMARIZE_SELECTED)' in source
    assert "self._on_summary_from_here_shortcut, id=self.ID_SUMMARIZE_FROM_HERE)" in source
    assert "self._on_summary_selected_shortcut,  id=self.ID_SUMMARIZE_SELECTED)" in source


def test_the_help_window_lists_them():
    source = (ROOT / "client" / "ui" / "dialogs" / "shortcuts_dialog.py").read_text(encoding="utf-8")
    assert 'i18n.t("shortcut_summarize_from_here_label")' in source
    assert 'i18n.t("shortcut_summarize_selected_label")' in source


class _Menu:
    def __init__(self):
        self.labels = []

    def Append(self, _id, label):
        self.labels.append(label)
        return label


def _menu_stub(from_here, selected, unread):
    return NS(
        main_window=NS(i18n=NS(t=lambda key: key), settings={}),
        _summary_unread_messages=lambda: unread,
        _summary_messages_from=lambda msg: from_here,
        _summary_selected_messages=lambda: selected,
        _ai_settings=lambda: (None, {}),
        _summary_ready=lambda config: True,
        Bind=lambda *args: None,
    )


def test_the_menu_items_carry_the_shortcut_after_a_tab():
    menu = _Menu()
    SummaryActionsMixin._append_summary_items(_menu_stub([{}], [{}], [{}]), menu, {})
    assert menu.labels == ["ai_summary_unread_menu\tCtrl+Shift+U",
                           "ai_summary_from_here_menu\tAlt+Shift+I",
                           "ai_summary_selected_menu\tCtrl+Alt+Shift+I"]


def test_an_item_without_rows_is_not_added():
    menu = _Menu()
    SummaryActionsMixin._append_summary_items(_menu_stub([{}], [], []), menu, {})
    assert menu.labels == ["ai_summary_from_here_menu\tAlt+Shift+I"]


def _handler_stub(rows, focused, selected_rows):
    calls = []
    stub = NS(
        messages_list=NS(GetFirstSelected=lambda: focused),
        _sorted_messages=rows,
        _is_separator=lambda row: row.get("separator", False),
        _summary_messages_from=lambda msg: [msg, "newer"],
        _summary_selected_messages=lambda: selected_rows,
        _on_summarize_messages=lambda messages, **kw: calls.append((messages, kw)),
    )
    return stub, calls


def test_from_here_shortcut_summarizes_from_the_focused_message():
    row = {"key": {"id": "a"}}
    stub, calls = _handler_stub([row], 0, [])
    SummaryActionsMixin._on_summary_from_here_shortcut(stub)
    assert calls == [([row, "newer"], {"follows_open_chat": True})]


def test_from_here_shortcut_with_no_focused_message_asks_with_nothing():
    stub, calls = _handler_stub([{"separator": True}], 0, [])
    SummaryActionsMixin._on_summary_from_here_shortcut(stub)
    stub, calls2 = _handler_stub([], -1, [])
    SummaryActionsMixin._on_summary_from_here_shortcut(stub)
    assert calls == [([], {"follows_open_chat": True})]
    assert calls2 == [([], {"follows_open_chat": True})]


def test_selected_shortcut_summarizes_the_selection():
    stub, calls = _handler_stub([], -1, ["x", "y"])
    SummaryActionsMixin._on_summary_selected_shortcut(stub)
    assert calls == [(["x", "y"], {"follows_open_chat": True})]


def test_an_empty_summary_says_there_is_nothing_to_summarize():
    spoken = []
    mw = NS(i18n=NS(t=lambda key: key), is_chat_hidden_by_vault=lambda jid: False,
            output=lambda text, **kw: spoken.append(text))
    stub = NS(main_window=mw, _summary_voice_running=lambda: False,
              conversation={"remoteJid": "x@s.whatsapp.net"}, _ai_dialog=None,
              _ai_settings=lambda: (None, {}), _summary_ready=lambda config: True)
    SummaryActionsMixin._on_summarize_messages(stub, [], follows_open_chat=True)
    assert spoken == ["ai_summary_nothing"]
