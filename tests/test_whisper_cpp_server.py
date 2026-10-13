"""whisper.cpp with the model kept loaded (core/transcription/whisper_cpp_server.py).

Reported from users' logs: with "Keep the model loaded in memory" on and a
graphics card, every transcription still reloaded the model. Those users were
on whisper.cpp, which ran one whisper-cli process per note; the setting only
reached faster-whisper. The same release ships whisper-server.exe, which is
now asked instead when the setting is on.

Run for real on a machine's verified CPU build while this was written (the
backend as WinZapp calls it): same text and segment times as whisper-cli, the
language read from the server's stderr, a cancellation killing the server and
the next run restarting it, release() leaving no process and no temporary
folder. The CUDA build could not be run there (no card); its program is
pinned by the digest read from the pinned zip.

No process is started here: the pure pieces are called directly and the
process handling through fakes.
"""

import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.transcription import (
    errors, residency, whisper_cpp_backend, whisper_cpp_builds, whisper_cpp_runtime, whisper_cpp_server as server,
)
from core.transcription.backend import TranscriptionRequest
from core.transcription.whisper_cpp_backend import WhisperCppBackend

CONFIG = server.ServerConfig("C:/rt/Release/whisper-server.exe", "C:/models/ggml-base.bin", True, None, 4)


# ── pure ───────────────────────────────────────────────────────────────────

def test_the_server_listens_on_loopback_only_and_decodes_as_whisper_cli_does():
    command = server.build_command("whisper-server.exe", "m.bin", 50123, 4, use_gpu=True)
    assert command[:3] == ["whisper-server.exe", "-m", "m.bin"]
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--port") + 1] == "50123"
    assert command[command.index("-bs") + 1] == "5" and command[command.index("-bo") + 1] == "5"
    assert command[command.index("-l") + 1] == "auto"  # always given: its default is English
    assert "-ng" not in command and "--vad" not in command


def test_the_processor_build_and_the_voice_filter_are_asked_for():
    command = server.build_command("s.exe", "m.bin", 1, 2, use_gpu=False, vad_model_path="vad.bin")
    assert "-ng" in command
    assert command[-3:] == ["--vad", "-vm", "vad.bin"]


@pytest.mark.parametrize("language,sent", [("pt", "pt"), (None, "auto"), ("", "auto"), ("PT ", "pt"),
                                           ("../../x", "auto")])
def test_a_request_carries_the_audio_untouched_and_a_language_that_is_a_code_or_auto(language, sent):
    audio = bytes(range(256)) * 4
    body, content_type = server.encode_request(audio, language, "BOUNDARY")
    assert content_type == "multipart/form-data; boundary=BOUNDARY"
    assert audio in body and body.endswith(b"\r\n--BOUNDARY--\r\n")
    assert f'name="language"\r\n\r\n{sent}\r\n'.encode() in body
    assert b'name="response_format"\r\n\r\nverbose_json\r\n' in body


def test_an_answer_becomes_segments_in_seconds():
    raw = json.dumps({"text": "a b", "segments": [
        {"text": " Hello ", "start": 0.07, "end": 4.19}, "junk", {"text": None, "start": "x", "end": True}]})
    segments = server.parse_response(raw.encode())
    assert [(s.text, s.start, s.end) for s in segments] == [("Hello", 0.07, 4.19), ("", 0.0, 0.0)]


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'{"text": "x"}', b'{"segments": "x"}'])
def test_an_answer_of_another_shape_is_refused_without_quoting_it(raw):
    with pytest.raises(ValueError) as error:
        server.parse_response(raw)
    assert raw.decode() not in str(error.value)


# ── the program on disk ────────────────────────────────────────────────────

def manifest(digest, folder="Release/"):
    return {"executable": f"{folder}whisper-cli.exe",
            "files": [[f"{folder}whisper-cli.exe", 1, "c" * 64], [f"{folder}whisper-server.exe", 2, digest]]}


def test_the_server_is_launched_only_when_it_is_the_pinned_file(monkeypatch):
    build = whisper_cpp_builds.BUILD_CUDA
    monkeypatch.setattr(whisper_cpp_runtime, "_read_manifest", lambda directory: manifest(build.server_sha256))
    path = whisper_cpp_runtime.server_path(build, "C:/rt")
    assert path.replace("\\", "/").endswith("Release/whisper-server.exe")
    # Another file under that name, a folder without it, no manifest, no pin.
    monkeypatch.setattr(whisper_cpp_runtime, "_read_manifest", lambda directory: manifest("d" * 64))
    assert whisper_cpp_runtime.server_path(build, "C:/rt") is None
    monkeypatch.setattr(whisper_cpp_runtime, "_read_manifest",
                        lambda directory: {"executable": "Release/whisper-cli.exe", "files": []})
    assert whisper_cpp_runtime.server_path(build, "C:/rt") is None
    monkeypatch.setattr(whisper_cpp_runtime, "_read_manifest", lambda directory: None)
    assert whisper_cpp_runtime.server_path(build, "C:/rt") is None
    unpinned = whisper_cpp_builds.RuntimeBuild("x", "x.zip", 1, "0" * 64, False)
    assert whisper_cpp_runtime.server_path(unpinned, "C:/rt") is None


