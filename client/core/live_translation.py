"""Gemini Live Translate audio transport. One resumable session per direction."""
from __future__ import annotations

import asyncio
import base64
from collections import deque
import json
import logging
import ssl
import threading
import time
from urllib.parse import urlencode

import numpy as np
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from core.call_audio_resampling import StreamingMonoResampler
from core.live_translation_playout import TranslationPlayout
from core.tls_trust import system_ssl_context

MODEL = "gemini-3.5-live-translate-preview"
ENDPOINT = ("wss://generativelanguage.googleapis.com/ws/"
            "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")
INPUT_RATE = 16000
OUTPUT_RATE = 24000
FRAME_SECONDS = 0.02
INPUT_BYTES = INPUT_RATE * 2 // 10
DUCKED_GAIN = 0.35
MICROPHONE_PAUSE_SECONDS = 0.30
MICROPHONE_SPEECH_RMS = 150
MAX_CATCH_UP_FRAMES = 6


class _GoogleConnect(connect):
    def process_redirect(self, exc):
        # Never forward a connection carrying a Gemini credential to a different URL.
        return exc


class _SetupRefused(Exception):
    pass


_NETWORK_ERRORS = (WebSocketException, OSError, ValueError, _SetupRefused)


def setup_message(language, handle, *, echo_target_language=True):
    return {"setup": {
        "model": f"models/{MODEL}",
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "translationConfig": {
                "targetLanguageCode": language, "echoTargetLanguage": echo_target_language,
            },
        },
        "sessionResumption": {"handle": handle} if handle else {},
        "contextWindowCompression": {"slidingWindow": {}},
    }}


def input_message(pcm):
    return {"realtimeInput": {"audio": {
        "mimeType": "audio/pcm;rate=16000", "data": base64.b64encode(pcm).decode("ascii"),
    }}}


def translated_pcm(message):
    content = message.get("serverContent") or {}
    for part in (content.get("modelTurn") or {}).get("parts", ()):
        inline = part.get("inlineData") or {}
        if inline.get("mimeType", "").split(";")[0] != "audio/pcm":
            continue
        pcm = base64.b64decode(inline.get("data") or "", validate=True)
        if pcm and len(pcm) % 2 == 0:
            yield pcm


def _convert(resampler, pcm, source_rate, target_rate):
    if source_rate == target_rate:
        return pcm
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    output = resampler.process(samples, source_rate, target_rate)
    return np.rint(output).clip(-32768, 32767).astype("<i2").tobytes()


def mix_translation(original, translation, gain):
    """Keep the peer audible under translated speech, with a short gain ramp."""
    if translation is None and gain == 1.0:
        return original, gain
    source = np.frombuffer(original, dtype="<i2").astype(np.float32)
    spoken = np.frombuffer(translation or bytes(len(original)), dtype="<i2").astype(np.float32)
    audible = bool(np.max(np.abs(spoken), initial=0) >= 250)
    target = DUCKED_GAIN if audible else 1.0
    next_gain = target + (gain - target) * (0.25 if audible else 0.9)
    ramp = np.linspace(gain, next_gain, source.size, dtype=np.float32)
    mixed = (source * ramp + spoken).round().clip(-32768, 32767).astype("<i2")
    return mixed.tobytes(), next_gain


class OutgoingTranslationGate:
    """Send either the microphone or the interpreter, with microphone priority."""

    def __init__(self, sample_rate):
        self._pause_samples = round(sample_rate * MICROPHONE_PAUSE_SECONDS)
        self.reset()

    def reset(self):
        self._quiet_samples = 0

    def select(self, microphone, take_translation):
        samples = np.frombuffer(microphone, dtype="<i2").astype(np.float32)
        energy = np.dot(samples, samples)
        if energy >= samples.size * MICROPHONE_SPEECH_RMS ** 2:
            self.reset()
        else:
            self._quiet_samples = min(self._quiet_samples + samples.size, self._pause_samples)
        if self._quiet_samples >= self._pause_samples:
            translated = take_translation(len(microphone))
            if translated is not None:
                return translated
        return microphone


