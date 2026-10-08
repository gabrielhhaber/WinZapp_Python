"""Focus and reassignment regressions, without an App, window or desktop hook."""
from types import SimpleNamespace
import pytest
import wx
from core.keyboard_shortcuts import SECTION, binding_for, conflicts, default_binding, logical_key
from core.shortcut_catalog import SHORTCUTS
from ui import shortcut_bindings as keys
from ui.dialogs.shortcuts_tab import GLOBAL, ShortcutsTabMixin, visible_shortcuts
from ui.conversation_panel.composer import ComposerMixin
from ui.conversation_panel.chat_selection import ChatSelectionMixin
from ui.conversation_panel.archived_panel import ArchivedConversationsPanel
from ui.media_viewer import MediaViewerDialog
from ui.conversation_panel.message_list import MessageListMixin
from ui.navigation import NavigationPanel


def translate(key):
    return {'main_nav': '&Navigation', 'messages': '&Messages',
            'type_message': 'Ty&pe message',
            'incoming_call_answer_button': '&Answer',
            'incoming_call_answer_with_video_button': 'Answer with &video',
            'incoming_call_answer_without_video_button': 'Answer without &video',
            'incoming_call_reject_button': '&Reject',
            'incoming_call_silence_button': '&Silence',
            'incoming_call_close_button': '&Close'}.get(key, key)


def spec(shortcut_id):
    return next(s for s in SHORTCUTS if s.id == shortcut_id)


def window(overrides=None):
    return SimpleNamespace(settings={SECTION: dict(overrides or {})},
                           i18n=SimpleNamespace(t=translate, language='en-US'))


class Event:
    def __init__(self, key, mod=0, source=None):
        self.key, self.mod, self.source = key, mod, source
        self.skipped = False

    def GetKeyCode(self): return self.key
    def GetUnicodeKey(self): return self.key
    def GetEventObject(self): return self.source
    def ControlDown(self): return bool(self.mod & 2)
    def AltDown(self): return bool(self.mod & 1)
    def ShiftDown(self): return bool(self.mod & 4)
    def MetaDown(self): return bool(self.mod & 8)
    def Skip(self): self.skipped = True


@pytest.mark.parametrize('shortcut', SHORTCUTS, ids=lambda s: s.scope + ':' + s.id)
def test_each_command_moves_only_in_its_declared_scope(shortcut):
    original = default_binding(shortcut, translate)
    if original is None:
        return  # A mnemonic absent from this recording locale has no old key.
    replacement = (7, wx.WXK_F24)
    overrides = {shortcut.id: list(replacement)}
    assert logical_key(shortcut.scope, replacement, overrides, SHORTCUTS, translate) == original
    assert logical_key(shortcut.scope, original, overrides, SHORTCUTS, translate) == (0, -1)
    assert logical_key('unrelated_window', replacement, overrides, SHORTCUTS, translate) == replacement


@pytest.mark.parametrize('scope,method,old_id', [
    ('chats', ChatSelectionMixin._on_conv_list_key_down, 'chats.ID_CTRL_F'),
    ('archived', ArchivedConversationsPanel._on_arch_list_key_down, 'archived.ID_CTRL_F'),
])
def test_freed_parent_key_reaches_new_child_command(scope, method, old_id):
    assert any(s.id == old_id for s in SHORTCUTS)
    owner = SimpleNamespace(main_window=window({old_id: [2, ord('J')],
                                               'chat_selection.select': [2, ord('F')]}))
    received = []
    owner._handle_chat_selection_key = lambda event: received.append(keys.event_chord(event)) or True
    method(owner, Event(ord('F'), 2))
    assert received == [(2, wx.WXK_SPACE)]


def test_child_does_not_reinterpret_parents_logical_output():
    owner = window({'chats.ID_PIN_LIST': [2, ord('J')],
                    'chat_selection.select': [2, ord('P')]})
    physical = Event(ord('J'), 2)
    first = keys.command_key_event(owner, 'chats', physical)
    second = keys.command_key_event(owner, 'chat_selection', first)
    assert keys.event_chord(second) == (2, ord('P'))
    assert second._original_chord == (2, ord('J'))