def test_both_builds_pin_their_server_apart_from_what_every_run_needs():
    for build in whisper_cpp_builds.BUILDS:
        assert len(build.server_sha256) == 64
        assert "Release/whisper-server.exe" not in dict(build.pinned_files)  # a folder without it still runs
    assert whisper_cpp_builds.BUILD_CPU.server_sha256 != whisper_cpp_builds.BUILD_CUDA.server_sha256


# ── the one server ─────────────────────────────────────────────────────────

class FakeServer:
    made = []

    def __init__(self, config):
        self.config = config
        self.is_alive = True
        self.stopped = False
        self.ready_error = None
        self.answer = (b'{"segments": []}', "")
        self.asked = []
        FakeServer.made.append(self)

    def alive(self):
        return self.is_alive

    def wait_until_ready(self, should_cancel=None):
        if self.ready_error:
            raise self.ready_error

    def transcribe(self, audio_path, language, progress=None, should_cancel=None):
        self.asked.append(audio_path)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def stop(self):
        self.stopped = True
        self.is_alive = False


@pytest.fixture
def one_server(monkeypatch):
    FakeServer.made = []
    monkeypatch.setattr(server, "_current", None)
    monkeypatch.setattr(sys, "platform", "win32")
    yield
    server._current = None


def ask(config=CONFIG, **kwargs):
    return server.transcribe(config, "a.wav", None, server_factory=FakeServer, **kwargs)


def test_the_model_is_loaded_once_for_any_number_of_recordings(one_server):
    ask()
    ask()
    ask()
    assert len(FakeServer.made) == 1 and len(FakeServer.made[0].asked) == 3
    assert server.running()


def test_another_model_device_or_filter_is_another_server(one_server):
    ask()
    ask(server.ServerConfig(CONFIG.executable, "C:/models/ggml-small.bin", True, None, 4))
    first, second = FakeServer.made
    assert first.stopped and not second.stopped and server._current is second


def test_a_server_that_died_is_replaced(one_server):
    ask()
    FakeServer.made[0].is_alive = False
    ask()
    assert len(FakeServer.made) == 2


def test_a_server_that_never_gets_ready_is_not_kept(one_server):
    def broken(config):
        made = FakeServer(config)
        made.ready_error = server.ServerUnavailable("did not answer in time")
        return made
    with pytest.raises(server.ServerUnavailable):
        server.transcribe(CONFIG, "a.wav", None, server_factory=broken)
    assert server._current is None and not server.running()


def test_a_second_transcription_meanwhile_does_not_wait(one_server):
    assert server._lock.acquire(blocking=False)
    try:
        with pytest.raises(server.ServerUnavailable):
            ask()
    finally:
        server._lock.release()
    assert FakeServer.made == []


def test_a_failed_request_leaves_no_dead_server_behind(one_server):
    ask()
    running = FakeServer.made[0]

    def die(*args, **kwargs):
        running.is_alive = False
        raise server.ServerUnavailable("status 500")
    running.transcribe = die
    with pytest.raises(server.ServerUnavailable):
        ask()
    assert running.stopped and server._current is None


def test_stopping_is_safe_with_nothing_running_and_stops_what_is(one_server):
    assert server.stop() is False
    ask()
    assert server.stop() is True
    assert FakeServer.made[0].stopped and not server.running()


