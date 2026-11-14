"""Offline method checks using synthetic wx-free stand-ins, not desktop/NVDA tests."""
from pathlib import Path
import ast
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]


def method(path, cls, name, **env):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    fn = next(n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), path, 'exec'), env)
    return env[name]


VALID = method('client/ui/dialogs/ai_text_result_dialog.py', 'TextAIResultDialog', '_valid',
               active_account_id=lambda: 'synthetic-account')
REPLACE = method('client/ui/dialogs/ai_writing_dialog.py', 'WritingHelpDialog', '_replace')


def dialog():
    field = NS(value='synthetic original', focus=False, end=False)
    field.GetValue = lambda: field.value
    field.ChangeValue = lambda text: setattr(field, 'value', text)
    field.SetFocus = lambda: setattr(field, 'focus', True)
    field.SetInsertionPointEnd = lambda: setattr(field, 'end', True)
    panel = NS(conversation={'remoteJid': 'synthetic-chat'}, message_field=field,
               _restoring_draft=False, changed=0)
    panel.on_change_message_field = lambda _: setattr(panel, 'changed', panel.changed + 1)
    d = NS(panel=panel, _source_draft=field.value, _latest='synthetic result',
           session=NS(closed=False, identity=('synthetic-account', 'synthetic-chat', 'draft'),
                      locked=False, active=None),
           main_window=NS(_shutting_down=False, _chat_lock_unlocked=False), closed=False)
    d._valid = lambda: VALID(d)
    def close():
        d.closed = True
        d._latest = ''
    d._close = close
    return d


def test_explicit_replace_updates_draft_and_never_sends():
    d = dialog()
    REPLACE(d)
    assert d.panel.message_field.value == 'synthetic result'
    assert d.panel.changed == 1 and d.closed
    assert d.panel.message_field.focus and d.panel.message_field.end
    assert d.panel._restoring_draft is False


def test_changed_draft_is_not_overwritten():
    d = dialog()
    d.panel.message_field.value = 'newer synthetic edit'
    REPLACE(d)
    assert d.panel.message_field.value == 'newer synthetic edit'
    assert not d.closed


def test_wrong_chat_cannot_receive_a_draft_result():
    d = dialog()
    d.panel.conversation = {'remoteJid': 'different synthetic chat'}
    assert not VALID(d)
    REPLACE(d)
    assert d.panel.message_field.value == 'synthetic original'


def test_wrong_account_cannot_receive_a_draft_result():
    d = dialog()
    d.session.identity = ('different-account', 'synthetic-chat', 'draft')
    assert not VALID(d)
    REPLACE(d)
    assert not d.closed


def test_closed_or_locked_or_shutdown_is_invalid():
    for condition in ['closed', 'locked', 'shutdown']:
        d = dialog()
        if condition == 'closed': d.session.closed = True
        if condition == 'locked': d.session.locked = True
        if condition == 'shutdown': d.main_window._shutting_down = True
        assert not VALID(d)


def test_pending_operation_cannot_replace_draft():
    d = dialog()
    d.session.active = object()
    REPLACE(d)
    assert not d.closed
    assert d.panel.message_field.value == 'synthetic original'


def test_no_duplicate_received_translation_menu_or_mixin():
    text = (ROOT / 'client/ui/conversation_panel/ai_writing.py').read_text()
    assert '_on_translate_message' not in text and '_translate_offered' not in text
    text = (ROOT / 'client/ui/conversation_panel/message_menu.py').read_text()
    assert '_translate_offered' not in text
    text = (ROOT / 'client/ui/conversations.py').read_text()
    assert 'TranslationActionsMixin,' in text and 'AIWritingMixin,' in text
