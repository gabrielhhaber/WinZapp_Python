"""Gemini rollover, call audio routing, and microphone privacy."""
import asyncio
import base64
from contextlib import asynccontextmanager, suppress
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import call_audio as audio_module, live_translation as translation_module
from core.call_audio import CALL_SAMPLE_RATE, CallAudioConfig, CallAudioSession
from core.live_translation import INPUT_BYTES, LiveTranslationDirection, MODEL, OUTPUT_RATE, mix_translation
from core.live_translation_languages import language_defaults
from core.live_translation_playout import TranslationPlayout
from main_window.call_translation import CallTranslationMixin, _is_group_call


class Socket:
    def __init__(self, first_message=None):
        self.messages = asyncio.Queue()
        self.sent = asyncio.Queue()
        self.messages.put_nowait(first_message if first_message is not None else {"setupComplete": {}})

    async def recv(self):
        return json.dumps(await self.messages.get())

    async def send(self, message):
        self.sent.put_nowait(json.loads(message))


async def setup_as_seen_by_google(socket):
    return (await asyncio.wait_for(socket.sent.get(), 1))["setup"]


async def test_goaway_reuses_latest_resumable_handle_with_the_same_language():
    sockets = asyncio.Queue()
    audio = asyncio.Queue()

    @asynccontextmanager
    async def connect(uri, **kwargs):
        socket = Socket()
        sockets.put_nowait(socket)
        yield socket

    published = asyncio.Queue()
    direction = LiveTranslationDirection(
        "fake", "ar", OUTPUT_RATE, lambda _, state: published.put_nowait(state),
        play=lambda _, pcm, rate: audio.put_nowait((pcm, rate)), connector=connect)
    task = asyncio.create_task(direction._reconnect())
    try:
        first = await asyncio.wait_for(sockets.get(), 1)
        setup = await setup_as_seen_by_google(first)
        assert setup["model"] == f"models/{MODEL}"
        assert setup["generationConfig"]["translationConfig"]["targetLanguageCode"] == "ar"
        assert setup["sessionResumption"] == {}
        assert setup["contextWindowCompression"] == {"slidingWindow": {}}
        assert await asyncio.wait_for(published.get(), 1) == "connecting"
        assert await asyncio.wait_for(published.get(), 1) == "ready"

        pcm = b"\x22\x00" * (INPUT_BYTES // 2)
        assert direction.feed(pcm, 16000)
        sent = (await asyncio.wait_for(first.sent.get(), 1))["realtimeInput"]["audio"]
        assert sent["mimeType"] == "audio/pcm;rate=16000"
        assert base64.b64decode(sent["data"]) == pcm

        translated = b"\x33\x00" * (OUTPUT_RATE // 50)
        first.messages.put_nowait({"serverContent": {"modelTurn": {"parts": [{"inlineData": {
            "mimeType": "audio/pcm;rate=24000", "data": base64.b64encode(translated).decode("ascii"),
        }}]}}})
        assert await asyncio.wait_for(audio.get(), 1) == (translated, OUTPUT_RATE)

        for handle, resumable in [("old", True), ("latest", True), ("unusable", False)]:
            first.messages.put_nowait({"sessionResumptionUpdate": {
                "resumable": resumable, "newHandle": handle}})
        first.messages.put_nowait({"goAway": {"timeLeft": "20s"}})
        second = await asyncio.wait_for(sockets.get(), 1)
        resumed = await setup_as_seen_by_google(second)
        assert resumed["sessionResumption"] == {"handle": "latest"}
        assert resumed["generationConfig"] == setup["generationConfig"]
    finally:
        direction.stop()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)


async def test_rejected_resume_reconnects_once_without_the_old_handle():
    sockets = asyncio.Queue()
    attempt = 0

    @asynccontextmanager
    async def connect(uri, **kwargs):
        nonlocal attempt
        attempt += 1
        socket = Socket({"error": "invalid resumption token"} if attempt == 2 else None)
        sockets.put_nowait(socket)
        yield socket

    direction = LiveTranslationDirection("fake", "en", OUTPUT_RATE, Mock(), connector=connect)
    task = asyncio.create_task(direction._reconnect())
    try:
        first = await asyncio.wait_for(sockets.get(), 1)
        await setup_as_seen_by_google(first)
        first.messages.put_nowait({"sessionResumptionUpdate": {
            "resumable": True, "newHandle": "expired"}})
        first.messages.put_nowait({"goAway": {}})
        second = await asyncio.wait_for(sockets.get(), 1)
        assert (await setup_as_seen_by_google(second))["sessionResumption"] == {"handle": "expired"}
        third = await asyncio.wait_for(sockets.get(), 2)
        assert (await setup_as_seen_by_google(third))["sessionResumption"] == {}
    finally:
        direction.stop()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)