def test_freed_key_can_reach_the_actual_ancestor_accelerator():
    mw = window({'message_list.play': [2, ord('J')], 'main.ID_ALT_1': [0, wx.WXK_SPACE]})
    mw._configured_shortcut_rows = [(spec('main.ID_ALT_1'), 101)]
    mw.GetParent = lambda: None
    field = SimpleNamespace(GetParent=lambda: mw)
    event = Event(wx.WXK_SPACE, source=field)
    proxy = keys.command_key_event(mw, 'message_list', event)
    assert proxy.GetKeyCode() == -1  # Never play using the former key.
    proxy.Skip()
    assert event.skipped  # Let the rebuilt ancestor table execute navigation.


def test_disabled_old_key_is_consumed_when_no_ancestor_uses_it():
    mw = window({'message_list.play': None})
    event = Event(wx.WXK_SPACE)
    keys.command_key_event(mw, 'message_list', event).Skip()
    assert not event.skipped


@pytest.mark.parametrize('focus_name', ['_reply_field', '_text_ctrl', '_position_slider', '_volume_slider'])
@pytest.mark.parametrize('changed', [False, True])
def test_playback_key_never_steals_editing_or_slider_keys(monkeypatch, focus_name, changed):
    owner = SimpleNamespace(main_window=window({'media.play': [2, ord('J')]} if changed else {}),
                            _current_kind='audio')
    for name in ('_reply_field', '_text_ctrl', '_position_slider', '_volume_slider'):
        setattr(owner, name, object())
    calls = []
    owner._on_play_pause = lambda _: calls.append('play')
    monkeypatch.setattr(wx.Window, 'FindFocus', lambda: getattr(owner, focus_name))
    event = Event(ord('J'), 2) if changed else Event(wx.WXK_SPACE)
    MediaViewerDialog._on_char_hook(owner, event)
    assert calls == [] and event.skipped


def test_changed_media_play_key_plays_only_with_transport_focus(monkeypatch):
    owner = SimpleNamespace(main_window=window({'media.play': [2, ord('J')]}), _current_kind='audio',
                            _reply_field=object(), _text_ctrl=object(), _position_slider=object(),
                            _volume_slider=object())
    calls = []
    owner._on_play_pause = lambda _: calls.append('play')
    monkeypatch.setattr(wx.Window, 'FindFocus', lambda: object())
    MediaViewerDialog._on_char_hook(owner, Event(ord('J'), 2))
    MediaViewerDialog._on_char_hook(owner, Event(wx.WXK_SPACE))
    assert calls == ['play']


@pytest.mark.parametrize('shortcut_id,default_key', [('autocomplete.insert', wx.WXK_RETURN),
                                                   ('autocomplete.close', wx.WXK_ESCAPE)])
@pytest.mark.parametrize('replacement', [None, [2, ord('J')]])
def test_autocomplete_early_hook_obeys_removed_and_changed_keys(monkeypatch, shortcut_id, default_key, replacement):
    calls = []
    owner = SimpleNamespace(main_window=window({shortcut_id: replacement}),
                            _mention_panel=SimpleNamespace(IsShown=lambda: True),
                            _mention_list=SimpleNamespace(GetSelection=lambda: 0),
                            _mention_suggestions=[('Name', 'private@c.us')],
                            message_field=SimpleNamespace(SetFocus=lambda: calls.append('focus')),
                            _is_phantom_nvda_char=lambda _: False,
                            _should_redirect_char_to_message=lambda _: False,
                            _hide_mention_suggestions=lambda: calls.append('close'),
                            _insert_mention=lambda *args: calls.append('insert'))
    monkeypatch.setattr(wx.Window, 'FindFocus', lambda: owner._mention_list)
    monkeypatch.setattr(wx, 'CallAfter', lambda f, *args: f(*args))
    ComposerMixin._on_conversation_char_hook(owner, Event(default_key))
    assert calls == []
    if replacement:
        ComposerMixin._on_conversation_char_hook(owner, Event(ord('J'), 2))
        assert calls[0] == ('insert' if shortcut_id.endswith('insert') else 'close')


