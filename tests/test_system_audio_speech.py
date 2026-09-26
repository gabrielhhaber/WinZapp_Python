"""Speech suppression integration, extracted without loading Windows UI."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "client/main.py"


def suppression(window):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MainWindow")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                  and n.name == "_voice_recording_silence_active")
    namespace = {}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace[method.name](window)


class SystemAudioSpeechTests(unittest.TestCase):
    def window(self, recording=True, mixed=True):
        return SimpleNamespace(
            settings={"speech_content": {"silence_while_recording": True}},
            conversations_panel=SimpleNamespace(_is_recording=recording,
                                               _recording_system_audio=mixed))

    def test_mixed_recording_does_not_suppress_speech_or_failure_announcement(self):
        self.assertFalse(suppression(self.window()))

    def test_ordinary_recording_keeps_existing_silence_preference(self):
        self.assertTrue(suppression(self.window(mixed=False)))

    def test_idle_speech_is_not_suppressed(self):
        self.assertFalse(suppression(self.window(recording=False, mixed=False)))


if __name__ == "__main__":
    unittest.main()
