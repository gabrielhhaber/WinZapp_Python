"""Bounded JPEG webcam capture for one-to-one calls."""

import logging
import re
import subprocess
import sys
import threading
import time


def camera_names(ffmpeg_output: str) -> list[str]:
    """Extract DirectShow video device names, excluding audio and aliases."""
    names = []
    in_video = False
    for line in ffmpeg_output.splitlines():
        if 'DirectShow video devices' in line:
            in_video = True
            continue
        if 'DirectShow audio devices' in line:
            in_video = False
        if not in_video or 'Alternative name' in line:
            continue
        # Recent FFmpeg builds may append a media-kind suffix, e.g.
        #   [dshow @ ...]  "Integrated Camera" (video)
        # and spacing after the dshow prefix is not stable.
        match = re.search(r'\]\s+"([^"]+)"(?:\s+\(video\))?\s*$', line, re.IGNORECASE)
        if match:
            names.append(match.group(1))
    return names


def list_camera_devices(ffmpeg: str) -> list[str]:
    """Enumerate DirectShow camera names via the same probe CameraCapture.start() uses."""
    if sys.platform != 'win32' or not ffmpeg:
        return []
    try:
        listed = subprocess.run(
            [ffmpeg, '-hide_banner', '-list_devices', 'true', '-f', 'dshow', '-i', 'dummy'],
            capture_output=True, text=True, errors='replace', timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        return []
    return camera_names(listed.stderr)


def jpeg_frames(stream, stop_event):
    """Yield complete JPEG images from FFmpeg's image2pipe output."""
    buffer = bytearray()
    while not stop_event.is_set():
        chunk = stream.read(4096)
        if not chunk:
            break
        buffer.extend(chunk)
        while not stop_event.is_set():
            start = buffer.find(b'\xff\xd8')
            if start < 0:
                buffer.clear()
                break
            if start:
                del buffer[:start]
            end = buffer.find(b'\xff\xd9', 2)
            if end < 0:
                if len(buffer) > 512_000:
                    buffer.clear()
                break
            frame = bytes(buffer[:end + 2])
            del buffer[:end + 2]
            yield frame


class CameraCapture:
    def __init__(self, ffmpeg: str, send_frame, transmit: bool = True):
        self.ffmpeg = ffmpeg
        self.send_frame = send_frame
        # A live attribute rather than a start()-time-only choice, so a
        # future caller could flip it on an already-open capture without
        # tearing it down. Read fresh every frame in _run(), never cached.
        self.transmit = transmit
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.process = None
        self.thread = None

    def start(self, preferred_name: str = ""):
        if sys.platform != 'win32':
            raise RuntimeError('Camera capture requires Windows')
        if not self.ffmpeg:
            raise RuntimeError('FFmpeg is required for camera capture')
        devices = list_camera_devices(self.ffmpeg)
        if not devices:
            raise RuntimeError('No camera found')
        # An empty/blank preference, or one that no longer matches a detected
        # device, means "system default" — the first camera ffmpeg lists,
        # exactly today's unconditional behaviour.
        device = preferred_name if preferred_name in devices else devices[0]
        logging.info('[call_video] camera selected: %s (detected=%d)', device, len(devices))
        self.process = subprocess.Popen(
            [self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'dshow',
             '-i', f'video={device}', '-an', '-vf', 'fps=10,scale=640:360:force_original_aspect_ratio=decrease',
             '-f', 'image2pipe', '-vcodec', 'mjpeg', '-q:v', '7', '-'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        # Poll instead of one flat ready.wait(8): when the camera is already
        # held by another app, ffmpeg exits within milliseconds, and waiting
        # out the full 8 s afterwards is pure dead time on the answer path.
        # accept_incoming_call() opens the camera BEFORE POSTing "accept",
        # with the ring tone already stopped, so every second spent here is a
        # second of total silence for a blind user who just pressed Answer --
        # and it applied to "answer without video" too, which is precisely
        # the person who did not want to wait for a camera.
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.ready.wait(0.1):
                break
            if self.process.poll() is not None:
                break
        if not self.ready.is_set():
            self.stop()
            raise RuntimeError('Camera did not produce video frames')

    def _run(self):
        accepted_frames = 0
        try:
            for frame in jpeg_frames(self.process.stdout, self.stop_event):
                if len(frame) <= 256_000:
                    accepted_frames += 1
                    if accepted_frames == 1 or accepted_frames % 100 == 0:
                        logging.info(
                            '[call_video] camera frames captured=%d bytes=%d',
                            accepted_frames, len(frame),
                        )
                    self.ready.set()
                    # Probing with transmit=False must never leak a frame to
                    # the peer: skip send_frame entirely rather than racing
                    # a caller that stops capture right after ready fires.
                    #
                    # stop_event is re-read here, not just between chunks:
                    # one read(4096) can carry several complete JPEGs, so
                    # after "turn video off" set the event, the frames
                    # already decoded from that last chunk would still have
                    # been sent -- the peer kept seeing the user for a
                    # moment after WinZapp announced video was off.
                    if self.transmit and not self.stop_event.is_set():
                        try:
                            self.send_frame(frame)
                        except Exception:
                            logging.exception('[call_video] failed to send camera frame')
        except Exception:
            if not self.stop_event.is_set():
                logging.exception('[call_video] camera capture failed')

    def stop(self):
        self.stop_event.set()
        if self.process is not None:
            self.process.kill()
            self.process = None
