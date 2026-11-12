"""Settings > Audio's noise reducer (ui/dialogs/noise_reducer_dialog.py) and
its own process for the graphics card builds (core/cuda_components:
noise_worker.py, noise_worker_host.py, runtime.py).

Reports of 2026-11-12: the noise reducer's settings button showed with the
noise reduction box unticked, and a change of processor needed a WinZapp
restart. A GPU build of onnxruntime cannot be swapped inside a process, so it
now runs in a process of its own, which restarts instead (announced), with
a voice message finished meanwhile waiting for it.

No real process is started here: the worker's side runs in a thread."""
import sys
import threading
from multiprocessing.connection import Client, Pipe
from types import SimpleNamespace

import numpy as np
import pytest

from core import neural_noise_reduction as nnr
from core.cuda_components import noise_worker, noise_worker_host, runtime
from core.cuda_components.noise_worker_host import NoiseWorkerHost, WorkerError, WorkerProcess
from ui.dialogs.noise_reducer_dialog import NoiseReducerButtonMixin


class _Ctrl:
    def __init__(self, value=False, shown=True):
        self.value, self.shown = value, shown

    def GetValue(self):
        return self.value

    def IsShown(self):
        return self.shown

    def Show(self, on=True):
        self.shown = on


def _panel(reduction_on, monkeypatch, installed=True):
    from core import noise_model
    monkeypatch.setattr(noise_model, "is_installed", lambda: installed)
    panel = SimpleNamespace(_noise_reduction_check=_Ctrl(reduction_on),
                            _noise_reducer_neural_check=_Ctrl(), _noise_reducer_btn=_Ctrl(),
                            _noise_reducer_page=SimpleNamespace(Layout=lambda: None))
    NoiseReducerButtonMixin._sync_noise_reducer_neural_box(panel)
    return panel


def test_the_settings_button_follows_the_noise_reduction_box(monkeypatch):
    off = _panel(False, monkeypatch)
    assert not off._noise_reducer_btn.shown and not off._noise_reducer_neural_check.shown
    on = _panel(True, monkeypatch)
    assert on._noise_reducer_btn.shown and on._noise_reducer_neural_check.shown
    no_model = _panel(True, monkeypatch, installed=False)
    assert no_model._noise_reducer_btn.shown and not no_model._noise_reducer_neural_check.shown


# ------------------------------------------------------------ runtime
def test_only_the_gpu_builds_run_in_a_process_of_their_own():
    assert runtime.uses_worker("cuda12", "win32") and runtime.uses_worker("cuda13", "win32")
    assert not runtime.uses_worker("cpu", "win32") and not runtime.uses_worker("coreml", "darwin")
    assert not runtime.uses_worker("cuda12", "linux")


def test_winzapps_own_process_keeps_the_processor_or_core_ml(monkeypatch):
    monkeypatch.setattr(runtime, "_active", None)
    assert runtime.set_in_process("cuda12") == "cpu"
    assert runtime.providers(["CUDAExecutionProvider", "CPUExecutionProvider"]) == [
        "CPUExecutionProvider"]
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    assert runtime.set_in_process("coreml") == "coreml"


def test_the_worker_is_winzapps_own_program_with_its_flag(tmp_path):
    assert noise_worker_host.worker_command("W.exe", frozen=True) == [
        "W.exe", noise_worker.WORKER_FLAG]
    assert noise_worker_host.worker_command("py.exe", frozen=False, client_dir=str(tmp_path)) == [
        "py.exe", str(tmp_path / "main.py"), noise_worker.WORKER_FLAG]


def test_main_hands_the_flag_over_before_importing_anything_else():
    from tests.god_modules import main_window_source
    source = main_window_source()
    handover = source.index(noise_worker.WORKER_FLAG)
    assert handover < source.index("import requests") and handover < source.index("import wx")


# ------------------------------------------------------------ the worker's side
class _Model:
    provider = "CUDAExecutionProvider"

    def process(self, pcm, rate):
        return pcm * 0.5