async def test_local_translation_plays_while_the_socket_upload_is_waiting():
    sending = asyncio.Event()
    playing = asyncio.Queue()
    never_sent = asyncio.Event()

    class SlowSocket(Socket):
        async def send(self, payload):
            if "realtimeInput" in json.loads(payload):
                sending.set()
                await never_sent.wait()
            await super().send(payload)

    socket = SlowSocket()
    ready = asyncio.Event()
    direction = LiveTranslationDirection(
        "fake", "ar", OUTPUT_RATE,
        lambda _, state: ready.set() if state == "ready" else None,
        play=lambda _, pcm, rate: playing.put_nowait(pcm))
    task = asyncio.create_task(direction._run_socket(socket, 0))
    try:
        await asyncio.wait_for(ready.wait(), 1)
        direction.feed(bytes(INPUT_BYTES), 16000)
        await asyncio.wait_for(sending.wait(), 1)
        sample = b"\x42\x00"
        socket.messages.put_nowait({"serverContent": {"modelTurn": {"parts": [{"inlineData": {
            "mimeType": "audio/pcm;rate=24000",
            "data": base64.b64encode(sample * (OUTPUT_RATE // 4)).decode("ascii"),
        }}]}}})
        assert await asyncio.wait_for(playing.get(), 0.6) == sample * (OUTPUT_RATE // 50)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("both", [False, True])
def test_routes_are_independent_and_desktop_audio_never_reaches_gemini(monkeypatch, both):
    class Worker:
        def __init__(self, key, language, rate, callback, play=None, duck=False):
            self.language, self.play = language, play
            self.feed, self.stop, self.set_muted = Mock(return_value=True), Mock(), Mock()
            self.take = Mock(side_effect=lambda size: b"\x00\x08" * (size // 2))
            self.start = Mock()

    monkeypatch.setattr(audio_module, "LiveTranslationDirection", Worker)
    sio = Mock()
    audio = CallAudioSession(sio, CallAudioConfig(session="test"), sounddevice_module=object())
    audio._input_stream = object()
    peer_audio, local_mic = b"\x11\x00" * 960, b"\x00\x04" * 960
    assert audio.start_live_translation("fake", "ar", "en" if both else "", Mock())
    incoming, *outgoing = audio.live_translation
    assert incoming.language == "ar"
    audio.enqueue_remote_audio(peer_audio, 16000)
    incoming.feed.assert_called_once_with(peer_audio, 16000)
    assert audio._output_queue.empty()

    incoming.play(incoming, b"\x33\x00", OUTPUT_RATE)
    assert audio._output_queue.get_nowait().pcm == b"\x33\x00"
    audio._desktop_audio = SimpleNamespace(take=lambda size: bytes(size), volume=100)
    audio._emit_microphone_frame(local_mic, "")
    emitted = sio.emit.call_args.args[1]
    if both:
        (sender,) = outgoing
        assert sender.language == "en"
        sender.feed.assert_called_once_with(local_mic, CALL_SAMPLE_RATE)
        assert base64.b64decode(emitted["pcm"]) == local_mic
        sender.take.assert_not_called()
        silence = bytes(len(local_mic))
        for _ in range(12):
            audio._emit_microphone_frame(silence, "")
            assert base64.b64decode(sio.emit.call_args.args[1]["pcm"]) == silence
        sender.take.assert_not_called()
        for _ in range(5):
            audio._emit_microphone_frame(silence, "")
        assert base64.b64decode(sio.emit.call_args.args[1]["pcm"]) == b"\x00\x08" * 960
        take_count = sender.take.call_count
        audio._emit_microphone_frame(local_mic, "")
        assert base64.b64decode(sio.emit.call_args.args[1]["pcm"]) == local_mic
        sender.take.assert_called_with(len(local_mic))
        assert sender.take.call_count == take_count
        count = sender.feed.call_count
        audio.set_microphone_muted(True)
        audio._emit_microphone_frame(local_mic, "")
        assert sender.feed.call_count == count
        assert base64.b64decode(sio.emit.call_args.args[1]["pcm"]) == bytes(len(local_mic))
    else:
        assert not outgoing
        assert base64.b64decode(emitted["pcm"]) == local_mic

    assert audio.stop_live_translation()
    incoming.play(incoming, peer_audio, OUTPUT_RATE)
    assert audio._output_queue.empty()
    audio.enqueue_remote_audio(peer_audio, 16000)
    assert audio._output_queue.get_nowait().pcm == peer_audio


def test_mute_clears_pending_pcm_and_retires_the_socket_generation():
    direction = LiveTranslationDirection("fake", "ar", OUTPUT_RATE, Mock())
    direction._ready = True
    assert direction.feed(b"\x10\x00", 16000)
    direction._output.push(b"\x20\x00", 0)
    assert direction.take(2) == b"\x20\x00"
    direction._output.push(b"\x30\x00", 0)
    direction.set_muted(True)
    assert not direction._input and direction._output.take(2, 1.0) is None
    assert direction.take(2) is None
    assert not direction.feed(b"\x40\x00", 16000)
    direction.set_muted(False)
    assert direction._generation == 2
    assert not direction.feed(b"\x40\x00", 16000)


async def test_late_speaker_wakeups_do_not_slow_down_buffered_translation(monkeypatch):
    now = 1.0
    wakeups = iter((1.052, 1.093, 1.198))
    played = []
    direction = LiveTranslationDirection(
        "fake", "ar", OUTPUT_RATE, Mock(), play=lambda _, pcm, rate: played.append(pcm))
    direction._ready = True
    samples = OUTPUT_RATE // 50
    frames = [number.to_bytes(2, "little") * samples for number in range(1, 26)]
    direction._output.push(b"".join(frames), now)

    async def sleep(_):
        nonlocal now
        woke = next(wakeups, None)
        if woke is None:
            direction.stop()
        else:
            now = woke

    monkeypatch.setattr(translation_module, "asyncio", SimpleNamespace(
        get_running_loop=lambda: SimpleNamespace(time=lambda: now), sleep=sleep))
    monkeypatch.setattr(translation_module, "time", SimpleNamespace(monotonic=lambda: now))
    await direction._play_audio(0)
    assert played == frames[:10]


def test_playout_buffers_bursts_and_releases_the_final_partial_frame_once():
    frame = b"\x23\x01" * (OUTPUT_RATE // 50)
    buffer = TranslationPlayout(OUTPUT_RATE)
    buffer.push(frame * 9, 1.0)
    assert buffer.take(len(frame), 1.06) is None
    buffer.push(frame * 7, 1.08)
    for tick in range(16):
        assert buffer.take(len(frame), 1.08 + tick * 0.02) == frame
    assert buffer.take(len(frame), 1.40) is None
    buffer.push(frame[:4], 1.42)
    assert buffer.take(len(frame), 1.63) is None
    assert buffer.take(len(frame), 1.74) == frame[:4].ljust(len(frame), b"\0")
    assert buffer.take(len(frame), 1.76) is None


def test_language_preferences_reuse_both_codes_and_validate_older_settings():
    assert language_defaults({}, "ar") == ("ar", "", False)
    assert language_defaults({}, "en-US") == ("en", "", False)
    saved = {"mine": "pt-PT", "peer": "zh-Hant", "duck": True}
    assert language_defaults(saved, "ar") == ("pt-PT", "zh-Hant", True)
    assert language_defaults({"mine": [], "peer": {}}, "tr-TR") == ("tr", "", False)


def test_ducking_keeps_original_audio_and_restores_it_when_translation_pauses():
    import numpy as np

    original = np.full(480, 1000, dtype="<i2").tobytes()
    translation = np.full(480, 2000, dtype="<i2").tobytes()
    untouched, gain = mix_translation(original, None, 1)
    assert untouched == original
    mixed, lower = mix_translation(original, translation, gain)
    result = np.frombuffer(mixed, dtype="<i2")
    assert result[0] == 3000
    assert 2000 < result[-1] < 3000
    recovering, higher = mix_translation(original, None, lower)
    samples = np.frombuffer(recovering, dtype="<i2")
    assert lower < higher < 1
    assert 0 < samples[0] < samples[-1] < 1000


@pytest.mark.parametrize("flag", ["is_group", "translation_group"])
def test_a_call_that_becomes_a_group_stops_translating_and_hides_the_button(flag):
    audio = SimpleNamespace(running=True, live_translation=(object(),), stop_live_translation=Mock())
    button = Mock()
    owner = SimpleNamespace(
        voice_call_window_translation_button=button, voice_call_window_mute_button=Mock(),
        _call_audio_session=audio, _voice_call_last_announced_state="ACTIVE",
        output=Mock(), i18n=SimpleNamespace(t=lambda key: key),
    )
    CallTranslationMixin._sync_live_translation_button(owner, {})
    button.Show.assert_called_with(False)
    CallTranslationMixin._sync_live_translation_button(owner, {"call_id": "single"})
    button.Show.assert_called_with(True)
    CallTranslationMixin._sync_live_translation_button(owner, {flag: True})
    audio.stop_live_translation.assert_called_once_with()
    button.Show.assert_called_with(False)


def test_group_detection_handles_a_roster_received_before_the_call_connected():
    active = {"call_id": "call"}
    owner = SimpleNamespace(_call_rosters={"call": ["p@s.whatsapp.net", "p@lid"]},
                            _chat_jids_equivalent=lambda a, b: a.partition("@")[0] == b.partition("@")[0])
    assert not _is_group_call(owner, active)
    owner._call_rosters["call"].append("q@s.whatsapp.net")
    assert _is_group_call(owner, active)
    owner._call_rosters.clear()
    assert _is_group_call(owner, active)