def test_enter_sender_matches_new_key_and_blocks_old_native_send():
    owner = window({'status_reply.send': [2, ord('J')]})
    callbacks, sent = [], []
    field = SimpleNamespace(Bind=lambda event, handler: callbacks.append(handler))
    keys.bind_enter_command(owner, field, 'status_reply.send', lambda _: sent.append('reply'))
    old, new, ordinary = Event(wx.WXK_RETURN), Event(ord('J'), 2), Event(wx.WXK_SPACE)
    for event in (old, new, ordinary): callbacks[0](event)
    assert sent == ['reply'] and not old.skipped and ordinary.skipped


def test_reply_conflicts_with_media_commands_as_well_as_classic_status():
    reply = spec('status_reply.send')
    assert 'media.ID_CTRL_LEFT' in {s.id for s in conflicts(reply, (2, wx.WXK_LEFT), {}, SHORTCUTS, translate)}
    assert 'status.ID_CTRL_R' in {s.id for s in conflicts(reply, (2, ord('R')), {}, SHORTCUTS, translate)}


def test_global_bookmark_scope_is_checked_when_editing_message_row():
    shortcut = spec('messages.ID_BOOKMARK[1]')
    assert 'main.ID_ALT_4' in {s.id for s in conflicts(shortcut, (1, ord('4')), {}, SHORTCUTS, translate)}


def settings_stub(overrides=None, global_vk=0):
    class Stub(ShortcutsTabMixin):
        def __init__(self):
            self.main_window = window()
            self._shortcut_overrides = dict(overrides or {})
            self._hotkey_field = SimpleNamespace(_vk=global_vk, _mod=2,
                                                 SetValue=lambda _: None)
            self._lang_combo = SimpleNamespace(GetSelection=lambda: 0)
            self._lang_codes = ['en-US']
            self._shortcuts_page = object()
            self._notebook = SimpleNamespace(FindPage=lambda _: 1, SetSelection=lambda _: None)
        def _shortcut_edit_finished(self): pass
    return Stub()


def test_cancelled_staged_edit_does_not_mutate_saved_preferences(monkeypatch):
    dialog = settings_stub({'main.ID_ALT_1': [2, ord('J')]})
    assert dialog.main_window.settings[SECTION] == {}
    monkeypatch.setattr('ui.dialogs.shortcuts_tab.refresh_shortcuts', lambda _: None)
    dialog._apply_shortcut_values()
    assert dialog.main_window.settings[SECTION] == {'main.ID_ALT_1': [2, ord('J')]}
    dialog._shortcut_overrides.clear()
    assert dialog.main_window.settings[SECTION] == {'main.ID_ALT_1': [2, ord('J')]}


def test_disabled_shortcut_and_unset_global_are_not_a_conflict():
    dialog = settings_stub({'main.ID_ALT_1': None})
    assert dialog._accept_shortcut(spec('main.ID_ALT_1'), None)
    assert dialog._validate_shortcut_values()


def test_legacy_general_capture_is_rechecked_before_apply(monkeypatch):
    dialog = settings_stub(global_vk=ord('R'))
    warnings = []
    monkeypatch.setattr(wx, 'MessageBox', lambda *args: warnings.append(args))
    assert not dialog._validate_shortcut_values()
    assert warnings and dialog.main_window.settings[SECTION] == {}


def test_reset_all_includes_the_optional_global_hotkey():
    dialog = settings_stub({'main.ID_ALT_1': [2, ord('J')]}, global_vk=ord('W'))
    dialog._reset_all_shortcuts()
    assert dialog._shortcut_overrides == {}
    assert (dialog._hotkey_field._vk, dialog._hotkey_field._mod) == (0, 0)


def test_hidden_vault_commands_do_not_leak_through_inventory():
    mw = window()
    mw._chat_lock_vault = SimpleNamespace(configured=True, hide_navigation=True)
    rows = visible_shortcuts(mw)
    assert not any(s.scope == 'locked' or s.id in ('main.ID_ALT_7', 'main.lock_vault') for s in rows)
    assert GLOBAL in rows


