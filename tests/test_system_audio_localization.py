"""Headless contracts for system-audio recording strings and defaults."""
import ast
import json
from pathlib import Path
import unittest
from string import Formatter

ROOT = Path(__file__).resolve().parents[1]
KEYS = {
    "system_audio_recording_title", "system_audio_recording_warning",
    "record_voice_message_system_audio", "ui_warn_system_audio_recording",
    "system_audio_recording_failed", "system_audio_recording_interrupted",
    "shortcut_ctrl_shift_h_label", "system_audio_recording_volume",
    "system_audio_recording_nvda_volume", "system_audio_recording_other_volume",
}


class SystemAudioLocalizationTests(unittest.TestCase):
    def test_registered_locales_have_nonempty_feature_strings(self):
        directory = ROOT / "client/languages"
        locales = json.loads((directory / "language_map.json").read_text(encoding="utf-8"))
        for locale in locales:
            with self.subTest(locale=locale):
                strings = json.loads((directory / (locale + ".json")).read_text(encoding="utf-8"))
                self.assertFalse(KEYS - strings.keys())
                for key in KEYS:
                    self.assertTrue(strings[key].strip(), key)
                self.assertIn("Ctrl+Shift+H", strings["shortcut_ctrl_shift_h_label"])
                english = json.loads((directory / 'en-US.json').read_text(encoding='utf-8'))
                for key in KEYS:
                    fields = lambda value: [field for _, field, _, _ in Formatter().parse(value)
                                            if field is not None]
                    self.assertEqual(fields(strings[key]), fields(english[key]), (locale, key))

    def test_registered_locales_have_concise_volume_labels(self):
        labels = {
            'pt-BR': ('Volume do áudio do computador (%)', 'Volume dos demais sons (%)', 'Volume do NVDA (%)'),
            'pt-PT': ('Volume do áudio do computador (%)', 'Volume dos restantes sons (%)', 'Volume do NVDA (%)'),
            'en-US': ('Computer audio volume (%)', 'Other sounds volume (%)', 'NVDA volume (%)'),
            'es-ES': ('Volumen del audio del ordenador (%)', 'Volumen de los demás sonidos (%)', 'Volumen de NVDA (%)'),
            'pl': ('Głośność dźwięku komputera (%)', 'Głośność innych dźwięków (%)', 'Głośność NVDA (%)'),
            'tr-TR': ('Bilgisayar sesi düzeyi (%)', 'Diğer seslerin düzeyi (%)', 'NVDA sesi düzeyi (%)'),
            'ro': ('Volumul sunetului computerului (%)', 'Volumul celorlalte sunete (%)', 'Volumul NVDA (%)'),
        }
        directory = ROOT / 'client/languages'
        locales = json.loads((directory / 'language_map.json').read_text(encoding='utf-8'))
        self.assertEqual(set(labels), set(locales))
        for locale in locales:
            strings = json.loads((directory / (locale + '.json')).read_text(encoding='utf-8'))
            for key, expected in zip(('system_audio_recording_volume',
                                      'system_audio_recording_other_volume',
                                      'system_audio_recording_nvda_volume'), labels[locale]):
                with self.subTest(locale=locale, key=key):
                    self.assertEqual(strings[key], expected)

    def test_registered_warnings_explain_recording_sliders_and_privacy_without_device_details(self):
        # Explain this recording mode, two sliders with NVDA, the fallback
        # without NVDA, privacy and unchanged listening volume.
        phrases = {
            'pt-BR': ('microfone', 'neste modo', 'se você usa o nvda', 'dois controles deslizantes',
                      'sem o nvda', 'demais sons', 'não alteram o volume que você ouve', 'informações privadas'),
            'pt-PT': ('microfone', 'neste modo', 'se utiliza o nvda', 'dois controlos deslizantes',
                      'sem o nvda', 'restantes sons', 'não alteram o volume que ouve', 'informações privadas'),
            'en-US': ('microphone', 'in this mode', 'if you use nvda', 'two sliders', 'without nvda',
                      'other computer sounds', 'do not change your listening volume', 'private information'),
            'es-ES': ('micrófono', 'en este modo', 'si usas nvda', 'dos deslizadores', 'sin nvda',
                      'demás sonidos', 'no cambian el volumen de escucha', 'información privada'),
            'pl': ('mikrofonu', 'w tym trybie rejestrujesz', 'jeśli korzystasz z nvda', 'dwa suwaki',
                   'bez nvda', 'innych dźwięków', 'nie zmieniają głośności odsłuchu', 'prywatne informacje'),
            'tr-TR': ('mikrofon', 'bu modda', 'nvda kullanıyorsanız', 'iki kaydırıcı', 'nvda olmadan',
                      'diğer bilgisayar seslerinin', 'duyduğunuz ses düzeyini değiştirmez', 'özel bilgiler'),
            'ro': ('microfon', 'în acest mod', 'dacă utilizați nvda', 'două glisoare', 'fără nvda',
                   'celorlalte sunete', 'nu modifică volumul de ascultare', 'informații private'),
        }
        device_terms = {
            'pt-BR': ('dispositivos de saída', 'padrão'),
            'pt-PT': ('dispositivos de saída', 'predefinido'),
            'en-US': ('output devices', 'default'),
            'es-ES': ('dispositivos de salida', 'predeterminado'),
            'pl': ('urządzeń wyjściowych', 'domyśln'),
            'tr-TR': ('çıkış aygıt', 'varsayılan'),
            'ro': ('dispozitivele de ieșire', 'implicit'),
        }
        directory = ROOT / 'client/languages'
        locales = json.loads((directory / 'language_map.json').read_text(encoding='utf-8'))
        self.assertEqual(set(phrases), set(locales))
        self.assertEqual(set(device_terms), set(locales))
        for locale in locales:
            with self.subTest(locale=locale):
                strings = json.loads((directory / (locale + '.json')).read_text(encoding='utf-8'))
                warning = strings['system_audio_recording_warning']
                for phrase in phrases[locale]:
                    self.assertIn(phrase, warning.lower())
                for term in device_terms[locale]:
                    self.assertNotIn(term, warning.lower())
                self.assertIn('NVDA', warning)
                self.assertEqual(warning.count('\n\n'), 1)
                self.assertTrue(warning.endswith('?'))
                self.assertEqual([field for _, field, _, _ in Formatter().parse(warning)
                                  if field is not None], [])

    def test_warning_enabled_in_both_default_sources(self):
        defaults = json.loads((ROOT / "client/data/settings_default.json").read_text(encoding="utf-8"))
        self.assertIs(defaults["user_interface"].get("warn_system_audio_recording"), True)
        self.assertEqual(defaults['user_interface'].get('system_audio_consent_revision'), 0)
        tree = ast.parse((ROOT / "client/core/utils.py").read_text(encoding="utf-8"))
        assignment = next(n for n in tree.body if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "DEFAULT_SETTINGS" for t in n.targets))
        assert isinstance(assignment.value, ast.Dict)
        ui = next(value for key, value in zip(assignment.value.keys, assignment.value.values)
                  if isinstance(key, ast.Constant) and key.value == "user_interface")
        assert isinstance(ui, ast.Dict)
        values = {key.value: value.value for key, value in zip(ui.keys, ui.values)
                  if isinstance(key, ast.Constant) and isinstance(value, ast.Constant)}
        self.assertIs(values.get("warn_system_audio_recording"), True)
        self.assertEqual(values.get('system_audio_consent_revision'), 0)

    def test_polish_warning_uses_requested_concise_copy(self):
        strings = json.loads((ROOT / "client/languages/pl.json").read_text(encoding="utf-8"))
        self.assertEqual(
            strings['system_audio_recording_warning'],
            'W tym trybie rejestrujesz dźwięk mikrofonu i komputera. Jeśli korzystasz z NVDA, '
            'dostępne będą dwa suwaki: głośności NVDA oraz innych dźwięków komputera. '
            'Bez NVDA regulujesz tylko głośność dźwięku komputera. '
            'Suwaki nie zmieniają głośności odsłuchu.\n\n'
            'Pamiętaj, że nagranie może zawierać Twoje prywatne informacje. Czy rozpocząć?',
        )


if __name__ == "__main__":
    unittest.main()
