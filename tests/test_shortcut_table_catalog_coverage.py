"""Every actual accelerator expression must resolve to the shipping catalog.

Evaluate only the nine literal table expressions against recording IDs/wx
constants. No wx objects, App, windows or actual keyboard registration.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
import pytest
import wx
from core.keyboard_shortcuts import SECTION
from ui import shortcut_bindings as keys

ROOT = Path(__file__).resolve().parents[1] / 'client'
TABLES = [
    ('main_window/shortcuts.py', 'main'),
    ('ui/conversation_panel/accelerators.py', 'chats'),
    ('ui/conversation_panel/accelerators.py', 'messages'),
    ('ui/conversation_panel/archived_panel.py', 'archived'),
    ('ui/chat_lock.py', 'locked'), ('status_panel.py', 'status'),
    ('calls_panel.py', 'calls'), ('ui/media_viewer.py', 'media'), ('main.py', 'call'),
]


class Owner:
    def __init__(self):
        self.settings = {SECTION: {}}
        self.i18n = SimpleNamespace(t=lambda key: {'main_nav': '&Navigation',
            'messages': '&Messages', 'type_message': 'Ty&pe message'}.get(key, key))
        self.ids = {}

    def __getattr__(self, name):
        if not name.startswith('ID_') and name != 'CTRL_W':
            raise AttributeError(name)
        if name not in self.ids:
            number = 1000 + len(self.ids) * 10
            # The numbered families are lists indexed by digit: the bookmarks
            # and the ten review-by-position commands (ID_REVIEW[d]).
            self.ids[name] = ([number + digit for digit in range(10)]
                              if 'BOOKMARK' in name or name in ('ID_REVIEW', 'ID_QUICK_REACTION') else number)
        return self.ids[name]


@pytest.mark.parametrize('path,scope', TABLES)
def test_actual_table_is_fully_configurable(path, scope, monkeypatch):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8-sig'))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == 'make_shortcut_table'
             and isinstance(n.args[1], ast.Constant) and n.args[1].value == scope]
    assert len(calls) == 1
    owner = Owner()
    env = dict(wx=wx, self=owner, ord=ord, range=range, str=str,
               CS=6, AS=5, CAS=7, nav_letter='N', focus_field_letter='P',
               focus_list_letter='M', messages_letter='M',
               mnemonic_letter=lambda text, default: text[text.index('&') + 1].upper(),
               i18n=owner.i18n)
    expression = ast.Expression(calls[0].args[2])
    entries = eval(compile(expression, path, 'eval'), {'__builtins__': {}}, env)
    monkeypatch.setattr(wx, 'AcceleratorTable', lambda rows: tuple(rows))
    table = keys.make_shortcut_table(owner, scope, entries)
    assert table and owner._configured_shortcut_rows
    # None must remove every entry, including aliases and parent-shared IDs.
    owner.settings[SECTION] = {s.id: None for s, _ in owner._configured_shortcut_rows}
    assert keys._table(owner._configured_shortcut_rows, owner) == ()


def _messages_table(monkeypatch, overrides=None):
    """The real `messages` table, evaluated as above: (owner, entries)."""
    path = 'ui/conversation_panel/accelerators.py'
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8-sig'))
    [call] = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Name) and n.func.id == 'make_shortcut_table'
              and isinstance(n.args[1], ast.Constant) and n.args[1].value == 'messages']
    owner = Owner()
    owner.settings[SECTION] = dict(overrides or {})
    env = dict(wx=wx, self=owner, ord=ord, range=range, str=str,
               CS=6, AS=5, CAS=7, nav_letter='N', focus_field_letter='P',
               focus_list_letter='M', messages_letter='M',
               mnemonic_letter=lambda text, default: text[text.index('&') + 1].upper(),
               i18n=owner.i18n)
    entries = eval(compile(ast.Expression(call.args[2]), path, 'eval'), {'__builtins__': {}}, env)
    monkeypatch.setattr(wx, 'AcceleratorTable', lambda rows: tuple(rows))
    return owner, keys.make_shortcut_table(owner, 'messages', entries)


def test_alt_shift_j_and_o_go_forward_in_the_messages_table(monkeypatch):
    owner, table = _messages_table(monkeypatch)
    assert (5, ord('J'), owner.ID_ALT_SHIFT_J) in table
    assert (5, ord('O'), owner.ID_ALT_SHIFT_O) in table


def test_the_review_positions_have_no_key_until_one_is_chosen(monkeypatch):
    owner, table = _messages_table(monkeypatch)
    assert not any(entry[2] in owner.ID_REVIEW for entry in table)
    owner, table = _messages_table(monkeypatch, {'messages.ID_REVIEW[1]': [5, ord('W')]})
    assert (5, ord('W'), owner.ID_REVIEW[1]) in table
