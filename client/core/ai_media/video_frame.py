"""Take still pictures out of a video, locally, so only a picture leaves.

The video's audio is never opened: only the video stream is demuxed and
decoded. One frame (``extract_frame``) or a handful spaced over the clip,
joined into one mosaic (``extract_mosaic``), is then shaped by
``image_input.prepare_image`` like any photo (oriented, re-encoded, metadata
removed).
"""
from io import BytesIO

from PIL import Image

from .config import MAX_PIXELS
from .errors import DescriptionError

#: A second in usually skips a black or fading first frame; a shorter video
#: uses its middle.
_TARGET_SECONDS = 1.0
#: Frames decoded at most while looking for the target (about 10 s at 30 fps).
_MAX_FRAMES = 300
_ROTATIONS = {90: 90, 180: 180, 270: 270}

#: Frames of a mosaic, and the shortest spacing worth another one: a clip of a
#: second or two gets fewer frames rather than near-identical ones.
FRAME_COUNT = 5
_MIN_GAP_SECONDS = 0.5
#: Largest side of one cell, and the gap between cells. Three cells across stay
#: under the 3072 pixels ``prepare_image`` keeps.
_CELL_SIDE = 1000
_GAP = 8


def frame_times(duration, count=FRAME_COUNT):
    """The instants (seconds) to take frames at: the middle of ``count`` equal
    slices of the clip, so none is the often black first frame or the last one.
    A clip too short for ``count`` gets fewer; an unknown length gets the first
    frame only."""
    if not duration or duration <= 0:
        return [0.0]
    count = max(1, min(count, int(duration // _MIN_GAP_SECONDS)))
    return [duration * (i + 0.5) / count for i in range(count)]


def grid_shape(count):
    """(columns, rows) of a mosaic of ``count`` frames, read left to right and
    then top to bottom."""
    if count <= 3:
        return max(count, 1), 1
    if count == 4:
        return 2, 2
    return 3, (count + 2) // 3


def _frame_image(frame):
    """The frame as a PIL image with the display rotation applied."""
    image = frame.to_image()
    turns = _ROTATIONS.get(int(round(getattr(frame, "rotation", 0) or 0)) % 360)
    if turns:
        # PyAV reports the display matrix as counter-clockwise degrees.
        image = image.rotate(turns, expand=True)
    return image


def _open_video(data, av):
    container = av.open(BytesIO(data), mode="r")
    try:
        if not container.streams.video:
            raise DescriptionError("media_format")
        stream = container.streams.video[0]
        if (stream.width or 0) * (stream.height or 0) > MAX_PIXELS:
            raise DescriptionError("media_size")
    except BaseException:
        container.close()
        raise
    return container, stream


def extract_frame(data):
    """JPEG bytes of one frame of the video in ``data``.

    Raises ``DescriptionError`` ``media_format`` for anything that is not a
    decodable video and ``media_size`` for a picture larger than the image
    limit."""
    try:
        import av
        from av.error import FFmpegError
    except ImportError:
        raise DescriptionError("media_format") from None
    try:
        container, stream = _open_video(data, av)
        with container:
            duration = _duration(container, av)
            target = min(_TARGET_SECONDS, duration / 2) if duration > 0 else 0.0
            frame = _pick_frame(container, stream, av, target)
            if frame is None:
                raise DescriptionError("media_format")
            output = BytesIO()
            _frame_image(frame).convert("RGB").save(output, format="JPEG", quality=92)
            return output.getvalue()
    except DescriptionError:
        raise
    except (FFmpegError, OSError, ValueError, TypeError, MemoryError):
        raise DescriptionError("media_format") from None


def extract_mosaic(data, count=FRAME_COUNT):
    """JPEG bytes of one picture holding up to ``count`` frames spaced over the
    video in ``data``, in chronological order (see ``grid_shape``).

    Every frame is shrunk as soon as it is decoded, so a large video never has
    several full-size pictures in memory. Same errors as ``extract_frame``."""
    try:
        import av
        from av.error import FFmpegError
    except ImportError:
        raise DescriptionError("media_format") from None
    try:
        container, stream = _open_video(data, av)
        with container:
            cells, seen = [], set()
            for target in frame_times(_duration(container, av), count):
                frame = _pick_frame(container, stream, av, target)
                moment = None if frame is None else frame.pts
                if frame is None or (moment is not None and moment in seen):
                    continue  # the same frame twice would only waste the picture
                seen.add(moment)
                image = _frame_image(frame).convert("RGB")
                image.thumbnail((_CELL_SIDE, _CELL_SIDE))
                cells.append(image)
            if not cells:
                raise DescriptionError("media_format")
            output = BytesIO()
            _mosaic(cells).save(output, format="JPEG", quality=92)
            return output.getvalue()
    except DescriptionError:
        raise
    except (FFmpegError, OSError, ValueError, TypeError, MemoryError):
        raise DescriptionError("media_format") from None


def _mosaic(cells):
    columns, rows = grid_shape(len(cells))
    width = max(cell.width for cell in cells)
    height = max(cell.height for cell in cells)
    canvas = Image.new("RGB", (columns * width + (columns - 1) * _GAP,
                               rows * height + (rows - 1) * _GAP), "white")
    for index, cell in enumerate(cells):
        row, column = divmod(index, columns)
        canvas.paste(cell, (column * (width + _GAP) + (width - cell.width) // 2,
                            row * (height + _GAP) + (height - cell.height) // 2))
    return canvas


def _duration(container, av):
    return float(container.duration) / av.time_base if container.duration else 0.0


def _pick_frame(container, stream, av, target):
    if target > 0:
        try:
            container.seek(int(target * av.time_base))
        except (av.error.FFmpegError, OSError, ValueError):
            container.seek(0)
    else:
        container.seek(0)
    chosen = None
    for count, frame in enumerate(container.decode(stream)):
        chosen = frame
        if (frame.time or 0.0) >= target or count + 1 >= _MAX_FRAMES:
            break
    return chosen
