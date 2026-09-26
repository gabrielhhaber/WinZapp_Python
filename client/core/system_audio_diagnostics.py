"""Private, best-effort capture diagnostics. Never log payloads or free text.

One writer per recorder, lazy account-scoped append, no root logger handlers.
Only lifecycle/error events reach disk; observations stay bounded in memory.
"""
from collections import deque
import json
import logging
from logging.handlers import RotatingFileHandler
import math
from pathlib import Path
import threading
import time

# Exact in-code reasons only. Never stringify an exception or traceback.
_REASONS = {
    'WASAPI capture data discontinuity': 'capture_discontinuity',
    'WASAPI packet timestamp is unreliable': 'unreliable_timestamp',
    'Capture packet arrived after its playback deadline': 'late_packet',
    'Timed out opening both recording devices': 'startup_timeout',
    'Timed out changing capture pause state': 'pause_timeout',
    'Capture worker did not stop before the deadline': 'stop_timeout',
    'A recorder instance can only be started once': 'already_started',
    'Audio endpoint has no friendly name': 'missing_endpoint_name',
    'COM returned a null interface': 'null_interface',
    'Capture packet has an invalid clock interval': 'invalid_clock_interval',
    'Capture packets overlap': 'packet_overlap',
    'Capture requires mono/stereo and a rate from 8000 to 192000 Hz': 'invalid_format',
    'Capture timeline exceeded its bounded buffer': 'timeline_overflow',
    'Capture timestamp went backwards': 'timestamp_backwards',
    'Capture worker missed the bounded buffer deadline': 'worker_deadline',
    'Clock correction exceeded the bounded buffer': 'clock_correction_overflow',
    'Configured microphone name is ambiguous': 'ambiguous_microphone',
    'Configured microphone was not found': 'microphone_not_found',
    'Invalid audio packet': 'invalid_packet',
    'System-audio volume must be finite and between 0 and 100': 'invalid_system_audio_volume',
    'NVDA volume must be finite and between 0 and 100': 'invalid_nvda_volume',
    'Source count must be a positive integer': 'invalid_source_count',
    'Late tolerance must fit inside the bounded buffer': 'invalid_late_tolerance',
    'Invalid or excessive WASAPI packet size': 'invalid_packet_size',
    'Microphone stopped delivering capture packets': 'microphone_stalled',
    'Pause must be requested outside the capture callback': 'pause_from_callback',
    'QueryPerformanceCounter failed': 'qpc_failure',
    'QueryPerformanceFrequency failed': 'qpc_frequency_failure',
    'Recorder has not started': 'not_started',
    'Recording callbacks must be callable': 'invalid_callback',
    'Recording was cancelled during startup': 'startup_cancelled',
    'System-audio recording requires Windows WASAPI': 'unsupported_platform',
    'The pinned audio endpoint is no longer active': 'endpoint_inactive',
    'WASAPI capture queue exceeded its bounded drain': 'capture_queue_overflow',
    'WASAPI returned a null audio buffer': 'null_audio_buffer',
    'Cannot pin NVDA process': 'nvda_pin_failed',
    'NVDA process snapshot failed': 'nvda_snapshot_failed',
    'NVDA process enumeration failed': 'nvda_enumeration_failed',
    'NVDA process session lookup failed': 'nvda_session_failed',
    'NVDA process identity lookup failed': 'nvda_identity_failed',
    'NVDA process image identity lookup failed': 'nvda_image_lookup_failed',
    'NVDA process image identity changed during discovery': 'nvda_image_changed',
    'NVDA process session changed during discovery': 'nvda_session_changed',
    'NVDA process lifetime check failed': 'nvda_lifetime_failed',
    'NVDA process handle cleanup failed': 'nvda_cleanup_failed',
    'NVDA process identity already released': 'nvda_identity_released',
    'NVDA process identity is ambiguous': 'nvda_identity_ambiguous',
    'The pinned NVDA process exited': 'nvda_exited',
    'The pinned NVDA process identity changed': 'nvda_identity_changed',
    'Too many pending process loopback activations': 'process_activation_limit',
    'Process loopback activation cancelled': 'process_activation_cancelled',
    'Process loopback activation returned null operation': 'process_null_operation',
    'Process loopback activation returned null client': 'process_null_client',
    'Process loopback activation timeout': 'process_activation_timeout',
    'Process loopback event creation failed': 'process_event_failed',
    'Process loopback event cleanup failed': 'process_event_cleanup_failed',
    'Process loopback native ownership violation': 'process_ownership_violation',
    'Invalid or excessive process WASAPI packet size': 'process_invalid_packet_size',
    'Unknown process WASAPI packet flags': 'process_unknown_flags',
    'Process WASAPI packet timestamp is unreliable': 'process_unreliable_timestamp',
    'Process WASAPI packet timestamp is nonmonotonic': 'process_timestamp_backwards',
    'Process WASAPI returned a null audio buffer': 'process_null_audio_buffer',
    'Process loopback initialization timeout': 'process_initialization_timeout',
    'Process loopback queue overflow': 'process_queue_overflow',
    'Process loopback drain timeout': 'process_drain_timeout',
    'Process loopback bounded drain exceeded': 'process_drain_overflow',
    'Process loopback command cancelled': 'process_command_cancelled',
    'Process loopback reset requires stopped capture': 'process_reset_running',
    'Process loopback closed': 'process_closed',
    'Process loopback command already pending': 'process_command_pending',
    'Process loopback command acknowledgement timeout': 'process_command_timeout',
    'Process loopback reader cleanup timeout': 'process_cleanup_timeout',
}
_CAPTURE_SOURCES = {'microphone', 'loopback', 'other', 'nvda'}
_SOURCES = {'worker', 'mixer', 'callback', 'backend'} | _CAPTURE_SOURCES
_PHASES = {'startup', 'open', 'start', 'read', 'mix', 'render', 'frames',
           'pause', 'resume', 'stop', 'reset', 'drain', 'cleanup', 'on_error'}
