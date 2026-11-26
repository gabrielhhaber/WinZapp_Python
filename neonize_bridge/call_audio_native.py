"""Negotiate native PCM off the event loop, under the media lifecycle gate."""


def served_protocols(calls):
    """Protocols the installed neonize lists; () when it predates the list."""
    listing = getattr(calls, 'audio_protocols', None)
    try:
        return tuple(listing()) if callable(listing) else ()
    except Exception:
        return ()


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
            protocol = data.get('audioProtocol', 1)
            if type(protocol) is not int or protocol not in (1, 2, 3):
                raise ValueError('unsupported audio protocol')
            if protocol == 3 and 3 not in served_protocols(self.calls):
                # A neonize wrapper older than protocol 3 would open protocol 1.
                protocol = 2
            args = (owner['id'], pcm_format['generation'], stream)
            descriptor = opener(*args, protocol=protocol) if protocol >= 2 else opener(*args)
            if descriptor is None:
                return None
            owner.update(media_protocol=1, mic_stream_epoch=stream,
                         native_audio_stream_epoch=stream)
            with self._mic_lock:
                self._mic.clear()
                self._mic_owner = None
            return descriptor
