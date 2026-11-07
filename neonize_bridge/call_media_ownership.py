"""Call-bound ingress and media controls, separate from native lifecycle."""

import time


class CallIngressMixin:
    def _publish_ingress_owner_locked(self):
        """Publish one record reference while the model lock is held. Readers
        never wait for that lock; workers revalidate all mutable state."""
        active = [c for c in self._calls.values() if c['state'] == 'ACTIVE' and c.get('connected')]
        self._ingress_owner = active[-1] if active and not self._closed else None

    def enqueue_media(self, kind, data=None):
        """Capture the record on arrival, never take the native lifecycle gate
        on aiohttp's thread. Work queues cannot retarget A's packet to B."""
        if (self._closed or kind not in ('mic', 'audio_stop', 'camera', 'camera_stop', 'camera_start')
                or not isinstance(data, (dict, type(None)))):
            return False
        data = {k: v for k, v in (data or {}).items() if k in (
            'pcm', 'jpeg', 'sampleRate', 'encoding', 'mediaProtocol', 'mediaEpoch',
            'streamEpoch', 'epoch', 'native')}
        key = 'pcm' if kind == 'mic' else 'jpeg' if kind == 'camera' else None
        raw = data.get(key, '') if key else ''
        limit = 12800 if kind == 'mic' else 341336
        if key and (not isinstance(raw, (str, bytes, bytearray)) or len(raw) > limit):
            return False
        if isinstance(raw, bytearray):
            raw = data[key] = bytes(raw)
        owner = self._ingress_owner
        if owner is None or not self._matches_epoch(owner, data):
            return False
        # Encoded length is a safe upper bound on decoded bytes, rounded up.
        weight = (len(raw) + 3) // 4 * 3 if isinstance(raw, str) else len(raw)
        epoch = data.get('streamEpoch') if kind in ('mic', 'audio_stop') else data.get('epoch')
        if epoch is not None and (type(epoch) is not int or not 0 < epoch < 2**53):
            return False
        if kind in ('mic', 'audio_stop') and data.get('mediaProtocol') == 1 and epoch is None:
            return False
        if kind == 'camera_start' and data.get('mediaProtocol') == 1 and epoch is None:
            return False
        expires = time.monotonic() + .2 if kind in ('mic', 'camera') else None
        return self._ingress.submit(kind, lambda: self._dispatch_media(kind, data, owner, expires),
                                    weight=weight, owner=owner, epoch=epoch)

    def _matches_epoch(self, call, data):
        if not isinstance(data, dict):
            return False
        if 'mediaProtocol' not in data and 'mediaEpoch' not in data:
            return call.get('media_protocol') != 1  # Explicit legacy compatibility until bound.
        return (type(data.get('mediaProtocol')) is int and data['mediaProtocol'] == 1
                and isinstance(data.get('mediaEpoch'), str)
                and data['mediaEpoch'] == call.get('media_epoch'))

    def _current_owner(self, owner, expires=None):
        with self._lock:
            return (not self._closed and self._calls.get(owner['id']) is owner
                    and owner['state'] == 'ACTIVE' and owner.get('connected')
                    and (expires is None or time.monotonic() <= expires))

    def _dispatch_media(self, kind, data, owner, expires=None):
        if kind in ('mic', 'audio_stop'):
            with self._lifecycle:
                if not self._current_owner(owner, expires):
                    return False
                if kind == 'mic':
                    self.microphone(data)
                else:
                    self.stop_microphone(data)
            return True
        if not self._current_owner(owner, expires):
            return False
        # Never hold the audio lifecycle gate across ffmpeg construction.
        if kind == 'camera':
            self.camera_frame(data, expected=owner)
        elif kind == 'camera_stop':
            self.camera_stop(data, expected=owner)
        elif kind == 'camera_start':
            self.camera_start(data, expected=owner)
        return True

    def close(self):
        self._closed = True
        self._ingress_owner = None
        self._ingress.close()
        with self._lock:
            calls = list(self._calls)
            self._audio_readers.clear()
        if self.video is not None:
            for call_id in calls:
                self.video.end(call_id)

    def camera_frame(self, data, *, expected=None):
        if self.video is not None:
            call = self._media_call(data, expected)
            if call is not None:
                epoch = data.get('epoch') if isinstance(data, dict) else None
                if epoch is not None:
                    if type(epoch) is not int or epoch < call.get('camera_epoch', 0) or epoch <= call.get('camera_stopped_epoch', 0):
                        return
                    if epoch > call.get('camera_epoch', 0):
                        kwargs = {'expected_epoch': call['media_epoch']} if expected is not None else {}
                        self.video.camera_stop(call['id'], False, **kwargs)
                    call['camera_epoch'] = epoch
                kwargs = {'expected_epoch': call['media_epoch']} if expected is not None else {}
                self.video.camera_frame(call['id'], data, **kwargs)

    def camera_stop(self, data=None, *, expected=None):
        if self.video is not None:
            call = self._media_call(data, expected)
            if call is None:
                return
            epoch = data.get('epoch') if isinstance(data, dict) else None
            if epoch is not None:
                if type(epoch) is not int or epoch < call.get('camera_epoch', 0):
                    return
                call['camera_stopped_epoch'] = max(epoch, call.get('camera_stopped_epoch', 0))
            native = isinstance(data, dict) and data.get("native") is True
            kwargs = {'expected_epoch': call['media_epoch']} if expected is not None else {}
            self.video.camera_stop(call['id'], native, **kwargs)

    def camera_start(self, data=None, *, expected=None):
        if self.video is not None:
            call = self._media_call(data, expected)
            if call is not None:
                kwargs = {'expected_epoch': call['media_epoch']} if expected is not None else {}
                epoch = data.get('epoch') if isinstance(data, dict) else None
                if isinstance(data, dict) and data.get('mediaProtocol') == 1 and epoch is None:
                    return False
                if epoch is not None:
                    if (type(epoch) is not int or not 0 < epoch < 2**53
                            or epoch < call.get('camera_epoch', 0)
                            or epoch <= call.get('camera_stopped_epoch', 0)):
                        return False
                    if epoch > call.get('camera_epoch', 0):
                        self.video.camera_stop(call['id'], False, **kwargs)
                        call['camera_epoch'] = epoch
                self.video.camera_start(call['id'], **kwargs)

    def _media_call(self, data, expected=None):
        with self._lock:
            active = [c for c in self._calls.values() if c['state'] == 'ACTIVE' and c.get('connected')]
            call = active[-1] if active else None
        return call if (call is not None and (expected is None or call is expected)
                        and self._matches_epoch(call, data or {}) and not self._closed) else None