def test_only_on_windows(one_server, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(server.ServerUnavailable):
        ask()


def test_a_server_that_cannot_be_tied_to_winzapps_life_is_killed_and_not_used(tmp_path):
    process = Mock()
    process.poll.return_value = None

    def refuse(_process):
        raise OSError(5, "AssignProcessToJobObject")
    config = server.ServerConfig(str(tmp_path / "whisper-server.exe"), str(tmp_path / "m.bin"), False, None, 2)
    with pytest.raises(server.ServerUnavailable):
        server._Server(config, popen=Mock(return_value=process), tie=refuse)
    process.kill.assert_called_once()


def test_the_program_runs_from_its_own_folder_hidden_and_with_no_console_for_its_words(tmp_path):
    process = Mock()
    process.poll.return_value = None
    popen = Mock(return_value=process)
    config = server.ServerConfig(str(tmp_path / "Release" / "whisper-server.exe"), str(tmp_path / "m.bin"),
                                 False, str(tmp_path / "vad.bin"), 2)
    running = server._Server(config, popen=popen, tie=lambda _process: 0)
    try:
        command = popen.call_args.args[0]
        options = popen.call_args.kwargs
        assert options["cwd"] == str(tmp_path / "Release")
        assert options["stdout"] == server.subprocess.DEVNULL and options["stdin"] == server.subprocess.DEVNULL
        assert command[command.index("-m") + 1] == "..\\m.bin" or command[command.index("-m") + 1] == "../m.bin"
        assert "-ng" in command and "--vad" in command
    finally:
        running.stop()
    process.kill.assert_called_once()


# ── the backend ────────────────────────────────────────────────────────────

REQUEST = TranscriptionRequest(audio_path="a.wav", models_root="C:/models", model_id="ggml-base",
                               device="cpu", compute_type="auto", language=None, vad_filter=False)


@pytest.fixture
def backend(monkeypatch):
    made = WhisperCppBackend(runtime_root="C:/rt", cpu_count=4)
    monkeypatch.setattr(whisper_cpp_backend.external_ggml, "model_file", lambda *args: "C:/models/ggml-base.bin")
    monkeypatch.setattr(made, "_executable_for", lambda device, capability: "C:/rt/Release/whisper-cli.exe")
    monkeypatch.setattr(whisper_cpp_runtime, "server_path", lambda build, root: "C:/rt/Release/whisper-server.exe")
    made.cli = Mock(return_value=((), "pt", 0.9))
    monkeypatch.setattr(made, "_run", made.cli)
    keep = {"on": True}
    monkeypatch.setattr(made, "_keeps_loaded", lambda: keep["on"])
    made.keep = keep
    return made


def test_with_the_setting_on_the_server_is_asked_and_whisper_cli_is_not_run(backend, monkeypatch):
    answer = json.dumps({"segments": [{"text": "olá", "start": 0.0, "end": 1.0}]}).encode()
    asked = Mock(return_value=(answer, "whisper_full_with_state: auto-detected language: pt (p = 0.97)\n"))
    monkeypatch.setattr(server, "transcribe", asked)
    result = backend.transcribe(REQUEST)
    assert result.text == "olá" and result.language == "pt" and result.language_probability == 0.97
    backend.cli.assert_not_called()
    config = asked.call_args.args[0]
    assert config == server.ServerConfig("C:/rt/Release/whisper-server.exe", "C:/models/ggml-base.bin",
                                         False, None, 3)


def test_a_language_that_was_asked_for_is_the_answers_language(backend, monkeypatch):
    monkeypatch.setattr(server, "transcribe", Mock(return_value=(b'{"segments": []}', "")))
    request = TranscriptionRequest(audio_path="a.wav", models_root="C:/models", model_id="ggml-base",
                                   device="cpu", compute_type="auto", language="pt", vad_filter=False)
    assert backend.transcribe(request).language == "pt"


def test_with_the_setting_off_nothing_changes(backend, monkeypatch):
    backend.keep["on"] = False
    asked = Mock()
    monkeypatch.setattr(server, "transcribe", asked)
    assert backend.transcribe(REQUEST).language == "pt"
    asked.assert_not_called()
    backend.cli.assert_called_once()


@pytest.mark.parametrize("failure", [
    server.ServerUnavailable("busy with another transcription"),
    server.ServerUnavailable("exited while loading"),
])
def test_any_trouble_with_the_server_runs_the_note_on_whisper_cli_instead(backend, monkeypatch, failure):
    monkeypatch.setattr(server, "transcribe", Mock(side_effect=failure))
    assert backend.transcribe(REQUEST).language == "pt"
    backend.cli.assert_called_once()


def test_an_unreadable_answer_also_goes_back_to_whisper_cli(backend, monkeypatch):
    monkeypatch.setattr(server, "transcribe", Mock(return_value=(b"<html>", "")))
    backend.transcribe(REQUEST)
    backend.cli.assert_called_once()


def test_a_folder_without_the_server_program_still_transcribes(backend, monkeypatch):
    monkeypatch.setattr(whisper_cpp_runtime, "server_path", lambda build, root: None)
    asked = Mock()
    monkeypatch.setattr(server, "transcribe", asked)
    backend.transcribe(REQUEST)
    asked.assert_not_called()
    backend.cli.assert_called_once()


def test_a_cancellation_is_a_cancellation_not_a_reason_to_start_over(backend, monkeypatch):
    cancelled = errors.TranscriptionError(errors.CANCELLED, "cancelled while whisper.cpp was running")
    monkeypatch.setattr(server, "transcribe", Mock(side_effect=cancelled))
    with pytest.raises(errors.TranscriptionError) as error:
        backend.transcribe(REQUEST)
    assert error.value.code == errors.CANCELLED
    backend.cli.assert_not_called()


def test_releasing_the_backend_stops_the_server(monkeypatch):
    stopped = Mock()
    monkeypatch.setattr(server, "stop", stopped)
    WhisperCppBackend().release()
    stopped.assert_called_once_with()


def test_the_setting_read_is_the_one_the_settings_window_writes():
    backend = WhisperCppBackend()
    manager = residency.residency
    before = manager.keep_loaded
    try:
        manager.configure(True)
        assert backend._keeps_loaded() is True
        manager.configure(False)
        assert backend._keeps_loaded() is False
    finally:
        manager.configure(before)
