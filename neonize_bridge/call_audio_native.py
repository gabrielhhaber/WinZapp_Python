"""Negotiate native PCM off the event loop, under the media lifecycle gate."""


class CallNativeAudioMixin:
    def open_audio_transport(self, data):
        with self._lifecycle:
            owner = self._ingress_owner
            if (owner is None or not self._current_owner(owner)
                    or not self._matches_epoch(owner, data)):
                raise LookupError('audio owner not connected')
            stream = data.get('streamEpoch')
            if type(stream) is not int or not 0 < stream < 2**53:
                raise ValueError('invalid audio stream')
            if (stream < owner.get('mic_stream_epoch', 0)
                    or stream <= owner.get('mic_stopped_epoch', 0)
                    or stream <= owner.get('native_audio_stream_epoch', 0)):
                raise ValueError('stale audio stream')
            opener = getattr(self.calls, 'open_audio_transport', None)
            pcm_format = self._format_for(owner)
            if not callable(opener) or pcm_format is None or not pcm_format['generation']:
                return None
            descriptor = opener(owner['id'], pcm_format['generation'], stream)
            if descriptor is None:
                return None
            owner.update(media_protocol=1, mic_stream_epoch=stream,
                         native_audio_stream_epoch=stream)
            with self._mic_lock:
                self._mic.clear()
                self._mic_owner = None
            return descriptor
