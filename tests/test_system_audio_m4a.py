"""Mixed-recording quality/ownership contracts; no GUI, capture or network."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('audio_transcode', ROOT / 'client/core/audio_transcode.py')
transcode = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transcode)


class TestMixedEncoder(unittest.TestCase):
    def test_encoder_requests_stereo_48k_aac_lc_192k_m4a(self):
        encode = getattr(transcode, 'encode_system_audio_to_m4a', None)
        self.assertTrue(callable(encode), 'Missing mixed-recording AAC encoder')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'capture.wav'
            source.write_bytes(b'original PCM source')
            ffmpeg = Path(directory) / 'ffmpeg.exe'
            ffmpeg.touch()
            def run(command, **kwargs):
                self.assertEqual(command[command.index('-i') + 1], str(source))
                for flag, value in [('-ac', '2'), ('-ar', '48000'), ('-c:a', 'aac'),
                                    ('-profile:a', 'aac_low'), ('-b:a', '192k')]:
                    self.assertEqual(command[command.index(flag) + 1], value)
                self.assertTrue(command[-1].endswith('.m4a'))
                Path(command[-1]).write_bytes(b'\x00\x00\x00\x20ftypM4A ' + b'x' * 64)
                return SimpleNamespace(returncode=0, stderr=b'')
            with patch.object(transcode.subprocess, 'run', side_effect=run) as called:
                result = encode(str(ffmpeg), str(source))
            try:
                self.assertTrue(Path(result).is_file())
                self.assertEqual(source.read_bytes(), b'original PCM source')
                called.assert_called_once()
            finally:
                if result:
                    os.unlink(result)
    def test_failed_encoder_cleans_partial_output_and_never_touches_source(self):
        encode = transcode.encode_system_audio_to_m4a
        for outcome in ['exit', 'timeout', 'exception', 'empty', 'missing']:
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / 'capture.wav'
                source.write_bytes(b'PCM remains')
                ffmpeg = Path(directory) / 'ffmpeg.exe'
                ffmpeg.touch()
                outputs = []
                def run(command, **kwargs):
                    output = Path(command[-1])
                    outputs.append(output)
                    output.write_bytes(b'partial')
                    if outcome == 'timeout':
                        raise subprocess.TimeoutExpired(command, 1800)
                    if outcome == 'exception':
                        raise OSError('cannot execute')
                    if outcome == 'empty': output.write_bytes(b'')
                    if outcome == 'missing': output.unlink()
                    return SimpleNamespace(returncode=1 if outcome == 'exit' else 0,
                                           stderr=b'encoder unavailable')
                with patch.object(transcode.subprocess, 'run', side_effect=run):
                    self.assertIsNone(encode(str(ffmpeg), str(source)))
                self.assertEqual(source.read_bytes(), b'PCM remains')
                self.assertTrue(outputs)
                self.assertFalse(outputs[0].exists())
    def test_m4a_passes_through_with_canonical_mime_even_with_windows_registry_override(self):
        with patch.object(transcode.mimetypes, 'guess_type', return_value=('audio/x-m4a', None)), \
             patch.object(transcode.subprocess, 'run') as run:
            result = transcode.prepare_audio_for_whatsapp('', 'mixed.M4A')
        self.assertEqual(result, ('mixed.M4A', 'audio/mp4'))
        run.assert_not_called()
    def test_native_bundled_ffmpeg_preserves_distinct_stereo_tones(self):
        import array
        import math
        import sys
        import wave
        ffmpeg = ROOT / 'client/api/node_modules/@ffmpeg-installer/win32-x64/ffmpeg.exe'
        if sys.platform != 'win32' or not ffmpeg.is_file():
            self.skipTest('native Windows bundled ffmpeg required; no capture is used')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'synthetic.wav'
            rate = 48000
            samples = array.array('h')
            for n in range(rate * 2):
                samples.extend((round(10000 * math.sin(2 * math.pi * 440 * n / rate)),
                                round(10000 * math.sin(2 * math.pi * 880 * n / rate))))
            with wave.open(str(source), 'wb') as wav:
                wav.setnchannels(2)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                wav.writeframes(samples.tobytes())
            output = transcode.encode_system_audio_to_m4a(str(ffmpeg), str(source))
            self.assertIsNotNone(output)
            try:
                result = subprocess.run([str(ffmpeg), '-i', output, '-f', 's16le', '-c:a', 'pcm_s16le', '-'],
                                        capture_output=True, timeout=60,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
                self.assertRegex(result.stderr.decode(errors='replace'), r'Audio: aac \(LC\).*48000 Hz, stereo')
                decoded = array.array('h')
                decoded.frombytes(result.stdout)
                self.assertGreaterEqual(len(decoded), rate * 2 * 2)
                def amplitude(channel, hz):
                    data = decoded[channel::2][rate // 2:rate + rate // 2]
                    sine = sum(x * math.sin(2 * math.pi * hz * i / rate) for i, x in enumerate(data))
                    cosine = sum(x * math.cos(2 * math.pi * hz * i / rate) for i, x in enumerate(data))
                    return 2 * math.hypot(sine, cosine) / len(data)
                for channel, wanted, other in [(0, 440, 880), (1, 880, 440)]:
                    self.assertGreater(amplitude(channel, wanted), 5000)
                    self.assertLess(amplitude(channel, other), amplitude(channel, wanted) * .02)
            finally:
                if output: os.unlink(output)


class TestOwnedMediaQueue(unittest.TestCase):
    def test_recorded_media_uses_attachment_sender_but_recording_cache_callback(self):
        import sys
        import threading
        fake_wx = SimpleNamespace(CallAfter=lambda fn, *args: fn(*args))
        spec = importlib.util.spec_from_file_location('mixed_test_queue', ROOT / 'client/core/message_queue.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'wx': fake_wx}):
            spec.loader.exec_module(module)
        with patch.object(module.threading, 'Thread'):
            mw = SimpleNamespace(offline_mode=False, _wa_connected=True,
                _own_sent_ids_lock=threading.Lock(), _own_sent_ids=set(),
                send_media_attachment=Mock(return_value='real-id'),
                send_audio_message=Mock(), _on_message_sent=Mock())
            queue = module.MessageQueue(mw)
        queue._RETRY_INTERVAL = 0
        mw._on_message_sent.side_effect = lambda *args: queue._stop.set()
        self.assertIn('owns_media_path', __import__('inspect').signature(module.PendingMessage).parameters,
                      'Recorded M4A needs explicit temporary-file ownership')
        pm = module.PendingMessage('local', 'chat', media_path='recording.m4a',
                                   media_type='audio', owns_media_path=True)
        queue.enqueue(pm)
        queue._run(True)
        mw.send_audio_message.assert_not_called()
        mw.send_media_attachment.assert_called_once()
        self.assertEqual(mw.send_media_attachment.call_args.args[:3],
                         ('chat', 'recording.m4a', 'audio'))
        mw._on_message_sent.assert_called_once_with('local', 'recording.m4a', 'real-id', 'chat', False)
        # User-selected attachments remain owned by their user, not the queue.
        attachment = module.PendingMessage('other', 'chat', media_path='user.m4a', media_type='audio')
        self.assertIsNone(attachment.recording_path)
    def queue_harness(self):
        import sys
        import threading
        from tests.test_system_audio_ui import functions
        fake_wx = SimpleNamespace(CallAfter=lambda fn, *args: fn(*args))
        spec = importlib.util.spec_from_file_location('mixed_cleanup_queue', ROOT / 'client/core/message_queue.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'wx': fake_wx}): spec.loader.exec_module(module)
        mw = SimpleNamespace(offline_mode=False, _wa_connected=True,
                             _own_sent_ids_lock=threading.Lock(), _own_sent_ids=set(),
                             _on_message_failed=Mock(), _on_message_unconfirmed=Mock(),
                             _on_message_sent=Mock(), send_audio_message=Mock())
        cleanup = functions(ROOT / 'client/main.py', ['_discard_temp_recording'], {'os': os})['_discard_temp_recording']
        mw._on_cancelled_message_dropped = Mock(side_effect=lambda local, path: cleanup(path))
        mw._on_cancelled_message_delivered = Mock(side_effect=lambda local, real, jid, path, *rest: cleanup(path))
        with patch.object(module.threading, 'Thread'):
            queue = module.MessageQueue(mw)
        queue._RETRY_INTERVAL = 0
        mw._on_message_failed.side_effect = lambda *args: queue._stop.set()
        mw._on_message_unconfirmed.side_effect = lambda *args: queue._stop.set()
        mw._on_message_sent.side_effect = lambda *args: queue._stop.set()
        return module, queue, mw

    def test_queued_cancel_disposes_only_explicitly_owned_media(self):
        for owned in [False, True]:
            with self.subTest(owned=owned), tempfile.TemporaryDirectory() as directory:
                module, queue, mw = self.queue_harness()
                path = Path(directory) / 'audio.m4a'
                path.write_bytes(b'keep user attachment')
                pm = module.PendingMessage('local', 'chat', media_path=str(path),
                                           media_type='audio', owns_media_path=owned)
                queue.enqueue(pm)
                self.assertTrue(queue.cancel('local'))
                self.assertEqual(path.exists(), not owned)

    def test_inflight_cancel_routes_owned_audio_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            module, queue, mw = self.queue_harness()
            path = Path(directory) / 'audio.m4a'
            path.write_bytes(b'owned recording')
            pm = module.PendingMessage('local', 'chat', media_path=str(path),
                                       media_type='audio', owns_media_path=True)
            queue.enqueue(pm)
            def send(*args, **kwargs):
                self.assertFalse(queue.cancel('local'))
                queue._stop.set()
                return 'delivered'
            mw.send_media_attachment = Mock(side_effect=send)
            queue._run(True)
            self.assertFalse(path.exists())
            mw._on_cancelled_message_delivered.assert_called_once_with(
                'local', 'delivered', 'chat', str(path), False, False)
            mw._on_message_sent.assert_not_called()

    def test_retry_keeps_m4a_until_terminal_failure_or_unknown_outcome(self):
        for outcome in ['failure', 'ambiguous', 'exception']:
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                module, queue, mw = self.queue_harness()
                path = Path(directory) / 'audio.m4a'
                path.write_bytes(b'retry same AAC bytes')
                pm = module.PendingMessage('local', 'chat', media_path=str(path),
                                           media_type='audio', owns_media_path=True)
                queue.enqueue(pm)
                attempts = []
                def send(*args, **kwargs):
                    attempts.append(path.read_bytes())
                    if len(attempts) == 1:
                        return {'ok': False, 'retry': True, 'error': 'transient'}
                    if outcome == 'exception': raise RuntimeError('transport stub')
                    return {'ok': False, 'retry': False, 'ambiguous': outcome == 'ambiguous'}
                mw.send_media_attachment = Mock(side_effect=send)
                queue._run(True)
                self.assertGreaterEqual(len(attempts), 2)
                self.assertFalse(path.exists(), 'terminal outcome must release plaintext M4A')
                if outcome == 'ambiguous': mw._on_message_unconfirmed.assert_called_once()
                else: mw._on_message_failed.assert_called_once()


class TestMixedComposer(unittest.TestCase):
    def setUp(self):
        import logging
        import sys
        import time
        import uuid
        import wave
        from cryptography.fernet import Fernet
        from tests.test_system_audio_ui import bind, functions, QueuedThreads
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.threads, self.ui = QueuedThreads(), []
        self.key = Fernet.generate_key()
        self.decrypt = Fernet(self.key).decrypt
        self.encoded = b'\x00\x00\x00\x20ftypM4A ' + b'encoded stereo' * 10
        self.written_wavs = []
        self.m4a_paths = []
        self.encoder = Mock(side_effect=self.encode)
        self.gate = Mock(side_effect=lambda data, *args: data)
        fake_wx = SimpleNamespace(CallAfter=lambda fn, *args: self.ui.append((fn, args)))
        spec = importlib.util.spec_from_file_location('mixed_composer_queue', ROOT / 'client/core/message_queue.py')
        queue_module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'wx': fake_wx}): spec.loader.exec_module(queue_module)
        self.modules = patch.dict(sys.modules, {
            'core.audio_transcode': SimpleNamespace(encode_system_audio_to_m4a=self.encoder),
            'core.audio_processing': SimpleNamespace(apply_noise_gate=self.gate),
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)
        mw = SimpleNamespace(
            key=self.key, settings={'general': {'noise_reduction_enabled': True}},
            i18n=SimpleNamespace(t=lambda key: key),
            voicemsg_send_sound=SimpleNamespace(play=Mock()),
            send_recording_status=Mock(), _schedule_set_chats=Mock(),
            _find_api_ffmpeg=Mock(return_value='bundled-ffmpeg'),
            _convert_wav_to_ogg=Mock(return_value=None),
            message_queue=SimpleNamespace(enqueue=Mock()),
            mark_conversation_as_read=Mock(), _on_message_failed=Mock(),
            _on_cancelled_message_dropped=Mock(),
        )
        self.p = SimpleNamespace(
            main_window=mw, _is_recording=True, _recording_system_audio=True,
            _system_audio_session={'stopped': True}, _recording_paused=False,
            _recording_frames=[b'\x01\x00\x02\x00' * 480],
            _recording_actual_rate=48000, _recording_actual_ch=2,
            _recording_stereo=True, _quoted_message={'key': {'id': 'quoted'}},
            conversation={'remoteJid': 'first-chat'}, _sorted_messages=[],
            _outgoing_virtual_messages={}, _cancelled_pending_messages={},
            _stop_system_audio_recording=Mock(), _stop_recording_stream=Mock(),
            _hide_voice_panel=Mock(), _clear_empty_placeholder=Mock(),
            _render_message_line=lambda msg: 'unchanged UI',
            messages_list=SimpleNamespace(Append=Mock(), GetItemCount=lambda: 1, EnsureVisible=Mock()),
            message_field=SimpleNamespace(SetFocus=Mock()), _on_cancel_reply=Mock(),
        )
        self.p._register_virtual_msg = lambda msg: self.p._outgoing_virtual_messages.update({msg['_local_id']: msg})
        self.p._is_cancelled_pending = lambda local: local in self.p._cancelled_pending_messages
        self.p._stop_system_audio_recording.side_effect = lambda: setattr(self.p, '_recording_system_audio', False)
        encrypt = functions(ROOT / 'client/core/utils.py', ['encrypt'], {'Fernet': Fernet})['encrypt']
        namespace = dict(logging=logging, os=os, tempfile=tempfile, time=time, uuid=uuid,
                         wave=wave, threading=self.threads, wx=fake_wx,
                         encode_as_stereo=lambda wanted, channels: wanted and channels == 2,
                         encrypt=encrypt, data_path=lambda name: str(self.folder / name),
                         PendingMessage=queue_module.PendingMessage)
        # Bind the actual send handler; no wx/App or capture APIs are imported.
        bind(self.p, ['_send_voice_message'], namespace)
        # New helper is bound when present so RED tests reach old behavior first.
        import ast
        tree = ast.parse((ROOT / 'client/ui/conversations.py').read_text(encoding='utf-8'))
        if any(isinstance(n, ast.FunctionDef) and n.name == '_enqueue_system_audio_file' for n in ast.walk(tree)):
            bind(self.p, ['_enqueue_system_audio_file'], namespace)

    def encode(self, ffmpeg, wav_path):
        import wave
        self.written_wavs.append(wav_path)
        with wave.open(wav_path, 'rb') as wav:
            self.assertEqual((wav.getnchannels(), wav.getframerate()), (2, 48000))
            self.assertEqual(wav.readframes(wav.getnframes()), b'\x01\x00\x02\x00' * 480)
        output = self.folder / 'recording.m4a'
        output.write_bytes(self.encoded)
        self.m4a_paths.append(output)
        return str(output)

    def drain_ui(self):
        while self.ui:
            fn, args = self.ui.pop(0)
            fn(*args)

    def test_mixed_send_is_ordinary_audio_and_bypasses_voice_gate_and_opus(self):
        self.p._send_voice_message(None)
        msg = self.p._sorted_messages[0]
        self.assertIs(msg['message']['audioMessage']['ptt'], False)
        self.assertEqual(msg['message']['audioMessage']['mimetype'], 'audio/mp4')
        self.assertTrue(msg['message']['audioMessage']['fileName'].endswith('.m4a'))
        # The worker must retain the first chat/quote even after navigation.
        self.p.conversation = {'remoteJid': 'new-chat'}
        self.p._recording_frames = [b'next recording']
        self.threads.drain()
        self.drain_ui()
        self.gate.assert_not_called()
        self.p.main_window._convert_wav_to_ogg.assert_not_called()
        pm = self.p.main_window.message_queue.enqueue.call_args.args[0]
        self.assertIsNone(pm.audio_path)
        self.assertIsNone(pm.ogg_bytes)
        self.assertEqual((pm.media_path, pm.media_type, pm.jid),
                         (str(self.m4a_paths[0]), 'audio', 'first-chat'))
        self.assertTrue(pm.owns_media_path)
        self.assertEqual(pm.quoted['key']['id'], 'quoted')
        cache = self.folder / 'voice_messages' / (pm.local_id + '.msv')
        self.assertEqual(self.decrypt(cache.read_bytes()), self.encoded)
        self.assertTrue(self.m4a_paths[0].exists(), 'retry source must survive enqueue')
        self.assertFalse(Path(self.written_wavs[0]).exists())
    def test_aac_failure_is_visible_and_never_falls_back_to_ogg(self):
        self.encoder.side_effect = None
        self.encoder.return_value = None
        self.p._send_voice_message(None)
        local_id = self.p._sorted_messages[0]['_local_id']
        self.threads.drain()
        self.drain_ui()
        self.p.main_window.message_queue.enqueue.assert_not_called()
        self.p.main_window._convert_wav_to_ogg.assert_not_called()
        self.p.main_window._on_message_failed.assert_called_once_with(
            local_id, 'media_audio_convert_failed', True)
        wav_path = self.encoder.call_args.args[1]
        self.assertFalse(Path(wav_path).exists())

    def test_cache_failure_is_visible_and_removes_plaintext_outputs(self):
        (self.folder / 'voice_messages').write_bytes(b'blocks directory creation')
        self.p._send_voice_message(None)
        self.threads.drain()
        self.drain_ui()
        self.p.main_window.message_queue.enqueue.assert_not_called()
        self.p.main_window._on_message_failed.assert_called_once()
        self.assertFalse(Path(self.written_wavs[0]).exists())
        self.assertFalse(self.m4a_paths[0].exists())

    def test_wav_write_failure_is_visible_and_cleans_partial_wav(self):
        self.p._send_voice_message(None)
        created = []
        original = tempfile.NamedTemporaryFile
        def named(**kwargs):
            result = original(dir=self.folder, **kwargs)
            created.append(result.name)
            return result
        with patch.object(tempfile, 'NamedTemporaryFile', side_effect=named), \
             patch('wave.open', side_effect=OSError('disk full')):
            self.threads.drain()
        self.drain_ui()
        self.p.main_window._on_message_failed.assert_called_once()
        self.p.main_window.message_queue.enqueue.assert_not_called()
        self.assertTrue(created)
        self.assertFalse(Path(created[0]).exists())
    def test_cancel_during_encoding_cannot_resurrect_send(self):
        self.p._send_voice_message(None)
        msg = self.p._sorted_messages[0]
        local_id = msg['_local_id']
        # Same markers _cancel_pending_message leaves when no queue worker yet
        # owns the recording (encoding is still in progress).
        self.p._cancelled_pending_messages[local_id] = msg
        self.p._outgoing_virtual_messages.pop(local_id)
        msg['_cancelled_awaiting_id'] = True
        self.threads.drain()
        self.drain_ui()
        self.p.main_window.message_queue.enqueue.assert_not_called()
        self.p.main_window._on_cancelled_message_dropped.assert_called_once_with(local_id)
        self.assertFalse(self.m4a_paths[0].exists())
        self.assertFalse((self.folder / 'voice_messages' / (local_id + '.msv')).exists())

    def test_ordinary_voice_remains_ptt_with_noise_gate_and_existing_ogg_encoder(self):
        self.p._recording_system_audio = False
        self.p._system_audio_session = None
        self.p._recording_actual_ch = 1
        self.p._recording_stereo = False
        self.p._send_voice_message(None)
        self.threads.drain()
        self.drain_ui()
        self.encoder.assert_not_called()
        self.gate.assert_called_once()
        self.p.main_window._convert_wav_to_ogg.assert_called_once()
        pm = self.p.main_window.message_queue.enqueue.call_args.args[0]
        self.assertIsNone(pm.media_path)
        self.assertFalse(pm.stereo)
        self.assertIs(self.p._sorted_messages[0]['message']['audioMessage']['ptt'], True)
        self.assertTrue(Path(pm.audio_path).exists())
        os.unlink(pm.audio_path)
    def test_sent_callback_promotes_m4a_cache_even_after_leaving_chat(self):
        import logging
        from tests.test_system_audio_ui import functions
        self.p._send_voice_message(None)
        self.threads.drain()
        self.drain_ui()
        pm = self.p.main_window.message_queue.enqueue.call_args.args[0]
        mw = self.p.main_window
        mw._is_cancelled_send = lambda local: False
        mw.db = SimpleNamespace(update_message_id=Mock())
        mw.message_sent_sound = SimpleNamespace(play=Mock())
        # No row from the originating chat is visible any more.
        mw.conversations_panel = SimpleNamespace(_mark_message_sent=Mock())
        namespace = dict(logging=logging, os=os, threading=self.threads,
                         data_path=lambda name: str(self.folder / name))
        sent = functions(ROOT / 'client/main.py', ['_on_message_sent'], namespace)['_on_message_sent']
        sent(mw, pm.local_id, pm.recording_path, 'real-id', pm.jid, False)
        self.threads.drain()
        real_cache = self.folder / 'voice_messages' / 'real-id.msv'
        self.assertEqual(self.decrypt(real_cache.read_bytes()), self.encoded)
        self.assertFalse(Path(pm.media_path).exists())
        mw.message_sent_sound.play.assert_called_once()
        mw.conversations_panel._mark_message_sent.assert_called_once_with(
            pm.local_id, real_id='real-id', quote_lost=False)
        mw.db.update_message_id.assert_called_once_with(pm.jid, pm.local_id, 'real-id')

    def test_encrypted_m4a_uses_existing_offline_playback_decoder(self):
        import logging
        from tests.test_system_audio_ui import bind
        self.p._send_voice_message(None)
        self.threads.drain()
        self.drain_ui()
        pm = self.p.main_window.message_queue.enqueue.call_args.args[0]
        cache = self.folder / 'voice_messages' / (pm.local_id + '.msv')
        decoded = self.folder / 'playback.wav'
        decoded.write_bytes(b'RIFF' + b'x' * 64)
        seen = []
        def decode(ffmpeg, path):
            seen.append(Path(path))
            self.assertTrue(path.endswith('.m4a'))
            self.assertEqual(Path(path).read_bytes(), self.encoded)
            return str(decoded)
        stream = SimpleNamespace(play=Mock())
        p = self.p
        p._audio_stream = None
        p._audio_positions = {}
        p._audio_timer = SimpleNamespace(Start=Mock())
        p._audio_speed_steps = [1.0]
        p._audio_speed_index = 0
        p._focused_msg_id = lambda: None
        p._stop_audio = Mock()
        p._open_audio_stream_from_temp_file = Mock(return_value=(stream, None))
        bind(p, ['_play_audio'], dict(os=os, tempfile=tempfile, logging=logging,
             decrypt_bytes=lambda data, key: self.decrypt(data), transcode_audio_to_wav=decode))
        p._play_audio(pm.local_id, 1, str(cache))
        self.assertEqual(p._audio_temp_file, str(decoded))
        self.assertFalse(seen[0].exists(), 'decrypted M4A must be removed after WAV decoding')
        stream.play.assert_called_once()  # stub only: never opens BASS/device
        p._stop_audio.assert_not_called()

    def test_real_attachment_pipeline_uploads_unchanged_m4a_as_audio_not_document_or_ptt(self):
        import logging
        import sys
        from tests.test_system_audio_ui import functions
        self.p._send_voice_message(None)
        self.threads.drain()
        self.drain_ui()
        pm = self.p.main_window.message_queue.enqueue.call_args.args[0]
        mw = self.p.main_window
        mw._resolve_jid_for_send = lambda jid: jid
        mw.wpp_server, mw.wpp_port, mw.token = 'http://not-used.invalid', 1, 'fake'
        mw._serialize_quoted_id = lambda *args, **kwargs: 'quoted-id'
        modules = {'core.audio_transcode': transcode,
                   'core.message_queue': SimpleNamespace(MessageCancelled=type('MessageCancelled', (Exception,), {}))}
        for name in ['video_transcode', 'multipart_stream', 'send_contract']:
            spec = importlib.util.spec_from_file_location(name, ROOT / 'client/core' / (name + '.py'))
            module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, modules): spec.loader.exec_module(module)
            modules['core.' + name] = module
        payloads = []
        def post(url, **kwargs):
            payloads.append(b''.join(kwargs['data']))
            return SimpleNamespace(status_code=201, json=lambda: {'status': 'success', 'response': {'id': 'real-id', 'ack': 1}})
        ns = dict(logging=logging, os=os, api_post=post,
                  accepted_message_id=modules['core.send_contract'].accepted_message_id,
                  MessageCancelled=modules['core.message_queue'].MessageCancelled)
        send = functions(ROOT / 'client/main.py', ['send_media_attachment'], ns)['send_media_attachment']
        with patch.dict(sys.modules, modules), patch.object(transcode.subprocess, 'run') as convert:
            result = send(mw, pm.jid, pm.media_path, pm.media_type, quoted=pm.quoted)
        self.assertEqual(result, 'real-id')
        convert.assert_not_called()
        self.assertIn(b'Content-Type: audio/mp4', payloads[0])
        self.assertIn(b'name="type"\r\n\r\naudio\r\n', payloads[0])
        self.assertIn(b'.m4a"', payloads[0])
        self.assertIn(self.encoded, payloads[0])
        self.assertNotIn(b'ptt', payloads[0])
        self.assertTrue(Path(pm.media_path).exists(), 'send callback, not sender, owns cleanup')


if __name__ == '__main__':
    unittest.main()
