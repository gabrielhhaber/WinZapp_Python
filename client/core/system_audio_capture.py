"""Headless microphone + system audio, optionally separating NVDA process audio.

The default captures one entire render endpoint. Opt-in NVDA separation uses
disjoint other/NVDA process streams across output endpoints when NVDA is present;
only absence of NVDA permits the ordinary endpoint path. QPC timestamps (100 ns)
align sources on one bounded timeline. Each source retains the original .5 gain
and the mixer emits clipped little-endian signed 16-bit PCM.
"""
from array import array
from dataclasses import dataclass
import math
import sys
import queue
import threading
import time


# Recording is not live monitoring: reserve 500ms for scheduling jitter.
# This covers the observed 249/335ms stalls with margin, not a guarantee.
PLAYOUT_DELAY_100NS = 5_000_000
LATE_TOLERANCE_SECONDS = .5


@dataclass(frozen=True)
class AudioPacket:
    timestamp: int  # QPC in 100 ns units, NOT time.time()
    data: bytes  # interleaved IEEE float32, little endian, target rate/channels


class TimelineMixer:
    """Bounded timestamp-to-PCM timeline; missing source packets are silence."""

    def __init__(self, rate, channels, epoch, capacity_seconds=2, *, late_tolerance_seconds=0.,
                 source_count=2):
        if type(source_count) is not int or source_count < 1:
            raise ValueError('Source count must be a positive integer')
        self.source_count = source_count
        self.rate, self.channels, self.epoch = rate, channels, epoch
        self.capacity = int(rate * capacity_seconds)
        if not 0 <= late_tolerance_seconds <= capacity_seconds:
            raise ValueError("Late tolerance must fit inside the bounded buffer")
        self._late_tolerance = int(rate * late_tolerance_seconds)
        self.late_packets, self.late_frames = [0] * source_count, [0] * source_count
        self.position = 0
        self._ring = array('f', [0]) * (self.capacity * channels)
        self._pending = [None] * source_count
        self._last_stamp = [None] * source_count
        self._last_end = [None] * source_count

    def _frame(self, timestamp):
        return round((timestamp - self.epoch) * self.rate / 10_000_000)

    def add(self, source, packet):
        if source not in range(self.source_count) or len(packet.data) % (4 * self.channels):
            raise ValueError('Invalid audio packet')
        start = self._frame(packet.timestamp)
        frames = len(packet.data) // (4 * self.channels)
        late = max(0, self.position - start) if self.position and frames else 0
        if late > self._late_tolerance:
            raise ValueError('Capture packet arrived after its playback deadline')
        if start + frames > self.position + self.capacity:
            raise ValueError('Capture timeline exceeded its bounded buffer')
        if not frames:
            return
        previous = self._last_stamp[source]
        if previous is not None and packet.timestamp <= previous:
            raise ValueError('Capture timestamp went backwards')
        self._commit(source, packet)
        if late:
            # _commit clips to position: never replay already emitted time or
            # move the remaining tail forward relative to the other source.
            self.late_packets[source] += 1
            self.late_frames[source] += min(frames, late)
        self._pending[source] = packet
        self._last_stamp[source] = packet.timestamp

    def _commit(self, source, following=None):
        packet = self._pending[source]
        if packet is None:
            return
        samples = array('f')
        samples.frombytes(packet.data)
        if sys.byteorder != 'little':
            samples.byteswap()
        count = len(samples) // self.channels
        start = self._frame(packet.timestamp)
        end = start + count
        if following is not None:
            next_start = self._frame(following.timestamp)
            # The OS does nominal-rate high-quality resampling. Re-time this
            # packet over its actual QPC interval for the small remaining
            # hardware-clock drift. Keep phase in absolute timeline frames,
            # not independently rounded durations. Large gaps are silence,
            # NEVER stretch the last sound through a quiet loopback interval.
            if abs(next_start - end) <= max(1, count * .01):
                end = next_start
        size = end - start
        if size <= 0:
            raise ValueError('Capture packet has an invalid clock interval')
        if end > self.position + self.capacity:
            raise ValueError('Clock correction exceeded the bounded buffer')
        last_end = self._last_end[source]
        begin = max(start, self.position, last_end if last_end is not None else start)
        # QPC jitter can put the next packet slightly before the previous
        # end (observed: 5 frames at 48 kHz). Keep its original time and skip
        # only the overlapping prefix via begin above; never double-mix it.
        # Bound this to 1 ms; large overlaps or wholly covered packets fail.
        if last_end is not None and start < last_end:
            if last_end - start > max(1, round(self.rate * .001)) or end <= last_end:
                raise ValueError('Capture packets overlap')
        for frame in range(begin, end):
            location = (frame - start) * count / size
            left = min(count - 1, int(location))
            fraction = location - left
            for channel in range(self.channels):
                a = samples[left * self.channels + channel]
                if left + 1 < count:
                    b = samples[(left + 1) * self.channels + channel]
                elif following is not None and size != count:
                    # One lookahead sample keeps interpolation continuous at
                    # packet boundaries; no Python/global device resampler.
                    import struct
                    b = struct.unpack_from('<f', following.data, channel * 4)[0]
                else:
                    b = a
                a = max(-65536., min(65536., a)) if math.isfinite(a) else 0.
                b = max(-65536., min(65536., b)) if math.isfinite(b) else 0.
                value = a + fraction * (b - a)
                offset = (frame % self.capacity) * self.channels + channel
                self._ring[offset] += value * .5
        self._last_end[source] = end
        self._pending[source] = None

    def render_until(self, timestamp):
        end = max(self.position, self._frame(timestamp))
        for source, packet in enumerate(self._pending):
            if packet is not None and self._frame(packet.timestamp) <= end:
                self._commit(source)
        if end - self.position > self.capacity:
            raise ValueError('Capture worker missed the bounded buffer deadline')
        pcm = array('h')
        while self.position < end:
            for channel in range(self.channels):
                offset = (self.position % self.capacity) * self.channels + channel
                sample = self._ring[offset]
                self._ring[offset] = 0
                pcm.append(max(-32768, min(32767, round(sample * 32768))))
            self.position += 1
        if sys.byteorder != 'little':
            pcm.byteswap()
        return pcm.tobytes()