_NUMBERS = {'delivered_frames', 'microphone_packets', 'loopback_packets',
            'microphone_frames', 'loopback_frames', 'poll_gap_ms', 'packet_frames',
            'packet_start_frame', 'position', 'deadline_frames',
            'last_end_frame', 'stamp_delta_ms', 'render_target_frame',
            'callback_ms', 'microphone_read_ms', 'loopback_read_ms', 'mic_idle_ms',
            'microphone_late_packets', 'loopback_late_packets',
            'microphone_late_frames', 'loopback_late_frames',
            'playout_delay_ms', 'late_tolerance_ms'}
_NUMBERS.update(source + suffix for source in _CAPTURE_SOURCES for suffix in
                ('_packets', '_frames', '_read_ms', '_late_packets', '_late_frames'))
_NATIVE_NUMBERS = {'state', 'available', 'flags', 'packet_frames', 'total_frames',
                   'stamp_delta_100ns', 'running', 'loopback', 'seen_packet',
                   'discontinuities', 'queue_peak_frames', 'packets', 'frames',
                   'process_loopback', 'failed', 'queued_frames', 'cleanup_failed',
                   'reader_alive'}


def _numbers(fields):
    return {k: v for k, v in fields.items() if k in _NUMBERS and
            type(v) in (int, float) and math.isfinite(v) and abs(v) < 1e18}


class _QuietRotatingHandler(RotatingFileHandler):
    def handleError(self, record):
        # logging's default implementation prints the record/exception to stderr.
        pass


