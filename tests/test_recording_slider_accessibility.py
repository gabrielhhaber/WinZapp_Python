"""Recording sliders expose names through MSAA, not just wx.SetName.

No wx application/windows: execute production provider and composer methods.
The fallback models the reported native label '100', not a live NVDA capture.
"""
import ast
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tests.test_system_audio_ui import ROOT, bind


class AccessibleBase:
    def GetName(self, childId):
        return (1, '')


def provider_class(wx):
    path = ROOT / 'client/ui/accessible.py'
    classes: list[ast.stmt] = [n for n in ast.parse(path.read_text(encoding='utf-8')).body
               if isinstance(n, ast.ClassDef) and n.name == 'AccessibleRecordingVolumeSlider']
    if not classes:
        return None
    ns = {'wx': wx}
    exec(compile(ast.Module(body=classes, type_ignores=[]), str(path), 'exec'), ns)
    return ns['AccessibleRecordingVolumeSlider']


class RecordingSliderNameTests(unittest.TestCase):
    def make_panel(self):
        wx = SimpleNamespace(Accessible=AccessibleBase, ACC_OK=0, ACC_NOT_IMPLEMENTED=1,
            StaticText=Mock(side_effect=lambda *a, **kw: Mock()),
            SL_HORIZONTAL=1, SL_LABELS=2, EXPAND=4, LEFT=8, RIGHT=16, BOTTOM=32,
            EVT_SLIDER='slider', EVT_KEY_DOWN='key')
        sliders = []
        def make_slider(*args, **kwargs):
            slider = Mock()
            slider._name = ''
            slider._provider = None
            slider.SetName.side_effect = lambda name: setattr(slider, '_name', name)
            slider.GetName.side_effect = lambda: slider._name
            slider.SetAccessible.side_effect = lambda a: setattr(slider, '_provider', a)
            sliders.append(slider)
            return slider
        wx.Slider = make_slider
        pl = json.loads((ROOT / 'client/languages/pl.json').read_text(encoding='utf-8'))
        p = SimpleNamespace(_voice_panel=object(), _send_voice_btn=object(),
            main_window=SimpleNamespace(settings={}, i18n=SimpleNamespace(t=pl.__getitem__)),
            _on_system_audio_volume=Mock(), _on_system_audio_volume_key=Mock(),
            _on_nvda_volume=Mock(), _on_nvda_volume_key=Mock(),
            _recording_system_audio=True, _recording_starting=False,
            _system_audio_session={'recorder':SimpleNamespace(nvda_volume_available=True)})
        cls = provider_class(wx)
        bind(p, ['_create_system_audio_volume_controls', '_create_nvda_volume_controls',
                 '_relabel_system_audio_volume_controls'],
             {'wx':wx, 'AccessibleRecordingVolumeSlider':cls})
        p._create_system_audio_volume_controls(Mock())
        p._create_nvda_volume_controls(Mock())
        return p, sliders, cls

    def exposed_name(self, slider):
        provider = slider._provider
        if provider is not None:
            status, name = provider.GetName(0)
            if status == 0:
                return name
        return '100'  # observed user symptom; native trackbar label fallback

    def test_both_real_composer_methods_attach_their_own_name_provider(self):
        p, sliders, cls = self.make_panel()
        self.assertEqual(self.exposed_name(sliders[0]),
                         'Głośność dźwięku komputera (%)')
        self.assertEqual(self.exposed_name(sliders[1]), 'Głośność NVDA (%)')
        self.assertIs(p._system_audio_volume_accessible, sliders[0]._provider)
        self.assertIs(p._nvda_volume_accessible, sliders[1]._provider)
        self.assertIsNot(sliders[0]._provider, sliders[1]._provider)

    def test_provider_reads_current_split_label_and_current_language(self):
        p, sliders, cls = self.make_panel()
        p._relabel_system_audio_volume_controls()
        self.assertEqual(self.exposed_name(sliders[0]),
                         'Głośność innych dźwięków (%)')
        en = json.loads((ROOT / 'client/languages/en-US.json').read_text(encoding='utf-8'))
        p.main_window.i18n.t = en.__getitem__
        p._relabel_system_audio_volume_controls()
        self.assertEqual(self.exposed_name(sliders[1]), en['system_audio_recording_nvda_volume'])
        p._system_audio_session['recorder'].nvda_volume_available = False
        p._relabel_system_audio_volume_controls()
        self.assertEqual(self.exposed_name(sliders[0]), en['system_audio_recording_volume'])

    def test_value_role_state_and_child_objects_stay_native(self):
        p, sliders, cls = self.make_panel()
        self.assertIsNotNone(cls, 'MSAA name provider missing')
        for member in ('GetValue', 'GetRole', 'GetState', 'GetChild', 'GetChildCount', 'SetValue'):
            self.assertNotIn(member, cls.__dict__)
        for slider in sliders:
            self.assertEqual(slider._provider.GetName(1), (1, ''))


if __name__ == '__main__':
    unittest.main()