class SystemAudioRecorder:
    """PCM16 recorder coordinating endpoint and optional process-reader streams.

    The microphone is always captured in mono, then centred in stereo output.
    Computer streams retain the requested output channels.

    start() waits for all clients (or raises); callbacks never precede successful
    startup. set_paused() acknowledges a shared timestamp boundary, stops and
    resets all streams; resume starts a fresh timeline without paused samples.
    Process-stream reset discards/drains only: it never calls IAudioClient.Reset.
    Native process readers own their COM threads and bounded FIFOs.
    stop()/stop_stream()/close() are idempotent and join the worker. Callbacks
    must be quick and must not wait on the UI thread. on_error is emitted once
    for a runtime failure, after cleanup; startup errors are raised by start().

    A 500 ms playout reserve absorbs transient WASAPI scheduling stalls.
    Packets up to 500 ms behind already emitted PCM lose only their late prefix;
    timestamps are never shifted. Greater lateness, overflow, missing microphone
    packets for 2 s or endpoint loss fails closed, preserving delivered PCM.
    OS COM calls cannot be forcibly interrupted safely: API waits are bounded, but a wedged driver may keep the
    daemon worker alive until that native call returns (then it cleans up).
    """
    def __init__(self, on_frames, on_error, *, microphone_name='', channels=1,
                 rate=48000, backend_factory=None, startup_timeout=5.,
                 shutdown_timeout=5., diagnostics=None, system_audio_volume=100,
                 separate_nvda=False, nvda_volume=100):
        from .wasapi_capture import capture_format, WasapiBackend
        capture_format(rate, channels)
        if not callable(on_frames) or not callable(on_error):
            raise TypeError('Recording callbacks must be callable')
        self.rate, self.channels = rate, channels
        self.failure = None
        # Published once before _ready: a later device loss must not turn an
        # already successful dual start into a startup exception in the waiter.
        self._started_successfully = False
        self._on_frames, self._on_error = on_frames, on_error
        self._name = microphone_name
        self._factory = backend_factory or WasapiBackend
        self._startup_timeout, self._shutdown_timeout = startup_timeout, shutdown_timeout
        self._ready, self._done = threading.Event(), threading.Event()
        self._stop, self._wake = threading.Event(), threading.Event()
        self._commands = queue.Queue(maxsize=16)
        self._lock = threading.Lock()
        self.set_system_audio_volume(system_audio_volume)
        self.set_nvda_volume(nvda_volume)
        self._separate_nvda = bool(separate_nvda)
        self._nvda_volume_available = False
        self._source_names = ('microphone', 'loopback')
        self._thread = None
        self._clock = None
        self._stop_stamp = None
        from .system_audio_diagnostics import CaptureDiagnostics
        self._diagnostics = diagnostics if diagnostics is not None else CaptureDiagnostics(
            rate=rate, channels=channels)

    def set_system_audio_volume(self, percent):
        """Set other-audio gain (whole endpoint if not separated), also paused.

        Does not change Windows/device volume or previously captured PCM.
        The worker snapshots the gain per packet; an in-flight packet may
        retain the previous value. Microphone gain stays unchanged.
        """
        try:
            percent = float(percent)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('System-audio volume must be finite and between 0 and 100') from exc
        if not math.isfinite(percent) or not 0 <= percent <= 100:
            raise ValueError('System-audio volume must be finite and between 0 and 100')
        with self._lock:
            self._system_audio_gain = percent / 100

    @property
    def nvda_volume_available(self):
        """True only after all isolated capture streams started successfully."""
        with self._lock:
            return self._nvda_volume_available

    def set_nvda_volume(self, percent):
        """Set the isolated NVDA gain for new packets; never change mic/other PCM."""
        try:
            percent = float(percent)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('NVDA volume must be finite and between 0 and 100') from exc
        if not math.isfinite(percent) or not 0 <= percent <= 100:
            raise ValueError('NVDA volume must be finite and between 0 and 100')
        with self._lock:
            self._nvda_gain = percent / 100

    def _diagnose(self, method, *args, **kwargs):
        # Also contain failures from injected sinks; logging cannot abort capture.
        try:
            getattr(self._diagnostics, method)(*args, **kwargs)
        except Exception:
            pass

    def _observe_stream(self, source, stream):
        try:
            self._diagnose('update', source=self._source_names[source],
                           native=stream.diagnostic_snapshot())
        except Exception:
            pass  # injected/third-party streams need not expose diagnostics

    def _observe_packet(self, source, packet, mixer):
        try:
            start = mixer._frame(packet.timestamp)
            previous = mixer._last_stamp[source]
            self._diagnose('packet', self._source_names[source],
                           packet_frames=len(packet.data) // (4 * self.channels),
                           packet_start_frame=start, position=mixer.position,
                           deadline_frames=start - mixer.position,
                           last_end_frame=mixer._last_end[source],
                           stamp_delta_ms=(packet.timestamp - previous) / 10000
                           if previous is not None else None)
        except Exception:
            pass

    def start(self):
        with self._lock:
            if self._thread is not None or self._stop.is_set():
                raise RuntimeError('A recorder instance can only be started once')
            self._thread = threading.Thread(target=self._run, name='WASAPI dual capture', daemon=True)
            self._thread.start()
        if not self._ready.wait(self._startup_timeout):
            self.failure = TimeoutError('Timed out opening both recording devices')
            self._diagnose('event', 'error', error=self.failure)
            self._stop.set()
            self._wake.set()
            raise self.failure
        if not self._started_successfully and self.failure is not None:
            raise self.failure
        if self._stop.is_set():
            raise RuntimeError('Recording was cancelled during startup')

    def set_paused(self, paused):
        if self._thread is None or not self._ready.is_set():
            raise RuntimeError('Recorder has not started')
        if self._done.is_set() or self._stop.is_set():
            if self.failure:
                raise self.failure
            return
        if threading.current_thread() is self._thread:
            raise RuntimeError('Pause must be requested outside the capture callback')
        acknowledged = threading.Event()
        try:
            self._commands.put((bool(paused), self._clock(), acknowledged),
                               timeout=self._shutdown_timeout)
        except queue.Full as exc:
            self._diagnose('event', 'error', error=exc, reason='pause_queue_timeout')
            raise
        self._wake.set()
        deadline = time.monotonic() + self._shutdown_timeout
        while not acknowledged.wait(.01):
            if self._done.is_set():
                if self.failure:
                    raise self.failure
                return
            if time.monotonic() >= deadline:
                self._stop.set()
                self._wake.set()
                exc = TimeoutError('Timed out changing capture pause state')
                self._diagnose('event', 'error', error=exc)
                raise exc

    def stop(self):
        with self._lock:
            if not self._stop.is_set():
                self._stop_stamp = self._clock() if self._clock is not None else None
                self._stop.set()
            thread = self._thread
        self._wake.set()
        if thread is not None and threading.current_thread() is not thread:
            thread.join(self._shutdown_timeout)
            if thread.is_alive():
                exc = TimeoutError('Capture worker did not stop before the deadline')
                self._diagnose('event', 'error', error=exc)
                raise exc

    stop_stream = stop
    close = stop

    def _run(self):
        backend, streams, mixer = None, [], None
        active, paused, last_safe = False, False, None
        callback_failed = False
        delivered_frames = 0
        late_packets, late_frames = [0, 0, 0], [0, 0, 0]
        last_poll = time.monotonic()

        def emit(stamp):
            nonlocal callback_failed, delivered_frames
            if mixer is not None:
                self._diagnose('update', phase='render', source='mixer',
                               position=mixer.position, render_target_frame=mixer._frame(stamp))
                pcm = mixer.render_until(stamp)
                if pcm:
                    try:
                        self._diagnose('update', phase='frames', source='callback')
                        before = time.monotonic()
                        self._on_frames(pcm)
                        delivered_frames += len(pcm) // (2 * self.channels)
                        self._diagnose('update', delivered_frames=delivered_frames)
                    except Exception:
                        callback_failed = True
                        raise
                    finally:
                        self._diagnose('update', callback_ms=(time.monotonic() - before) * 1000)

        def stream_call(source, method):
            stream = streams[source]
            self._diagnose('update', phase='read' if method == 'read_packets' else method,
                           source=self._source_names[source])
            before = time.monotonic()
            try:
                if method == 'start' and getattr(stream, 'process_loopback', False):
                    return stream.start(cancel_event=self._stop)
                return getattr(stream, method)()
            finally:
                self._observe_stream(source, stream)
                if method == 'read_packets':
                    self._diagnose('update', **{self._source_names[source] + '_read_ms':
                                               (time.monotonic() - before) * 1000})

        def collect():
            assert mixer is not None  # collection begins only after dual Start
            # Fetch ALL before committing any to the mixer: no reduced-source
            # fallback if another capture stream failed this very poll.
            batches = [stream_call(source, 'read_packets') for source in range(len(streams))]
            for source, packets in enumerate(batches):
                for packet in packets:
                    if source == 0 and self.channels == 2:
                        # Capture mono at the device, then centre the voice in
                        # stereo without changing its QPC time or frame count.
                        # Slice copies preserve float32 bytes, including endian.
                        mono = array('f')
                        mono.frombytes(packet.data)
                        stereo = array('f', [0]) * (len(mono) * 2)
                        stereo[::2] = mono
                        stereo[1::2] = mono
                        packet = AudioPacket(packet.timestamp, stereo.tobytes())
                    self._observe_packet(source, packet, mixer)
                    self._diagnose('update', phase='mix', source=self._source_names[source])
                    previous_packets = mixer.late_packets[source]
                    previous_frames = mixer.late_frames[source]
                    if source in (1, 2):
                        with self._lock:
                            gain = self._system_audio_gain if source == 1 else self._nvda_gain
                        if gain != 1.:
                            # Freeze gain on arrival, before pending lookahead
                            # or ring-buffer mixing. Later UI changes must not
                            # rescale already captured/committed audio.
                            samples = array('f')
                            samples.frombytes(packet.data)
                            if sys.byteorder != 'little':
                                samples.byteswap()
                            for index in range(len(samples)):
                                samples[index] *= gain
                            if sys.byteorder != 'little':
                                samples.byteswap()
                            packet = AudioPacket(packet.timestamp, samples.tobytes())
                    mixer.add(source, packet)
                    if mixer.late_packets[source] != previous_packets:
                        late_packets[source] += mixer.late_packets[source] - previous_packets
                        late_frames[source] += mixer.late_frames[source] - previous_frames
                        name = self._source_names[source]
                        self._diagnose('update', **{
                            name + '_late_packets': late_packets[source],
                            name + '_late_frames': late_frames[source]})
            return bool(batches[0])

        try:
            self._diagnose('update', playout_delay_ms=PLAYOUT_DELAY_100NS / 10000,
                           late_tolerance_ms=LATE_TOLERANCE_SECONDS * 1000,
                           microphone_late_packets=0, loopback_late_packets=0,
                           microphone_late_frames=0, loopback_late_frames=0)
            self._diagnose('event', 'start')
            self._diagnose('update', phase='open', source='backend')
            backend = self._factory()
            self._clock = backend.clock
            if self._stop.is_set():
                return
            self._diagnose('update', phase='open', source='microphone')
            streams.append(backend.open_microphone(self._name, self.rate, 1))
            self._diagnose('event', 'opened')
            if self._stop.is_set():
                return
            self._diagnose('update', phase='open', source='loopback')
            separated = None
            if self._separate_nvda:
                self._diagnose('update', source='nvda')
                separated = backend.open_nvda_loopbacks(self.rate, self.channels)
            if separated is None:
                self._diagnose('update', source='loopback')
                if self._stop.is_set():
                    return
                streams.append(backend.open_loopback(self.rate, self.channels))
            else:
                self._source_names = ('microphone', 'other', 'nvda')
                streams.extend(separated)
            self._diagnose('event', 'opened')
            if self._stop.is_set():
                return
            for source, stream in enumerate(streams):
                if self._stop.is_set():
                    return
                stream_call(source, 'start')
            if self._stop.is_set():
                return
            epoch = self._clock()
            mixer = TimelineMixer(self.rate, self.channels, epoch,
                                  late_tolerance_seconds=LATE_TOLERANCE_SECONDS,
                                  source_count=len(streams))
            last_safe = last_mic = epoch
            active = True
            with self._lock:
                self._nvda_volume_available = separated is not None
            self._started_successfully = True
            self._ready.set()
            while not self._stop.is_set():
                poll = time.monotonic()
                self._diagnose('update', poll_gap_ms=(poll - last_poll) * 1000)
                last_poll = poll
                self._wake.clear()
                try:
                    desired, stamp, ack = self._commands.get_nowait()
                except queue.Empty:
                    pass
                else:
                    if desired != paused:
                        if desired:
                            for source in range(len(streams)):
                                stream_call(source, 'stop')
                            collect()
                            emit(stamp)
                            for source in range(len(streams)):
                                stream_call(source, 'reset')
                            paused = True
                            self._diagnose('update', phase='pause', source='worker')
                            self._diagnose('event', 'pause')
                        else:
                            for source in range(len(streams)):
                                # Cancellation can arrive while an earlier native
                                # Start is blocked. Do not start later sources.
                                if self._stop.is_set():
                                    return
                                stream_call(source, 'start')
                            if self._stop.is_set():
                                return
                            epoch = self._clock()
                            mixer = TimelineMixer(self.rate, self.channels, epoch,
                                                  late_tolerance_seconds=LATE_TOLERANCE_SECONDS,
                                                  source_count=len(streams))
                            last_safe = last_mic = epoch
                            paused = False
                            self._diagnose('update', phase='resume', source='worker')
                            self._diagnose('event', 'resume')
                    ack.set()
                if not paused:
                    has_mic = collect()
                    now = self._clock()
                    if has_mic:
                        last_mic = now
                    elif now - last_mic > 20_000_000:
                        self._diagnose('update', phase='read', source='microphone',
                                       mic_idle_ms=(now - last_mic) / 10000)
                        raise RuntimeError('Microphone stopped delivering capture packets')
                    self._diagnose('update', mic_idle_ms=(now - last_mic) / 10000)
                    last_safe = now
                    emit(now - PLAYOUT_DELAY_100NS)
                else:
                    # Stopped/reset streams cannot retain preview sound; still
                    # detect removal during pause instead of falsely reporting
                    # a healthy recorder until the next resume.
                    for source in range(len(streams)):
                        stream_call(source, 'read_packets')
                self._wake.wait(.005)
            if not paused:
                for source in range(len(streams)):
                    stream_call(source, 'stop')
                collect()
                emit(self._stop_stamp if self._stop_stamp is not None else self._clock())
        except Exception as exc:
            self._diagnose('event', 'error', error=exc)
            self.failure = exc
            # Flush previously verified packets, never the failed source's
            # unverified current poll, and never repeat a failing callback.
            if active and not paused and last_safe is not None and not callback_failed:
                try:
                    emit(last_safe)
                except Exception as exc:
                    self._diagnose('event', 'error', error=exc)
        finally:
            for source in reversed(range(len(streams))):
                stream = streams[source]
                try:
                    self._diagnose('update', phase='cleanup', source=self._source_names[source])
                    stream.close()
                except Exception as exc:
                    self._diagnose('event', 'error', error=exc)
                    if self.failure is None:
                        self.failure = exc
            if backend is not None:
                try:
                    self._diagnose('update', phase='cleanup', source='backend')
                    backend.close()
                except Exception as exc:
                    self._diagnose('event', 'error', error=exc)
                    if self.failure is None:
                        self.failure = exc
            self._ready.set()
            self._done.set()
            if active and self.failure is not None:
                try:
                    self._diagnose('update', phase='on_error', source='callback')
                    self._on_error(self.failure)
                except Exception as exc:
                    self._diagnose('event', 'error', error=exc)
            self._diagnose('update', phase='stop', source='worker')
            self._diagnose('event', 'stop')
            self._diagnose('close')
