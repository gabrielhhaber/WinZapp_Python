"""Optional Neonize PCM over authenticated loopback TCP; Socket.IO fallback."""
import logging
import queue
import socket
import struct
import threading
import time

from core.call_media_ownership import media_fields, valid_media_epoch


def read_exact(connection, size):
    result = bytearray()
    while len(result) < size:
        chunk = connection.recv(size - len(result))
        if not chunk:
            raise EOFError('native audio disconnected')
        result.extend(chunk)
    return bytes(result)


def validate_descriptor(value):
    if (not isinstance(value, dict) or type(value.get('protocol')) is not int
            or value['protocol'] not in (1, 2) or type(value.get('port')) is not int
            or not 0 < value['port'] < 65536
            or type(value.get('sampleRate')) is not int or value['sampleRate'] not in (16000, 48000)
            or type(value.get('generation')) is not int or not 0 < value['generation'] < 2**64
            or not isinstance(value.get('secret'), str) or len(value['secret']) != 64):
        raise ValueError('invalid native audio descriptor')
    secret = bytes.fromhex(value['secret'])
    if len(secret) != 32:
        raise ValueError('invalid native audio secret')
    return value['port'], secret, value['sampleRate'], value['generation']


class NeonizeCallAudioTransport:
    def __init__(self, fallback, negotiate):
        self._fallback = fallback
        self._negotiate = negotiate
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._queue = queue.Queue(maxsize=3)  # at most 60 ms capture
        self._epoch = ''
        self._started = False
        self._active = False
        self._conn = None
        self._thread = None
        self._fallback_requested = False

    def configure_playout(self, enabled):
        connection = None
        with self._lock:
            lost_timed_playout = getattr(self, '_timed', False) and not enabled
            self._timed = bool(enabled)
            if lost_timed_playout:
                # A shared ringing stream can become an exclusive stream at
                # an unsupported rate on answer. Release protocol 2 so Go
                # resumes codec PLC; do not send the bridge-stop control.
                self._fallback_requested = True
                connection = self._conn
        if connection is not None:
            self._disconnect(connection)

    def fallback(self):
        with self._lock:
            self._fallback_requested = True
            connection = self._conn
        if connection is not None:
            self._disconnect(connection)

    def configure(self, receiver, stream_epoch):
        self._receiver = receiver
        self._stream = stream_epoch

    @property
    def receiving_native(self):
        with self._lock:
            return self._active

    def bind_owner(self, epoch):
        with self._lock:
            if not valid_media_epoch(epoch) or (self._epoch and self._epoch != epoch):
                return
            self._epoch = epoch
            self._launch_locked()

    def start(self):
        self._fallback.start()
        with self._lock:
            self._started = True
            self._launch_locked()

    def _launch_locked(self):
        if self._started and self._epoch and self._thread is None and not self._closed.is_set():
            self._thread = threading.Thread(target=self._run, name='call-native-audio', daemon=True)
            self._thread.start()

    def send_microphone(self, pcm, *, sample_rate, media_epoch, stream_epoch):
        with self._lock:
            if self._closed.is_set():
                return
            active = self._active
        if not active:
            self._fallback.send_microphone(pcm, sample_rate=sample_rate,
                                          media_epoch=media_epoch, stream_epoch=stream_epoch)
            return
        if sample_rate != 48000 or len(pcm) != 1920 or media_epoch != self._epoch or stream_epoch != self._stream:
            return
        item = time.monotonic(), bytes(pcm)
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                pass

    def _run(self):
        connection = None
        writer = None
        disconnected = threading.Event()
        try:
            # Call ownership can arrive while the outgoing call is still ringing.
            while not self._closed.is_set() and not self._fallback_requested:
                body = media_fields(self._epoch, self._stream)
                if getattr(self, '_timed', False):
                    body['audioProtocol'] = 2
                response = self._negotiate(body)
                if response.status_code != 409:
                    break
                self._closed.wait(.5)
            if self._closed.is_set() or self._fallback_requested:
                return
            if response.status_code != 200:
                logging.info('[call_audio] native transport unsupported/unavailable; using Socket.IO')
                return
            descriptor = response.json().get('response')
            port, secret, rate, generation = validate_descriptor(descriptor)
            protocol = descriptor['protocol']
            header_size = 28 if protocol == 2 else 12
            connection = socket.create_connection(('127.0.0.1', port), timeout=1)
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                if self._closed.is_set() or self._fallback_requested:
                    return
                self._conn = connection
            connection.sendall(secret)
            if read_exact(connection, 4) != (b'NZA2' if protocol == 2 else b'NZA1'):
                raise ValueError('invalid native audio handshake')
            with self._lock:
                if self._closed.is_set() or self._fallback_requested:
                    return
                self._active = True
            logging.info('[call_audio] transport=native')
            writer = threading.Thread(target=self._write, args=(connection, disconnected),
                                      name='call-native-microphone', daemon=True)
            writer.start()
            while not self._closed.is_set():
                size, = struct.unpack('<I', read_exact(connection, 4))
                if not header_size <= size <= header_size + rate * 60 // 1000 * 2 or size % 2:
                    raise ValueError('invalid native audio frame size')
                packet = read_exact(connection, size)
                if struct.unpack_from('<IQ', packet) != (rate, generation):
                    raise ValueError('native audio owner changed')
                if len(packet) > header_size and not self._closed.is_set():
                    if protocol == 2:
                        timestamp, ssrc, clock, flags = struct.unpack_from('<IIII', packet, 12)
                        if flags not in (0,1) or (flags == 1 and clock not in (16000,48000)):
                            raise ValueError('invalid native timeline')
                        if flags == 1:
                            self._receiver(packet[header_size:], rate, media_epoch=self._epoch,
                                           timeline=(timestamp, ssrc, clock))
                            continue
                    self._receiver(packet[header_size:], rate, media_epoch=self._epoch)
        except (OSError, EOFError, ValueError, TypeError, AttributeError):
            # Descriptors contain a credential: never log the response/exception.
            logging.info('[call_audio] native transport unavailable; using Socket.IO')
        except Exception:
            logging.warning('[call_audio] native transport failed; using Socket.IO')
        finally:
            disconnected.set()
            if connection is not None:
                self._disconnect(connection)
            with self._lock:
                self._active = False
                self._conn = None
            if writer is not None:
                writer.join(1)

    def _write(self, connection, disconnected):
        try:
            while not self._closed.is_set() and not disconnected.is_set():
                try:
                    created, pcm = self._queue.get(timeout=.5)
                    if time.monotonic() - created > .2:
                        continue
                except queue.Empty:
                    pcm = b''  # heartbeat, also for a receive-only monitor
                if self._closed.is_set() or disconnected.is_set():
                    return
                connection.sendall(struct.pack('<I', len(pcm)) + pcm)
        except OSError:
            self._disconnect(connection)

    @staticmethod
    def _disconnect(connection):
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection.close()

    def close(self):
        self._closed.set()
        with self._lock:
            self._active = False
            connection = self._conn
        if connection is not None:
            self._disconnect(connection)

    def stop(self, *, media_epoch, stream_epoch):
        self.close()
        self._fallback.stop(media_epoch=media_epoch, stream_epoch=stream_epoch)
