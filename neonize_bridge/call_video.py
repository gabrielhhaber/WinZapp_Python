"""The video of a session's calls: the client's camera to the other side and
the other side's video to the client, through video_codec.py and the DLL's
video functions (neonize_patches/neonize/0008, neonize.calls).

The client speaks to the bridge as to WPPConnect (core/websocket_client.py,
api_patches/src/util/callMediaBridge.ts):

- `call:video:camera` {jpeg (base64), epoch}: one camera frame, 10 a second,
  sent while the call is a video call and the camera is on;
- `call:video:camera:stop` {native}: stop sending; `native` when the USER
  turned the camera off, which tells WhatsApp too (camera state 0); without
  it (the call ended, the capture was discarded) only the frames stop;
- `call:video:camera:start`: the user turned the camera back on (state 1);
- `call:video:remote` {session, jpeg (base64)}: one frame of the other side.

An encoder per call starts with the first camera frame; meowcaller holds our
video back until the other side has acknowledged it, and refuses the access
units until then (dropped here). The other side's units are read on one
thread per call from the moment it is connected and decoded once the first
arrives. Everything of a call is closed when it ends (`end`).
"""
import base64
import logging
import threading

import video_codec

log = logging.getLogger("bridge")

VIDEO_REQUEST, VIDEO_ACCEPT, VIDEO_STOP, VIDEO_ENABLE, VIDEO_DISABLE = 1, 2, 3, 4, 5


def camera_jpeg(data):
    """The JPEG of a `call:video:camera`, or None when it is not one."""
    if not isinstance(data, dict):
        return None
    raw = data.get("jpeg")
    if isinstance(raw, str):
        try:
            raw = base64.b64decode(raw, validate=True)
        except (ValueError, TypeError):
            return None
    if not isinstance(raw, (bytes, bytearray)) or not raw.startswith(b"\xff\xd8"):
        return None
    return bytes(raw) if len(raw) <= video_codec.MAX_JPEG else None


class CallVideo:
    """`calls`: neonize.calls.Calls (or a fake); `codec`: what makes encoders
    and decoders (video_codec.FfmpegCodec); `emit(event, data)` to the
    session's clients."""

    def __init__(self, name, calls, emit, codec):
        self.name, self.calls, self.emit, self.codec = name, calls, emit, codec
        self._lock = threading.RLock()
        self._encoders = {}
        self._encoder_claims = {}
        self._decoders = {}
        self._owners = {}
        self._wire_epochs = {}
        self._receiving = {}

    # ------------------------------------------------------------- outgoing
    def bind_call(self, call_id, media_epoch):
        with self._lock:
            self._wire_epochs[call_id] = media_epoch

    def camera_frame(self, call_id, data, *, expected_epoch=None):
        jpeg = camera_jpeg(data)
        if not call_id or jpeg is None:
            return
        with self._lock:
            if expected_epoch is not None and self._wire_epochs.get(call_id) != expected_epoch:
                return
            owner = self._owners.get(call_id)
            if owner is None:
                return
            encoder = self._encoders.get(call_id)
            if encoder is None:
                claim = object()
                self._encoder_claims[call_id] = claim
                try:
                    encoder = self._encoders[call_id] = self.codec.encoder(
                        lambda unit: self._send(call_id, owner, claim, unit))
                except Exception:
                    self._encoder_claims.pop(call_id, None)
                    raise
                log.info("call video: camera encoder started")
            encoder.push(jpeg)

    def _send(self, call_id, owner, claim, unit):
        # False until the other side acknowledged our video: nothing to do
        with self._lock:
            if (self._owners.get(call_id) is owner
                    and self._encoder_claims.get(call_id) is claim):
                self.calls.write_video(call_id, unit, video_codec.FRAME_MS)

    def camera_stop(self, call_id, native=False, *, expected_epoch=None):
        with self._lock:
            if expected_epoch is not None and self._wire_epochs.get(call_id) != expected_epoch:
                return
            owner = self._owners.get(call_id)
            self._encoder_claims.pop(call_id, None)
            encoder = self._encoders.pop(call_id, None)
        try:
            if encoder is not None:
                encoder.close()
        finally:
            if native and call_id:
                with self._lock:
                    if (self._owners.get(call_id) is owner
                            and (expected_epoch is None or self._wire_epochs.get(call_id) == expected_epoch)):
                        self.calls.video(call_id, VIDEO_DISABLE)

    def camera_start(self, call_id, *, expected_epoch=None):
        with self._lock:
            if expected_epoch is not None and self._wire_epochs.get(call_id) != expected_epoch:
                return False
            return bool(call_id) and self.calls.video(call_id, VIDEO_ENABLE)

    # ------------------------------------------------------------- incoming
    def start_receiving(self, call_id):
        """Reads the other side's video of the call until it ends (once)."""
        claim = self._claim_receiver(call_id)
        if claim is None:
            return
        try:
            threading.Thread(target=self._pump, args=(call_id, claim), daemon=True,
                             name="call-video").start()
        except Exception:
            with self._lock:
                if self._receiving.get(call_id) is claim:
                    self._receiving.pop(call_id, None)
            raise

    def _claim_receiver(self, call_id):
        with self._lock:
            if not call_id or call_id in self._receiving:
                return None
            owner = self._owners.setdefault(call_id, object())
            claim = (object(), owner)
            self._receiving[call_id] = claim
            return claim

    def _owns_receiver(self, call_id, claim):
        return (claim is not None and self._receiving.get(call_id) is claim
                and self._owners.get(call_id) is claim[1])

    def _pump(self, call_id, claim):
        try:
            while True:
                with self._lock:
                    if not self._owns_receiver(call_id, claim):
                        return
                unit = self.calls.read_video(call_id, 500)
                if unit is None:
                    return
                if unit:
                    with self._lock:
                        if not self._owns_receiver(call_id, claim):
                            return
                        self._decoder(call_id, claim[1]).push(unit)
        except Exception:
            log.exception("call video: receiving")
        finally:
            with self._lock:
                if self._receiving.get(call_id) is claim:
                    self._receiving.pop(call_id, None)

    def _decoder(self, call_id, owner):
        with self._lock:
            decoder = self._decoders.get(call_id)
            if decoder is None:
                decoder = self._decoders[call_id] = self.codec.decoder(
                    lambda jpeg: self._show(call_id, owner, jpeg))
                log.info("call video: the other side's video started")
            return decoder

    def _show(self, call_id, owner, jpeg):
        with self._lock:
            if self._owners.get(call_id) is owner:
                self.emit("call:video:remote", {"session": self.name,
                                                **({'mediaEpoch': self._wire_epochs[call_id]} if call_id in self._wire_epochs else {}),
                                                "jpeg": base64.b64encode(jpeg).decode("ascii")})

    # ----------------------------------------------------------------- end
    def end(self, call_id):
        with self._lock:
            self._owners.pop(call_id, None)
            self._wire_epochs.pop(call_id, None)
            self._receiving.pop(call_id, None)
            self._encoder_claims.pop(call_id, None)
            pipes = [self._encoders.pop(call_id, None), self._decoders.pop(call_id, None)]
        for pipe in pipes:
            if pipe is not None:
                pipe.close()