def test_the_worker_loads_cleans_and_never_raises():
    worker = noise_worker.Worker("cuda12", make_denoiser=lambda path: _Model())
    assert worker.handle(("denoise", b"\x00\x40", 48000, 1))[0] == "error"   # nothing loaded
    assert worker.handle(("load", "m.onnx")) == ("ok", "CUDAExecutionProvider")
    assert worker.handle(("denoise", b"\x00\x40", 48000, 1)) == ("ok", b"\x00\x20")
    assert worker.handle(("nonsense",))[0] == "error"
    assert worker.handle(("quit",)) == ("ok",)


def test_the_worker_ends_on_quit_or_when_winzapp_is_gone():
    mine, theirs = Pipe()
    done = []
    thread = threading.Thread(target=lambda: done.append(noise_worker.serve(
        theirs, noise_worker.Worker("cuda12", make_denoiser=lambda p: _Model()))))
    thread.start()
    mine.send(("quit",))
    assert mine.recv() == ("ok",)
    thread.join(5)
    assert done == [0]
    mine, theirs = Pipe()
    thread = threading.Thread(target=noise_worker.serve, args=(theirs, noise_worker.Worker("x")))
    thread.start()
    mine.close()
    thread.join(5)
    assert not thread.is_alive()


def test_the_address_comes_from_the_environment_never_argv():
    address, key, device = noise_worker.parse_address("4321:" + "ab" * 32 + ":cuda13")
    assert address == ("127.0.0.1", 4321) and key == b"\xab" * 32 and device == "cuda13"


# ------------------------------------------------------------ WinZapp's side
class _ThreadProcess:
    """popen(): the worker's side runs in a thread, as noise_worker.main would."""

    def __init__(self, command, env, **_kw):
        self.command, self.env = command, env
        address, key, device = noise_worker.parse_address(env[noise_worker.ENV_ADDRESS])
        self.thread = threading.Thread(target=self._run, args=(address, key, device), daemon=True)
        self.thread.start()

    def _run(self, address, key, device):
        conn = Client(address, authkey=key)
        noise_worker.serve(conn, noise_worker.Worker(device, make_denoiser=lambda p: _Model()))
        conn.close()

    def poll(self):
        return None if self.thread.is_alive() else 0

    def wait(self, timeout=None):
        self.thread.join(timeout)

    def kill(self):
        pass


def test_the_process_is_started_loaded_used_and_ended():
    started = []
    worker = WorkerProcess("cuda12", popen=lambda c, **kw: started.append(_ThreadProcess(c, **kw))
                           or started[-1])
    worker.start("m.onnx")
    assert worker.provider == "CUDAExecutionProvider" and worker.alive()
    assert started[0].command[-1] == noise_worker.WORKER_FLAG
    assert worker.denoise(b"\x00\x40\x00\xc0", 48000, 2) == b"\x00\x20\x00\xe0"
    worker.stop()
    assert not worker.alive() and not started[0].thread.is_alive()


def test_a_process_that_dies_before_connecting_is_not_waited_for(monkeypatch):
    monkeypatch.setattr(noise_worker_host, "CONNECT_SECONDS", 30)
    dead = SimpleNamespace(poll=lambda: 1, wait=lambda t=None: None, kill=lambda: None)
    worker = WorkerProcess("cuda12", popen=lambda c, **kw: dead)
    import time
    t0 = time.monotonic()
    with pytest.raises(WorkerError):
        worker.start("m.onnx")
    assert time.monotonic() - t0 < 5


class _Fake:
    def __init__(self, device, provider="CUDAExecutionProvider", fail=False, gate=None):
        self.device, self.provider, self.fail, self.gate = device, provider, fail, gate
        self.stopped = False

    def start(self, path):
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail:
            raise WorkerError("no card")

    def denoise(self, audio, rate, channels):
        return self.device.encode() + b":" + audio

    def alive(self):
        return not self.stopped

    def stop(self):
        self.stopped = True


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime, "_active", None)
    monkeypatch.setattr(nnr, "forget_denoiser", lambda: None)
    made, said, options = [], [], {}

    def make(device):
        made.append(_Fake(device, **options.get(device, {})))
        return made[-1]
    h = NoiseWorkerHost(make_process=make, model_path=lambda: "m.onnx")
    h.say = said.append
    return SimpleNamespace(h=h, made=made, said=said, options=options)