def test_numpad_matches_the_main_digit_and_never_windows_modifier():
    owner = window({'device.pick[1]': [2, ord('J')]})
    assert keys.command_key_event(owner, 'device', Event(wx.WXK_NUMPAD1)).GetKeyCode() == -1
    assert not keys.matches(owner, Event(ord('J'), 2 | 8), 'device.pick[1]')


@pytest.mark.parametrize('selected,selection_enabled,expected', [
    (False, True, 'play'), (True, True, 'select'), (True, False, 'play')])
@pytest.mark.parametrize('changed', [False, True])
def test_message_space_preserves_selection_and_playback_priority(selected, selection_enabled, expected, changed):
    calls = []
    owner = SimpleNamespace(
        main_window=window({'message_list.play': [2, ord('J')]} if changed else {}),
        messages_list=SimpleNamespace(GetFocusedItem=lambda: 0, GetItemCount=lambda: 1),
        _is_loading_more=False, _messages_offset=0, _sorted_messages=[{'key': {'id': 'm'}}],
        selected_messages={'m'} if selected else set(),
        _selection_mode_enabled=lambda: selection_enabled,
        _toggle_message_selection=lambda _: calls.append('select') or True,
        _space_toggles_playback=lambda _: calls.append('play') or True)
    event = Event(ord('J'), 2) if changed else Event(wx.WXK_SPACE)
    MessageListMixin._on_messages_list_key_down(owner, event)
    assert calls == [expected] and not event.skipped
    if changed:
        MessageListMixin._on_messages_list_key_down(owner, Event(wx.WXK_SPACE))
        assert calls == [expected]


def composer_stub(overrides=None):
    calls = []
    owner = SimpleNamespace(main_window=window(overrides), _undo_emoticon_conversion=lambda: False,
                            _mention_panel=SimpleNamespace(IsShown=lambda: False),
                            message_field=SimpleNamespace(WriteText=lambda text: calls.append(('write', text))),
                            _convert_emoticon_before_caret=lambda _: None,
                            on_change_message_field=lambda _: None,
                            on_send_message=lambda _: calls.append(('send', None)))
    return owner, calls


@pytest.mark.parametrize('overrides', [{}, {'message_list.play': [2, ord('J')]}, {'media.play': [0, wx.WXK_SPACE]}])
def test_space_in_composer_is_always_native_text(overrides):
    owner, calls = composer_stub(overrides)
    event = Event(wx.WXK_SPACE)
    ComposerMixin._on_message_field_key_down(owner, event)
    assert event.skipped and calls == []


def test_new_send_key_sends_and_old_enter_inserts_newline():
    owner, calls = composer_stub({'composer.send': [2, ord('J')]})
    ComposerMixin._on_message_field_key_down(owner, Event(ord('J'), 2))
    ComposerMixin._on_message_field_key_down(owner, Event(wx.WXK_RETURN))
    ComposerMixin._on_message_field_key_down(owner, Event(wx.WXK_RETURN, 4))
    assert calls == [('send', None), ('write', '\n'), ('write', '\n')]


def test_navigation_space_changes_only_the_focused_navigation_row():
    calls = []
    owner = SimpleNamespace(main_window=window({'navigation.activate': [2, ord('J')]}),
                            nav_list=SimpleNamespace(GetFocusedItem=lambda: 2,
                                                     Select=lambda row: calls.append(('select', row))),
                            on_nav_item_selected=lambda event: calls.append(('open', event.GetIndex())))
    NavigationPanel._on_nav_key_down(owner, Event(ord('J'), 2))
    NavigationPanel._on_nav_key_down(owner, Event(wx.WXK_SPACE))
    assert calls == [('select', 2), ('open', 2)]


def test_freed_calls_filter_key_reaches_new_main_command():
    mw = window({'calls.next_filter': [2, ord('J')], 'main.ID_ALT_1': [2, wx.WXK_TAB]})
    mw._configured_shortcut_rows = [(spec('main.ID_ALT_1'), 101)]
    mw.GetParent = lambda: None
    field = SimpleNamespace(GetParent=lambda: mw)
    owner = SimpleNamespace(main_window=mw)
    event = Event(wx.WXK_TAB, 2, field)
    keys.handle_calls_shortcut(owner, event)
    assert event.skipped


