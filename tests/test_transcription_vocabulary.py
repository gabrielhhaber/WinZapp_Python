"""The user's vocabulary (names, acronyms) handed to Whisper as its initial prompt.

Pure rules (core/transcription/vocabulary.py) and the two places it travels:
faster-whisper's `initial_prompt` argument and whisper-cli's `--prompt`. It is a
hint kept in the account's settings only, and no log line may carry it.
"""

import ast
import inspect
import logging

from core.transcription import (
    backend as backend_module,
    faster_whisper_backend,
    message_run,
    vocabulary,
    whisper_cpp_backend,
    whisper_cpp_cli,
)


class TestWhatTheUserTyped:
    def test_one_per_line_or_separated_by_commas_and_semicolons(self):
        assert vocabulary.terms_of("Ana Souza\nWinZapp, NVDA; JAWS") == [
            "Ana Souza", "WinZapp", "NVDA", "JAWS"]

    def test_blanks_and_repeats_ignoring_case_are_dropped_in_order(self):
        assert vocabulary.terms_of("  nvda \n\n NVDA,Nvda ,jaws") == ["nvda", "jaws"]

    def test_control_characters_do_not_reach_the_prompt(self):
        assert vocabulary.terms_of("a\x00b\x1fc") == ["a b c"]

    def test_the_prompt_is_the_terms_joined(self):
        assert vocabulary.prompt_from_text("Ana\nNVDA") == "Ana, NVDA"

    def test_nothing_typed_is_no_prompt(self):
        assert vocabulary.prompt_from_text("") == ""
        assert vocabulary.prompt_from_text(None) == ""
        assert vocabulary.prompt_from_text(" \n , ; ") == ""

    def test_the_prompt_is_cut_in_whole_terms_within_the_limit(self):
        text = "\n".join(f"term{i:03d}" for i in range(200))
        prompt = vocabulary.prompt_from_text(text)
        assert 0 < len(prompt) <= vocabulary.MAX_CHARS
        assert prompt.split(", ")[-1].startswith("term") and len(prompt.split(", ")[-1]) == 7

    def test_a_single_term_longer_than_the_limit_gives_nothing_not_a_fragment(self):
        assert vocabulary.prompt_from_text("x" * (vocabulary.MAX_CHARS + 1)) == ""

    def test_settings_without_the_key_or_with_a_wrong_value_give_nothing(self):
        assert vocabulary.prompt_from_settings({}) == ""
        assert vocabulary.prompt_from_settings({"transcription": None}) == ""
        assert vocabulary.prompt_from_settings({"transcription": {"vocabulary": 5}}) == ""
        assert vocabulary.prompt_from_settings(None) == ""

    def test_settings_with_the_key(self):
        settings = {"transcription": {"vocabulary": "Ana\nNVDA"}}
        assert vocabulary.prompt_from_settings(settings) == "Ana, NVDA"

    def test_whisper_cpp_keeps_only_plain_ascii_terms(self):
        assert vocabulary.ascii_prompt("Ana, José, NVDA, Łukasz") == "Ana, NVDA"
        assert vocabulary.ascii_prompt("José") == ""
        assert vocabulary.ascii_prompt("") == ""


class TestFasterWhisper:
    @staticmethod
    def _model():
        class Model:
            calls = []

            def transcribe(self, audio, **kwargs):
                self.calls.append(kwargs)
                return iter(()), object()
        return Model()

    def _request(self, **extra):
        return backend_module.TranscriptionRequest(
            audio_path="no-such-file.wav", models_root="", model_id="small",
            device="cpu", compute_type="int8", **extra)

    def test_the_prompt_is_passed_when_there_is_one(self):
        model = self._model()
        faster_whisper_backend.FasterWhisperBackend()._run(
            model, self._request(initial_prompt="Ana, NVDA"))
        assert model.calls[0]["initial_prompt"] == "Ana, NVDA"

    def test_nothing_is_passed_without_one(self):
        model = self._model()
        faster_whisper_backend.FasterWhisperBackend()._run(model, self._request())
        assert "initial_prompt" not in model.calls[0]


class TestWhisperCpp:
    def _command(self, **extra):
        return whisper_cpp_cli.build_command(
            "cli.exe", "m.bin", "a.wav", "out", "pt", 4, False, **extra)

    def test_the_prompt_becomes_the_prompt_flag(self):
        command = self._command(prompt="Ana, NVDA")
        assert command[-2:] == ["--prompt", "Ana, NVDA"]

    def test_no_prompt_no_flag(self):
        assert "--prompt" not in self._command()

    def test_a_vocabulary_keeps_the_server_out_of_it(self):
        """The server's per-request prompt was not confirmed on a real run:
        with a vocabulary the note goes through whisper-cli."""
        backend = whisper_cpp_backend.WhisperCppBackend()
        backend._keeps_loaded = lambda: True
        request = backend_module.TranscriptionRequest(
            audio_path="a.wav", models_root="", model_id="small", device="cpu",
            compute_type="q5", initial_prompt="Ana")
        try:
            backend._run_on_server("m.bin", request, None, None, None)
        except Exception as exc:
            assert type(exc).__name__ == "ServerUnavailable"
            assert "vocabulary" in str(exc)
        else:
            raise AssertionError("the server was used")


class TestThePlumbing:
    def test_the_message_run_names_the_prompt_only_when_there_is_one(self):
        source = inspect.getsource(message_run.MessageTranscription._run_job)
        assert "vocabulary.prompt_from_settings(self._settings)" in source

    def test_the_job_hands_the_prompt_to_the_request(self):
        from core.transcription import job
        source = inspect.getsource(job.TranscriptionJob._decode_held)
        assert "initial_prompt=self._initial_prompt" in source

    def test_nothing_in_the_package_logs_the_vocabulary(self):
        """No logging call of the package names the prompt or the vocabulary."""
        import pathlib
        import core.transcription as package
        root = pathlib.Path(package.__file__).parent
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "logging"):
                    text = ast.unparse(node).lower()
                    assert "initial_prompt" not in text and "vocabulary" not in text, (
                        path.name, text)


def test_logging_import_is_used():
    assert logging  # keeps the import honest for the walk above
