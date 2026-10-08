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
            self.ids[name] = ([number + digit for digit in range(10)]
                              if 'BOOKMARK' in name else number)
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
