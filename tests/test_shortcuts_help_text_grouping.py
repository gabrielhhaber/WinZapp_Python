"""F1 list: one heading per context name, no repeated line under a heading,
and call labels only where the shortcut is a call shortcut."""
from types import SimpleNamespace
from core.keyboard_shortcuts import SECTION
from core.shortcut_catalog import SHORTCUTS
from ui.dialogs.shortcuts_tab import shortcuts_help_text


def _window():
    return SimpleNamespace(settings={SECTION: {}, 'general': {}},
                           i18n=SimpleNamespace(t=lambda key: key, language='en-US'))


def _sections(text):
    sections, current = {}, None
    headings = []
    for line in text.split('\n'):
        if not line:
            current = None
            continue
        if current is None:
            current = line
            headings.append(line)
            sections.setdefault(line, [])
        else:
            sections[current].append(line)
    return headings, sections


def test_each_heading_appears_once():
    headings, _ = _sections(shortcuts_help_text(_window(), SimpleNamespace(t=lambda key: key)))
    assert len(headings) == len(set(headings))


def test_no_line_is_repeated_under_a_heading():
    _, sections = _sections(shortcuts_help_text(_window(), SimpleNamespace(t=lambda key: key)))
    for heading, lines in sections.items():
        assert len(lines) == len(set(lines)), heading


def _label(shortcut_id):
    return next(s.label for s in SHORTCUTS if s.id == shortcut_id)


def test_chat_shortcuts_do_not_borrow_call_labels():
    assert _label('chats.ID_ARCHIVE_LIST') == 'archive_chat'
    assert _label('messages.ID_CTRL_C') == 'shortcut_ctrl_c_label'
    assert _label('messages.ID_CTRL_SHIFT_R') == 'shortcut_ctrl_shift_r_label'
    assert _label('messages.ID_BULK_DELETE') == 'shortcut_bulk_delete_label'
    assert _label('status.ID_ESCAPE') == 'close'


def test_mention_contexts_keep_their_own_headings():
    """Enter while typing a mention and Enter on a mention in a message are
    different things; they must not share a heading."""
    headings, sections = _sections(shortcuts_help_text(_window(), SimpleNamespace(t=lambda key: key)))
    assert 'mention_suggestions_label' in headings
    assert 'mentions_section_label' in headings
    assert 'shortcut_scope_mentions' not in headings
    assert 'shortcut_insert_mention: Enter' not in sections['mentions_section_label']
    assert not any(line.startswith('shortcut_insert_mention') for line in sections['mentions_section_label'])
