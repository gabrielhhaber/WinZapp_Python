"""Audio messages without ffprobe.

neonize's `build_audio_message` uploads the file and then runs `ffprobe` only to
fill `audioMessage.seconds`; WinZapp does not ship ffprobe, so every voice note
failed after the upload. `build` makes the same message with the duration from
media_probe.py.

A voice note also carries the `waveform` (64 bytes, 0-100) and `mediaKeyTimestamp`
that WhatsApp's own apps always send: without them WhatsApp on Android did not
deliver the voice note at all (one grey tick forever, issue #664) while iPhone
accepted it.

Tested in test_audio_message.py.
"""
import array
import sys
import time

import media_probe

WAVEFORM_SAMPLES = 64
WAVEFORM_RATE = 8000        # the decode rate; enough to draw 64 bars


def waveform_of_pcm(pcm):
    """The 64-byte waveform of signed 16-bit little-endian mono PCM: the mean
    level of each 64th of the recording, scaled so the loudest is 100. b"" when
    there is no sound to draw."""
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    if len(samples) < WAVEFORM_SAMPLES:
        return b""
    block = len(samples) // WAVEFORM_SAMPLES
    levels = [sum(abs(v) for v in samples[i * block:(i + 1) * block]) / block
              for i in range(WAVEFORM_SAMPLES)]
    loudest = max(levels)
    if loudest <= 0:
        return b""
    return bytes(min(100, int(level / loudest * 100)) for level in levels)


def _decoded_pcm(data):
    output = media_probe.run_ffmpeg(
        data, "-f", "s16le", "-ac", "1", "-ar", str(WAVEFORM_RATE), "-v", "error", "pipe:1")
    return b"" if output is None else output[0]


def waveform(data, pcm_of=None):
    """Waveform of the audio in `data` (decoded by the bundled ffmpeg); b"" when
    it cannot be decoded, so the voice note still goes out."""
    try:
        return waveform_of_pcm((pcm_of or _decoded_pcm)(data))
    except Exception:
        media_probe.log.exception("voice note waveform")
        return b""


def build(client, file, ptt, mimetype_of=None, seconds_of=None, waveform_of=None,
          now=time.time):
    """The audio Message neonize's `build_audio_message` would build, minus ffprobe.
    `file` is a path or the bytes; the upload goes through `client.upload`, so the
    progress client of transfers.py still sees it."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import AudioMessage, Message
    data = media_probe.read(file)
    upload = client.upload(data)
    mimetype = (mimetype_of or media_probe.mimetype)(data) or ""
    # neonize only detects "audio/ogg"; WhatsApp wants "audio/ogg; codecs=opus"
    # (without it the voice note may not even show up on some devices)
    if mimetype.startswith("audio/ogg"):
        mimetype = "audio/ogg; codecs=opus"
    message = Message(audioMessage=AudioMessage(
        URL=upload.url, directPath=upload.DirectPath, mediaKey=upload.MediaKey,
        fileEncSHA256=upload.FileEncSHA256, fileSHA256=upload.FileSHA256,
        fileLength=upload.FileLength, seconds=(seconds_of or media_probe.duration_seconds)(data),
        mimetype=mimetype, PTT=ptt, mediaKeyTimestamp=int(now())))
    if ptt:
        bars = (waveform_of or waveform)(data)
        if bars:
            message.audioMessage.waveform = bars
    return message
