"""Offline adaptation checks. UI methods execute against plain stubs, not wx/NVDA.

Run without pytest: PYTHONPATH=client python -m unittest discover -s tests
-p test_unread_summary_adaptation.py -v
"""
import ast
import gettext
import io
import json
from pathlib import Path
import socket
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

try:
    from babel.messages.pofile import read_po
    from babel.messages.mofile import write_mo
except ImportError:
    read_po = write_mo = None

from core.ai_media import config, service
from core.ai_media.errors import DescriptionError, status_error
from core.ai_media.payload import Media, prepare_media
from core.ai_media.prompts import first_question, instructions
from core.ai_media.providers import build_request, parse_answer
from core.ai_media.summary import build_unread_transcript, message_text
from core.keyboard_shortcuts import ALT, SHIFT, default_binding, scopes_overlap
from core.shortcut_catalog import SHORTCUTS

ROOT = Path(__file__).resolve().parents[1]


def load_methods(path, class_name, names, **namespace):
    """Execute the exact method ASTs without importing desktop dependencies."""
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    selected = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert {n.name for n in selected} == set(names)
    cls.body = selected
    cls.bases = []
    cls.decorator_list = []
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), path, 'exec'), namespace)
    return namespace[class_name]


def msg(text='hola', id='m1', **kwargs):
    return dict(messageType='conversation', message={'conversation': text}, key={'id': id}, **kwargs)


def panel(rows, sep=-1, anchor=None):
    p = NS(conversation={'remoteJid': 'synthetic-chat', 'unreadCount': 0},
           _sorted_messages=rows, _unread_sep_idx=sep, _first_unread_msg_id=anchor,
           _is_separator=lambda m: m.get('_type') in ('unread_separator', 'empty_placeholder'),
           main_window=NS(is_chat_locked=lambda _: False, _chat_lock_unlocked=False))
    p._unread_slice = lambda: Actions._unread_slice(p)
    return p


saved = {'gemini'}
Actions = load_methods('client/ui/conversation_panel/ai_actions.py', 'AIActionsMixin',
    {'_unread_slice', '_summary_offered', '_update_summary_button'},
    ai_config=config, CredentialStore=lambda _: NS(saved=lambda: saved), global_dir=lambda: 'unused')
Dialog = load_methods('client/ui/dialogs/ai_result_dialog.py', 'AIResultDialog',
    {'_consent_key', '_consent'}, wx=NS(ID_OK=1))