class CaptureDiagnostics:
    """JSONL lifecycle sink; update/packet are memory-only, close is final.

    A supplied path is for tests/embedding. The default resolves log_path only
    on the first event, after account bootstrap. Missing bootstrap/IO failures
    silently disable that event, never fall back to a global or root log.
    """
    def __init__(self, *, rate, channels, path=None):
        self._path = path
        self._rate, self._channels = rate, channels
        self._handler = None
        self._closed = False
        self._lock = threading.RLock()
        self._started = time.monotonic()
        self._metrics = {source + suffix: 0 for source in _CAPTURE_SOURCES
                         for suffix in ('_packets', '_frames')}
        self._metrics['delivered_frames'] = 0
        self._recent = deque(maxlen=8)
        self._native = {}
        self._phase, self._source = 'startup', 'worker'
        self._failed = False

    def update(self, *, phase=None, source=None, native=None, **fields):
        try:
            with self._lock:
                if phase in _PHASES:
                    self._phase = phase
                if source in _SOURCES:
                    self._source = source
                self._metrics.update(_numbers(fields))
                if source in _CAPTURE_SOURCES and type(native) is dict:
                    self._native[source] = {k: v for k, v in native.items()
                        if k in _NATIVE_NUMBERS and type(v) is int and abs(v) < 1e18}
        except Exception:
            pass

    def packet(self, source, **fields):
        try:
            with self._lock:
                if source not in _CAPTURE_SOURCES:
                    return
                numeric = _numbers(fields)
                self._metrics[source + '_packets'] += 1
                self._metrics[source + '_frames'] += numeric.get('packet_frames', 0)
                self._recent.append(dict(source=source, **numeric))
        except Exception:
            pass

    def event(self, event, *, error=None, reason=None, _context=False):
        try:
            with self._lock:
                if self._closed or event not in {'start', 'opened', 'pause', 'resume', 'stop', 'error'}:
                    return
                # A native ReleaseBuffer/constructor cleanup can mask the packet
                # error without changing the callback's historical exception.
                # Keep its immediate context first, once, without tracebacks/text.
                if error is not None and not self._failed and not _context:
                    prior = error.__context__
                    if prior is not None and prior is not error:
                        self.event('error', error=prior, _context=True)
                record = dict(event=event, rate=self._rate, channels=self._channels,
                              time_unix_ms=time.time_ns() // 1_000_000,
                              elapsed_ms=round((time.monotonic() - self._started) * 1000, 3),
                              phase=self._phase, source=self._source,
                              recent=list(self._recent), native=dict(self._native), **self._metrics)
                if error is not None:
                    kind = type(error).__name__
                    args = error.args
                    reason = ('pause_queue_timeout' if reason == 'pause_queue_timeout' else
                              _REASONS.get(args[0], 'unknown') if args and type(args[0]) is str else 'unknown')
                    record.update(reason=reason, primary=not self._failed, exception_type=kind if kind in {
                        'RuntimeError', 'ValueError', 'OSError', 'TimeoutError', 'WasapiError',
                        'TypeError', 'AttributeError', 'IndexError', 'KeyError', 'BufferError',
                        'OverflowError', 'MemoryError', 'PermissionError', 'FileNotFoundError',
                        'NotImplementedError', 'AssertionError', 'Full'
                    } else 'Exception')
                    hresult = getattr(error, 'hresult', None)
                    if type(hresult) is int and 0 <= hresult <= 0xffffffff:
                        record.update(hresult=f'0x{hresult:08x}', reason='hresult_failure')
                        operation = getattr(error, 'operation', None)
                        if type(operation) is str and operation in {
                            'CoInitializeEx(MTA)', 'MMDeviceEnumerator',
                            *(f'COM method {i}' for i in range(3, 15))
                        }:
                            record['operation'] = operation
                    self._failed = True
                if self._handler is None:
                    path = self._path
                    if path is None:
                        from app_paths import log_path
                        path = log_path('system_audio_capture.log')
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    self._handler = _QuietRotatingHandler(path, mode='a', maxBytes=1024 * 1024,
                                                         backupCount=2, encoding='utf-8')
                self._handler.handle(logging.LogRecord('system_audio_capture', logging.INFO,
                                     '', 0, json.dumps(record, separators=(',', ':')), (), None))
        except Exception:
            pass  # Diagnostics must never affect recording, including path/IO failures.

    def close(self):
        try:
            with self._lock:
                self._closed = True
                if self._handler is not None:
                    self._handler.close()
                    self._handler = None
        except Exception:
            pass