@pytest.mark.parametrize('context', ['Reply to &Alice', 'Type to &Group: members only', 'Type to A && B'])
def test_refresh_preserves_context_and_restores_only_its_mnemonic(context):
    class Label:
        def __init__(self): self.text = context
        def GetLabel(self): return self.text
        def SetLabel(self, text): self.text = text
    mw = window({'messages.ID_ALT_FOCUS_FIELD': [2, ord('J')]})
    label = Label()
    mw.conversations_panel = SimpleNamespace(message_label=label)
    keys.refresh_mnemonics(mw)
    assert label.text == context.replace('&', '') if '&&' not in context else label.text == context
    mw.settings[SECTION].clear()
    keys.refresh_mnemonics(mw)
    assert label.text == context


def test_new_context_never_reactivates_the_old_native_focus_mnemonic():
    mw = window({'messages.ID_ALT_FOCUS_FIELD': None})
    texts = []
    label = SimpleNamespace(SetLabel=lambda text: texts.append(text))
    keys.set_shortcut_label(mw, label, 'messages.ID_ALT_FOCUS_FIELD', 'Reply to &Alice')
    keys.set_shortcut_label(mw, label, 'messages.ID_ALT_FOCUS_FIELD', 'Type to &Group')
    assert texts == ['Reply to Alice', 'Type to Group']


def test_settings_navigation_replaces_the_localized_comma_hint():
    mw = window({'main.ID_CTRL_COMMA': [2, ord('J')]})
    mw.i18n.t = lambda key: {'settings_shortcut': 'ctrl+virgül', 'shortcut_key_control': 'Kontrol'}[key]
    assert keys.navigation_shortcut_label(mw, 'settings', 'Ayarlar ctrl+virgül') == 'Ayarlar Kontrol+J'


def test_freed_composer_enter_reaches_new_main_command_instead_of_newline():
    owner, calls = composer_stub({'composer.send': [2, ord('J')], 'main.ID_ALT_1': [0, wx.WXK_RETURN]})
    mw = owner.main_window
    mw._configured_shortcut_rows = [(spec('main.ID_ALT_1'), 101)]
    mw.GetParent = lambda: None
    field = SimpleNamespace(GetParent=lambda: mw)
    event = Event(wx.WXK_RETURN, source=field)
    ComposerMixin._on_message_field_key_down(owner, event)
    assert event.skipped and calls == []


@pytest.mark.parametrize('assignment', [None, [2, ord('J')]])
def test_incoming_call_table_removes_or_replaces_original_answer_key(monkeypatch, assignment):
    from ui.dialogs import incoming_call as incoming
    tables, labels, accessible, answered = [], [], [], []
    button = SimpleNamespace(SetLabel=lambda text: labels.append(text),
                             SetAccessible=lambda value: accessible.append(value), IsEnabled=lambda: True)
    owner = SimpleNamespace(main_window=window({'incoming.incoming_call_answer_button': assignment}),
                            _i18n=SimpleNamespace(t=translate),
                            _label_specs=lambda: [(button, 'incoming_call_answer_button', lambda _: answered.append(True))],
                            SetAcceleratorTable=lambda table: tables.append(table))
    monkeypatch.setattr(incoming, 'AccessibleAltShortcutButton', lambda *args: args)
    monkeypatch.setattr(wx, 'NewIdRef', lambda: SimpleNamespace(GetId=lambda: 101))
    monkeypatch.setattr(wx, 'AcceleratorEntry', lambda mod, key, command: (mod, key, command))
    monkeypatch.setattr(wx, 'AcceleratorTable', lambda entries: tuple(entries))
    incoming.IncomingCallDialog._apply_labels(owner)
    assert labels == ['Answer']  # No native bare-letter mnemonic remains.
    if assignment is None:
        assert tables == [()] and accessible == [None]
    else:
        assert tables == [((2, ord('J'), 101),)]
        incoming.IncomingCallDialog._on_accelerator(owner, SimpleNamespace(GetId=lambda: 101))
        assert answered == [True]
