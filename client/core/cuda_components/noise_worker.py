"""The noise reducer's own process, for the GPU builds of onnxruntime.

A GPU build of onnxruntime cannot be swapped inside a process (runtime.py),
so it runs here: WinZapp starts its own executable with WORKER_FLAG
(main.py hands that over before importing anything else), and this process
connects back over a local socket authenticated with a one-time key
(multiprocessing.connection), puts the build in place before anything
imports onnxruntime, and answers requests until WinZapp says "quit" or goes
away (the closed socket ends the loop). Changing the device ends this
process and starts another: the restart the settings announce.

Requests and answers are tuples; every answer starts with "ok" or "error":
  ("load", model_path)                 -> ("ok", provider in use)
  ("denoise", pcm16, rate, channels)   -> ("ok", pcm16 cleaned)
  ("quit",)                            -> ("ok",)
"""

import logging
import os
import sys

#: The argument that makes WinZapp's executable this process.
WORKER_FLAG = "--noise-reducer-worker"
#: "port:authkey-hex:device" handed over in the environment, never argv.
ENV_ADDRESS = "WINZAPP_NOISE_WORKER"
#: The file this process logs to (WinZapp's own log is not shared).
ENV_LOG = "WINZAPP_NOISE_WORKER_LOG"


def parse_address(value):
    """("127.0.0.1", port), authkey and device from ENV_ADDRESS's value."""
    port, key, device = (value or "").split(":", 2)
    return ("127.0.0.1", int(port)), bytes.fromhex(key), device


class Worker:
    """The requests of one connection; `make_denoiser(path)` loads the model
    (NeuralDenoiser by default) and is replaced in tests."""

    def __init__(self, device, make_denoiser=None):
        self.device = device
        self.denoiser = None
        self._make = make_denoiser

    def _load(self, path):
        if self._make is not None:
            return self._make(path)
        from core.neural_noise_reduction import NeuralDenoiser
        return NeuralDenoiser(path)

    def handle(self, request):
        """One request -> its answer; never raises."""
        try:
            kind = request[0]
            if kind == "load":
                self.denoiser = self._load(request[1])
                return ("ok", getattr(self.denoiser, "provider", ""))
            if kind == "denoise":
                if self.denoiser is None:
                    return ("error", "model not loaded")
                from core.neural_noise_reduction import denoise_pcm16
                return ("ok", denoise_pcm16(request[1], request[2], request[3], self.denoiser))
            if kind == "quit":
                return ("ok",)
            return ("error", f"unknown request {kind!r}")
        except Exception as exc:
            logging.warning("[noise_worker] %s failed", request[0] if request else "?",
                            exc_info=True)
            return ("error", f"{type(exc).__name__}: {exc}")


def serve(conn, worker) -> int:
    """Answer requests on *conn* until "quit" or the other end is gone."""
    while True:
        try:
            request = conn.recv()
        except (EOFError, OSError):
            return 0
        answer = worker.handle(request)
        try:
            conn.send(answer)
        except (EOFError, OSError):
            return 0
        if request and request[0] == "quit":
            return 0


def main(environ=None) -> int:
    environ = os.environ if environ is None else environ
    log_file = environ.get(ENV_LOG)
    if log_file:
        logging.basicConfig(filename=log_file, filemode="w", level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
    try:
        address, key, device = parse_address(environ.get(ENV_ADDRESS))
    except (ValueError, TypeError):
        logging.error("[noise_worker] no address to connect to")
        return 2
    from core.cuda_components import runtime
    in_use = runtime.activate(device)        # before anything imports onnxruntime
    logging.info("[noise_worker] started for %s (build in place: %s)", device, in_use)
    from multiprocessing.connection import Client
    try:
        conn = Client(address, authkey=key)
    except Exception as exc:
        logging.error("[noise_worker] could not connect: %s", type(exc).__name__)
        return 3
    try:
        return serve(conn, Worker(device))
    finally:
        conn.close()
        logging.info("[noise_worker] ended")


if __name__ == "__main__":   # pragma: no cover - started through main.py
    sys.exit(main())