class LiveTranslationDirection:
    """Owns one socket and small, bounded PCM buffers for its call direction."""

    def __init__(self, api_key, language, output_rate, on_state, *, play=None, duck=False,
                 connector=_GoogleConnect):
        self._api_key, self.language = api_key, language
        self._output_rate, self._on_state, self._play = output_rate, on_state, play
        self._connect = connector
        self._duck = duck
        self._lock = threading.Lock()
        self._input = deque(maxlen=16)
        self._original = deque(maxlen=16)
        # Outgoing speech can wait behind the microphone for a full phrase.
        self._output = TranslationPlayout(output_rate, limit_seconds=4 if play is not None else 15)
        self._ready = False
        self._muted = False
        self._generation = 0
        self._handle = ""
        self._state = ""
        self._stopped = threading.Event()
        self._running = None
        self._thread = threading.Thread(target=self._run, name="gemini-call-translation", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stopped.set()
        with self._lock:
            self._ready = False
            self._input.clear()
            self._original.clear()
            self._output.clear()
        running = self._running
        if running is not None:
            loop, task = running
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The worker already closed the loop.

    def set_muted(self, muted):
        with self._lock:
            if muted == self._muted:
                return
            self._muted = muted
            self._generation += 1
            self._input.clear()
            self._original.clear()
            self._output.clear()
            self._ready = False

    def feed(self, pcm, rate):
        """Return whether the current translation socket accepted fresh PCM."""
        with self._lock:
            if not self._ready or self._stopped.is_set() or self._muted:
                return False
            frame = (time.monotonic(), pcm, rate)
            self._input.append(frame)
            if self._duck and self._play is not None:
                self._original.append(frame)
            return True

    def take(self, size):
        with self._lock:
            if not self._ready or self._muted:
                return None
            return self._output.take(size, time.monotonic())

    def _publish(self, state):
        if self._state != state and not self._stopped.is_set():
            self._state = state
            self._on_state(self, state)

    def _run(self):
        try:
            asyncio.run(self._reconnect())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            # Socket exceptions can contain service response text or the URL with its key.
            logging.warning("[call translation] worker stopped (%s)", type(exc).__name__)
            self._publish("failed")
        finally:
            self._running = None
            self.stop()

    async def _reconnect(self):
        self._running = asyncio.get_running_loop(), asyncio.current_task()
        tls = system_ssl_context() or ssl.create_default_context()
        logger = logging.Logger("gemini-call-websocket")
        logger.disabled = True
        uri = f"{ENDPOINT}?{urlencode({'key': self._api_key})}"
        generation, failures = -1, 0
        while not self._stopped.is_set():
            with self._lock:
                current, muted = self._generation, self._muted
            if current != generation:
                # A mute/unmute boundary discards previous microphone context.
                self._handle, generation, failures = "", current, 0
            if muted:
                await asyncio.sleep(FRAME_SECONDS)
                continue
            used_handle = self._handle
            opened_at = time.monotonic()
            self._publish("reconnecting" if self._state else "connecting")
            try:
                async with self._connect(uri, ssl=tls, logger=logger, open_timeout=8,
                                         close_timeout=0.25, max_size=2 ** 20) as ws:
                    await self._run_socket(ws, generation)
                    failures = 0
            except _NETWORK_ERRORS as exc:
                if isinstance(exc, InvalidStatus) and exc.response.status_code in (400, 401, 403):
                    self._publish("failed")
                    return
                if used_handle and isinstance(exc, _SetupRefused):
                    self._handle = ""
                failures = 1 if time.monotonic() - opened_at >= 20 else failures + 1
                if failures >= 3:
                    self._publish("failed")
                    return
                for _ in range(10 * failures):
                    if self._stopped.is_set() or generation != self._generation:
                        break
                    await asyncio.sleep(FRAME_SECONDS)

    async def _run_socket(self, ws, generation):
        echo = self._play is not None and not self._duck
        setup = setup_message(self.language, self._handle, echo_target_language=echo)
        await asyncio.wait_for(ws.send(json.dumps(setup)), 3)
        try:
            response = json.loads(await asyncio.wait_for(ws.recv(), 8))
        except ConnectionClosed as exc:
            if exc.rcvd and exc.rcvd.code in (1007, 1008, 1011):
                raise _SetupRefused() from exc
            raise
        if "setupComplete" not in response:
            raise _SetupRefused()
        with self._lock:
            if self._stopped.is_set() or generation != self._generation:
                return
            self._ready = True
        self._publish("ready")
        tasks = [asyncio.create_task(self._receive(ws, generation)),
                 asyncio.create_task(self._send_audio(ws, generation))]
        if self._play is not None:
            tasks.append(asyncio.create_task(self._play_audio(generation)))
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            with self._lock:
                self._ready = False
                self._input.clear()
                self._original.clear()
                self._output.clear()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _receive(self, ws, generation):
        resampler = StreamingMonoResampler()
        while not self._stopped.is_set():
            message = json.loads(await ws.recv())
            update = message.get("sessionResumptionUpdate") or {}
            if update.get("resumable") is True and isinstance(update.get("newHandle"), str):
                self._handle = update["newHandle"] or self._handle
            if "goAway" in message:
                return
            for pcm in translated_pcm(message):
                audio = _convert(resampler, pcm, OUTPUT_RATE, self._output_rate)
                with self._lock:
                    if generation != self._generation or self._stopped.is_set():
                        return
                    self._output.push(audio, time.monotonic())

    async def _send_audio(self, ws, generation):
        resampler = StreamingMonoResampler()
        packet = bytearray()
        while not self._stopped.is_set():
            with self._lock:
                if generation != self._generation:
                    return
                frames = tuple(self._input)
                self._input.clear()
            now = time.monotonic()
            for timestamp, pcm, rate in frames:
                if now - timestamp > 0.35:
                    packet.clear()
                    resampler.reset()
                    continue
                packet.extend(_convert(resampler, pcm, rate, INPUT_RATE))
            while len(packet) >= INPUT_BYTES and generation == self._generation and not self._stopped.is_set():
                payload = input_message(bytes(packet[:INPUT_BYTES]))
                del packet[:INPUT_BYTES]
                await asyncio.wait_for(ws.send(json.dumps(payload)), 3)
            await asyncio.sleep(FRAME_SECONDS)

    async def _play_audio(self, generation):
        # Keep the speaker clock independent of websocket backpressure.
        rate = self._output_rate
        size = round(rate * FRAME_SECONDS) * 2
        clock = asyncio.get_running_loop().time
        next_frame = clock()
        resampler = StreamingMonoResampler()
        original = bytearray()
        gain, playing = 1.0, False
        while not self._stopped.is_set():
            with self._lock:
                if generation != self._generation:
                    return
                frames = tuple(self._original)
                self._original.clear()
            now = time.monotonic()
            for timestamp, pcm, source_rate in frames:
                if now - timestamp <= 0.24:
                    original.extend(_convert(resampler, pcm, source_rate, rate))
                else:
                    original.clear()
                    resampler.reset()
            del original[:max(0, len(original) - size * 12)]
            now = clock()
            if now >= next_frame:
                due = 1 + int((now - next_frame) / FRAME_SECONDS)
                next_frame += due * FRAME_SECONDS
                # The device drains on its own clock. Refill the frames a
                # late wakeup missed without flooding it after a long stall.
                for _ in range(min(due, MAX_CATCH_UP_FRAMES)):
                    audio = self.take(size)
                    if self._duck and (original or audio is not None):
                        source = bytes(original[:size]).ljust(size, b"\0")
                        del original[:size]
                        audio, gain = mix_translation(source, audio, gain)
                    if audio is not None:
                        playing = True
                    if playing:
                        self._play(self, audio if audio is not None else bytes(size), rate)
            await asyncio.sleep(max(0, next_frame - clock()))