def test_the_process_starts_on_the_first_recording_and_is_kept(host):
    host.h.configure("cuda12")
    assert host.made == []                                   # nothing at startup
    assert host.h.denoise(b"a", 48000, 1) == b"cuda12:a"
    assert host.h.denoise(b"b", 48000, 1) == b"cuda12:b"
    assert len(host.made) == 1


def test_another_card_restarts_the_process_and_says_so(host):
    host.h.configure("cuda12")
    host.h.denoise(b"a", 48000, 1)
    host.h.apply("cuda13", background=False)
    assert host.made[0].stopped and host.made[1].device == "cuda13"
    assert host.said == ["noise_reducer_restarting", "noise_reducer_ready"]
    assert host.h.denoise(b"a", 48000, 1) == b"cuda13:a"


def test_a_card_that_cannot_start_says_the_processor_is_used_and_is_not_retried(host):
    host.options["cuda13"] = {"fail": True}
    host.h.configure("cuda12")
    host.h.apply("cuda13", background=False)
    assert host.said == ["noise_reducer_restarting", "noise_reducer_restart_failed"]
    with pytest.raises(WorkerError):
        host.h.denoise(b"a", 48000, 1)
    assert len(host.made) == 1                               # not tried on every recording
    host.options["cuda13"] = {}
    host.h.apply("cpu", background=False)
    host.h.apply("cuda13", background=False)                 # chosen anew: tried again
    assert host.h.denoise(b"a", 48000, 1) == b"cuda13:a"


def test_a_session_that_fell_back_to_the_processor_is_said(host):
    host.options["cuda12"] = {"provider": "CPUExecutionProvider"}
    host.h.apply("cuda12", background=False)
    assert host.said[-1] == "noise_reducer_restart_failed"


def test_the_processor_ends_the_process_without_a_word(host):
    host.h.configure("cuda12")
    host.h.denoise(b"a", 48000, 1)
    host.h.apply("cpu", background=False)
    assert host.made[0].stopped and host.said == []
    with pytest.raises(WorkerError):
        host.h.denoise(b"a", 48000, 1)                       # the caller uses the processor


def test_nothing_starts_when_the_reducer_is_off(host):
    host.h.apply("cuda12", start=False, background=False)
    assert host.made == [] and host.said == []
    assert host.h.device == "cuda12"


def test_a_voice_message_finished_during_the_restart_waits_for_it(host):
    gate = threading.Event()
    host.options["cuda13"] = {"gate": gate}
    host.h.configure("cuda12")
    restart = host.h.apply("cuda13")
    for _ in range(200):
        if host.h.restarting:
            break
        threading.Event().wait(0.01)
    assert host.h.restarting
    got = []
    message = threading.Thread(target=lambda: got.append(host.h.denoise(b"v", 48000, 1)))
    message.start()
    for _ in range(200):
        if "noise_reducer_waiting_restart" in host.said:
            break
        threading.Event().wait(0.01)
    assert got == [] and "noise_reducer_waiting_restart" in host.said
    gate.set()
    restart.join(5)
    message.join(5)
    assert got == [b"cuda13:v"]                              # cleaned by the new process


# ------------------------------------------------------------ reduce_voice_noise
def test_a_recording_goes_to_the_card_and_falls_back_to_the_processor(monkeypatch, tmp_path):
    model = tmp_path / "m.onnx"
    model.write_bytes(b"x")
    monkeypatch.setattr(nnr, "model_path", lambda: str(model))
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    fake = NoiseWorkerHost(make_process=lambda d: _Fake(d), model_path=lambda: str(model))
    fake.configure("cuda12")
    monkeypatch.setattr(noise_worker_host, "host", fake)
    assert nnr.reduce_voice_noise(b"\x00\x40", 48000, 1) == (b"cuda12:\x00\x40", "neural")

    def broken(*a):
        raise WorkerError("went away")
    monkeypatch.setattr(fake, "denoise", broken)
    monkeypatch.setattr(nnr, "_denoiser", SimpleNamespace(
        process=lambda pcm, rate: np.asarray(pcm, dtype=np.float32) * 0.5))
    assert nnr.reduce_voice_noise(b"\x00\x40", 48000, 1) == (b"\x00\x20", "neural")