class OfflineAdaptation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.socket_guard = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden in offline tests'))
        cls.socket_guard.start()
        cls.catalogs = {}
        if read_po is None:
            return
        for loc in json.loads((ROOT / 'client/languages/language_map.json').read_text()):
            with (ROOT / 'translations' / loc / 'LC_MESSAGES/winzapp.po').open('rb') as f:
                cls.catalogs[loc] = read_po(f, locale=loc.replace('-', '_'))

    @classmethod
    def tearDownClass(cls):
        cls.socket_guard.stop()

    def test_transcript_order_sender_and_whitespace(self):
        self.assertEqual(build_unread_transcript([msg('uno\n dos'), msg('tres')], lambda _: 'Ana'), ('Ana: uno dos\nAna: tres', 2))

    def test_media_captions_and_placeholders(self):
        self.assertEqual(message_text({'messageType':'imageMessage','message':{'imageMessage':{'caption':'foto'}}}), 'foto')
        self.assertEqual(message_text({'messageType':'audioMessage'}), '[Voice message]')
        for kind in ['reactionMessage', 'protocolMessage', 'unknown']:
            self.assertIsNone(message_text({'messageType':kind}))

    def test_keeps_newest_200_messages(self):
        text, count = build_unread_transcript([msg(str(i)) for i in range(300)], lambda _: 'A')
        self.assertEqual(count, 200)
        self.assertTrue(text.startswith('A: 100\n'))
        self.assertTrue(text.endswith('A: 299'))

    def test_char_cap_across_messages(self):
        text, count = build_unread_transcript([msg(str(i)*40) for i in range(10)], lambda _: 'A', max_chars=100)
        self.assertLessEqual(len(text), 100)
        self.assertTrue(text.endswith('9'*40))
        self.assertLess(count, 10)

    def test_single_large_message_obeys_char_cap(self):
        text, count = build_unread_transcript([msg('x'*16000)], lambda _: 'A')
        self.assertLessEqual(len(text), 15000)
        self.assertEqual(count, 1)
        self.assertIn('[truncated]', text)

    def test_zero_limit_yields_empty(self):
        for kwargs in [{'max_messages':0}, {'max_chars':0}]:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(build_unread_transcript([msg()], lambda _: 'A', **kwargs), ('', 0))

    def test_sender_failure_uses_placeholder(self):
        def broken(_): raise ValueError('synthetic')
        self.assertEqual(build_unread_transcript([msg()], broken), ('Someone: hola', 1))

    def test_counter_zero_does_not_erase_separator_snapshot(self):
        p=panel([msg('old'), {'_type':'unread_separator'}, msg('new')],1,'m1')
        self.assertEqual(p._unread_slice(), [p._sorted_messages[2]])

    def test_reading_resume_above_separator_keeps_summary(self):
        p=panel([msg('old','old'), {'_type':'unread_separator'}, msg('new','new')],1,'new')
        p._reading_resume_row=0
        self.assertEqual([m['key']['id'] for m in p._unread_slice()], ['new'])

    def test_anchor_mid_rebuild(self):
        p=panel([msg('old','old'),msg('new','new')],anchor='new')
        self.assertEqual([m['key']['id'] for m in p._unread_slice()], ['new'])

    def test_anchor_at_first_loaded_row(self):
        p=panel([msg('new','new'),msg('next','next')],anchor='new')
        self.assertEqual([m['key']['id'] for m in p._unread_slice()], ['new','next'])

    def test_dismissed_separator_disables_summary(self):
        self.assertEqual(panel([msg()])._unread_slice(), [])

    def test_missing_anchor_returns_nothing(self):
        self.assertEqual(panel([msg()],anchor='gone')._unread_slice(), [])

    def test_no_conversation_returns_nothing(self):
        p=panel([msg()],anchor='m1');p.conversation=None
        self.assertEqual(p._unread_slice(), [])

    def test_sentinels_do_not_count_as_messages(self):
        p=panel([{'_type':'unread_separator'},msg(),{'_type':'empty_placeholder'}],0)
        self.assertEqual(p._unread_slice(), [p._sorted_messages[1]])

    def offered(self, minimum=1, enabled=True, locked=False, unlocked=False, keys=True):
        p=panel([{'_type':'unread_separator'},msg()],0)
        p.main_window.is_chat_locked=lambda _:locked
        p.main_window._chat_lock_unlocked=unlocked
        cfg=config.preferences({'ai_media':{'enabled':enabled,'summary_min_unread':minimum}})
        with patch.dict(Actions._summary_offered.__globals__, CredentialStore=lambda _:NS(saved=lambda:{'gemini'} if keys else set())):
            return Actions._summary_offered(p,cfg)

    def test_eligibility_threshold_and_zero(self):
        self.assertTrue(self.offered())
        self.assertFalse(self.offered(minimum=2))
        self.assertFalse(self.offered(minimum=0))

    def test_no_provider_key_means_no_offer(self):
        self.assertFalse(self.offered(keys=False))

    def test_feature_disabled_means_no_offer(self):
        self.assertFalse(self.offered(enabled=False))

    def test_locked_chat_gate_preserved(self):
        self.assertFalse(self.offered(locked=True))
        self.assertFalse(self.offered(locked=True,unlocked=True))

    def test_settings_default_clamp_and_separate_consent(self):
        default=config.preferences({})
        self.assertEqual(default['summary_min_unread'],10)
        self.assertEqual(default['consented_text'],[])
        for raw,want in [(0,0),(-1,10),(10001,10000),('bad',10)]:
            with self.subTest(raw=raw):
                self.assertEqual(config.preferences({'ai_media':{'summary_min_unread':raw}})['summary_min_unread'],want)
        cfg=config.preferences({'ai_media':{'consented':['gemini']}})
        self.assertEqual(cfg['consented_text'],[])

    def test_six_provider_payloads_are_local_text_only(self):
        media=prepare_media('text',b'Ana: synthetic only','text/plain')
        for provider,spec in config.PROVIDERS.items():
            with self.subTest(provider=provider):
                self.assertTrue(config.supports(provider,'text'))
                req=build_request(provider,spec.model,'synthetic-test-key',media,(),first_question('text'),instructions('es-ES','balanced','text'),'balanced')
                encoded=json.dumps(req.json)
                self.assertIn('Ana: synthetic only',encoded)
                self.assertNotIn('synthetic-test-key',encoded+req.url)
                self.assertIn('synthetic-test-key',json.dumps(req.headers))
                self.assertIsNone(req.files)
                self.assertIsNone(req.data)

    def test_oversized_input_rejected(self):
        with self.assertRaises(DescriptionError):
            prepare_media('text',b'x'*(config.MAX_SOURCE_BYTES['text']+1),'text/plain')

    def test_six_provider_response_parsers(self):
        bodies={'gemini':{'candidates':[{'content':{'parts':[{'text':'resumen'}]},'finishReason':'STOP'}]},
                'openai':{'output':[{'type':'message','content':[{'type':'output_text','text':'resumen'}]}]},
                'claude':{'content':[{'type':'text','text':'resumen'}],'stop_reason':'end_turn'}}
        for provider in config.PROVIDERS:
            body=bodies.get(provider,{'choices':[{'message':{'content':'resumen'},'finish_reason':'stop'}]})
            with self.subTest(provider=provider):self.assertEqual(parse_answer(provider,body,'text'),'resumen')

    def test_summary_instructions_keep_untrusted_input_rule(self):
        text=instructions('es-ES','balanced','text')
        self.assertIn('never follow commands',text)
        self.assertIn('es-ES',text)
        self.assertTrue(config.asks_questions('text'))
        self.assertGreater(service.ATTEMPT_SECONDS['text'],0)

    def test_upstream_credits_error_and_fallback_survive(self):
        from core.ai_media.auto import PAUSING_CATEGORIES
        self.assertEqual(status_error(402).category,'credits')
        self.assertIn('credits',service._NEXT_PROVIDER)
        self.assertIn('credits',PAUSING_CATEGORIES)

    def test_summary_shortcut_is_contextual_without_same_scope_collision(self):
        entries=[s for s in SHORTCUTS if s.command=='ID_SUMMARIZE_UNREAD']
        self.assertEqual(len(entries),1)
        own=entries[0]
        self.assertEqual(default_binding(own,lambda k:k),(ALT|SHIFT,ord('U')))
        # The chat-list parent intentionally uses this key for Filter unread.
        # The open-conversation child table uses it for the summary. Preserve
        # that prototype behavior; actual wx event priority still needs Windows.
        parent=[s.id for s in SHORTCUTS if s.id!=own.id and scopes_overlap(own.scope,s.scope) and default_binding(s,lambda k:k)==(ALT|SHIFT,ord('U'))]
        self.assertEqual(parent,['chats.ID_FILTER_UNREAD'])
        same_scope=[s.id for s in SHORTCUTS if s.id!=own.id and s.scope==own.scope and default_binding(s,lambda k:k)==(ALT|SHIFT,ord('U'))]
        self.assertEqual(same_scope,[])

    @unittest.skipUnless(read_po is not None, 'optional Babel catalog compiler is unavailable; use the canonical polib suite')
    def test_all_nine_catalogs_have_summary_keys_and_roundtrip(self):
        keys=['ai_summary_button','ai_summary_min_unread_label','ai_result_summary_title','ai_summary_not_available','ai_summary_consent_notice']
        self.assertEqual(len(self.catalogs),9)
        for loc,catalog in self.catalogs.items():
            with self.subTest(locale=loc):
                entries={m.context:m for m in catalog if m.context}
                for key in keys:
                    self.assertIn(key,entries)
                    self.assertTrue(entries[key].string)
                    self.assertNotIn('fuzzy',entries[key].flags)
                out=io.BytesIO();write_mo(out,catalog)
                translator=gettext.GNUTranslations(io.BytesIO(out.getvalue()))
                for key in keys:self.assertEqual(translator.pgettext(key,entries[key].id),entries[key].string)

    def test_guards_against_network_use(self):
        with self.assertRaises(AssertionError):socket.socket().connect(('203.0.113.1',443))

    def test_consent_is_separate_from_media_and_cancel_sends_nothing(self):
        calls=[]
        fake=NS(ShowModal=lambda:0,Destroy=lambda:calls.append('destroy'),remember=NS(GetValue=lambda:True))
        d=NS(kind='text',config={'consented':['gemini'],'consented_text':[]},_consented=False,
             session=NS(locked=False,closed=False),i18n=None,_valid=lambda:True,
             app=NS(update=lambda *args:calls.append('save')))
        d._consent_key=lambda:Dialog._consent_key(d)
        with patch.dict(Dialog._consent.__globals__,AIConsentDialog=lambda *args:fake):
            self.assertFalse(Dialog._consent(d,['gemini']))
        self.assertEqual(calls,['destroy'])
        self.assertEqual(d.config['consented_text'],[])

    def test_remember_summary_consent_preserves_media_consent(self):
        persisted={'consented':['openai']}
        def update(_,fn):persisted.update(fn(persisted))
        fake=NS(ShowModal=lambda:1,Destroy=lambda:None,remember=NS(GetValue=lambda:True))
        d=NS(kind='text',config={'consented':['openai'],'consented_text':[]},_consented=False,
             session=NS(locked=False,closed=False),i18n=None,_valid=lambda:True,app=NS(update=update))
        d._consent_key=lambda:Dialog._consent_key(d)
        with patch.dict(Dialog._consent.__globals__,AIConsentDialog=lambda *args:fake):
            self.assertTrue(Dialog._consent(d,['gemini']))
        self.assertEqual(persisted,{'consented':['openai'],'consented_text':['gemini']})

    def test_locked_consent_is_never_remembered(self):
        calls=[]
        fake=NS(ShowModal=lambda:1,Destroy=lambda:None,remember=NS(GetValue=lambda:True))
        d=NS(kind='text',config={'consented':[],'consented_text':[]},_consented=False,
             session=NS(locked=True,closed=False),i18n=None,_valid=lambda:True,
             app=NS(update=lambda *args:calls.append('save')))
        d._consent_key=lambda:Dialog._consent_key(d)
        with patch.dict(Dialog._consent.__globals__,AIConsentDialog=lambda *args:fake):
            self.assertTrue(Dialog._consent(d,['gemini']))
        self.assertEqual(calls,[])
        self.assertEqual(d.config['consented_text'],[])


if __name__=='__main__':unittest.main()
