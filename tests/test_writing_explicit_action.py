"""Offline checks of the production open hook, with no wx or provider access."""
from pathlib import Path
import ast
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]


def production_method(file, cls, method, **env):
    tree = ast.parse((ROOT / file).read_text(encoding='utf-8'))
    owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    fn = next(n for n in owner.body if isinstance(n, ast.FunctionDef) and n.name == method)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), file, 'exec'), env)
    return env[method]


def test_open_does_not_schedule_consent_worker_or_request_even_with_saved_consent():
    calls = []
    def forbidden(*args, **kwargs):
        calls.append('request')
        raise AssertionError('opening writing help must not start work')
    hook = production_method('client/ui/dialogs/ai_writing_dialog.py', 'WritingHelpDialog', '_on_open',
                             wx=NS(CallAfter=forbidden))
    d = NS(status=NS(ChangeValue=lambda t: calls.append(('status', t))),
           action=NS(SetFocus=lambda: calls.append('action-focus')),
           i18n=NS(t=lambda key: key), _regenerate=forbidden, _consent=forbidden,
           _worker=forbidden, config={'consented_writing': ['gemini'], 'consented_translate': True})
    hook(d)
    assert calls == [('status', 'writing_choose_before_run'), 'action-focus']


def test_shared_windows_keep_existing_autostart():
    calls = []
    run = object()
    hook = production_method('client/ui/dialogs/ai_text_result_dialog.py', 'TextAIResultDialog', '_on_open',
                             wx=NS(CallAfter=lambda fn: calls.append(fn)))
    hook(NS(result=NS(SetFocus=lambda: calls.append('result-focus')), _regenerate=run))
    assert calls == ['result-focus', run]


def test_action_change_only_updates_choice_controls():
    calls = []
    hook = production_method('client/ui/dialogs/ai_writing_dialog.py', 'WritingHelpDialog', '_action_changed')
    hook(NS(_sync_target=lambda: calls.append('target')), NS(Skip=lambda: calls.append('skip')))
    assert calls == ['target', 'skip']


def test_constructor_uses_overridable_open_hook_not_direct_start():
    tree = ast.parse((ROOT / 'client/ui/dialogs/ai_text_result_dialog.py').read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'TextAIResultDialog')
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
    calls = [n for n in ast.walk(init) if isinstance(n, ast.Call)]
    assert any(isinstance(n.func, ast.Attribute) and n.func.attr == '_on_open' for n in calls)
    assert not any(isinstance(n.func, ast.Attribute) and n.func.attr in ('CallAfter', '_regenerate', 'submit', '_worker', '_consent') for n in calls)


def test_close_before_run_disposes_without_consent_or_request():
    calls = []
    dispose = production_method('client/ui/dialogs/ai_text_result_dialog.py', 'TextAIResultDialog', '_dispose',
                                record=lambda *a, **kw: None)
    close = production_method('client/ui/dialogs/ai_text_result_dialog.py', 'TextAIResultDialog', '_close')
    session = NS(closed=False)
    def stop_session():
        session.closed = True
        calls.append('session-closed')
    session.close = stop_session
    d = NS(session=session, _processing=NS(stop=lambda: None), _latest='', _timer=None,
           result=NS(ChangeValue=lambda t: None), status=NS(ChangeValue=lambda t: None),
           _consent_dialog=None, _finish_close=lambda: calls.append('window-closed'))
    d._dispose = lambda: dispose(d)
    close(d)
    assert calls == ['session-closed', 'window-closed']


def test_explicit_run_uses_current_choice_and_submits_once():
    calls = []
    later = NS(Stop=lambda: None)
    run = production_method('client/ui/dialogs/ai_text_result_dialog.py', 'TextAIResultDialog', '_regenerate',
                            record=lambda *a, **kw: None,
                            wx=NS(CallLater=lambda *a: later, CallAfter=lambda *a: None),
                            submit=lambda work, done: calls.append(('submit', work())),
                            _WATCHDOG_GRACE_SECONDS=5,
                            DescriptionError=RuntimeError, CredentialError=ValueError)
    for chosen in ['check', 'professional', 'translate']:
        calls.clear()
        operation = NS(total_seconds=60)
        session = NS(active=None, begin=lambda q: (1, operation))
        d = NS(session=session, _valid=lambda: True, _consent=lambda: calls.append('consent') or True,
               _question=lambda: chosen, _busy=lambda busy: None,
               i18n=NS(t=lambda key: key), status=NS(ChangeValue=lambda t: None), _speak=lambda t: None,
               _worker=lambda op: (lambda: chosen), _timeout=lambda generation: None)
        run(d)
        assert calls == ['consent', ('submit', chosen)]
