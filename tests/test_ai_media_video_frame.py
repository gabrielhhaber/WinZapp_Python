"""A video is described from still frames taken locally: only a picture is sent."""
import io

import pytest

from core.ai_media import config, prompts
from core.ai_media.errors import DescriptionError
from core.ai_media.payload import prepare_media
from core.ai_media.video_frame import extract_frame, extract_mosaic, frame_times, grid_shape

av = pytest.importorskip("av")
np = pytest.importorskip("numpy")
from PIL import Image


def make_video(frames=30, rate=10, rotation=0, audio=False):
    """A small mp4 whose frame ``i`` is uniformly gray ``i * 8``."""
    buffer = io.BytesIO()
    with av.open(buffer, "w", format="mp4") as container:
        stream = container.add_stream("mpeg4", rate=rate)
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        if rotation:
            stream.set_display_rotation(rotation)
        if audio:
            container.add_stream("aac", rate=8000)
        for i in range(frames):
            frame = av.VideoFrame.from_ndarray(np.full((48, 64, 3), i * 8, dtype=np.uint8), format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return buffer.getvalue()


def gray(jpeg):
    with Image.open(io.BytesIO(jpeg)) as image:
        return image.convert("L").resize((1, 1)).getpixel((0, 0)), image.size


def test_the_frame_is_a_jpeg_taken_about_a_second_in_not_the_first_frame():
    level, size = gray(extract_frame(make_video()))
    assert size == (64, 48)
    assert 60 <= level <= 100  # frame 10 of 30 at 10 fps is gray 80; the first would be 0


def test_a_clip_shorter_than_two_seconds_uses_its_middle():
    level, _ = gray(extract_frame(make_video(frames=10)))  # 1 s long: middle is frame 5 (gray 40)
    assert 25 <= level <= 55


def test_a_single_frame_video_still_gives_that_frame():
    level, _ = gray(extract_frame(make_video(frames=1)))
    assert level <= 10


def test_the_display_rotation_is_applied():
    _, size = gray(extract_frame(make_video(rotation=90)))
    assert size == (48, 64)


@pytest.mark.parametrize("data", [b"", b"not a video at all", b"\x00" * 4096, b"%PDF-1.7 body"])
def test_anything_that_is_not_a_video_is_a_format_error(data):
    with pytest.raises(DescriptionError, match="ai_error_media_format"):
        extract_frame(data)


def test_a_file_without_a_video_stream_is_a_format_error():
    buffer = io.BytesIO()
    with av.open(buffer, "w", format="wav") as container:
        stream = container.add_stream("pcm_s16le", rate=8000)
        frame = av.AudioFrame.from_ndarray(np.zeros((1, 800), dtype=np.int16), format="s16", layout="mono")
        frame.sample_rate = 8000
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    with pytest.raises(DescriptionError, match="ai_error_media_format"):
        extract_frame(buffer.getvalue())


def test_a_picture_larger_than_the_image_limit_is_a_size_error(monkeypatch):
    from core.ai_media import video_frame
    monkeypatch.setattr(video_frame, "MAX_PIXELS", 64 * 48 - 1)
    with pytest.raises(DescriptionError, match="ai_error_media_size"):
        extract_frame(make_video())


def test_prepare_media_turns_a_video_into_an_image_payload():
    video = make_video(audio=True)
    media = prepare_media("video", video, "video/mp4", video_frames="one")
    assert (media.kind, media.mime, media.filename) == ("image", "image/jpeg", "frame.jpg")
    assert media.data[:2] == b"\xff\xd8" and media.data != video and len(media.data) < len(video) * 4
    assert gray(media.data)[1] == (64, 48)


def test_a_video_is_described_by_any_provider_that_takes_pictures():
    for name, provider in config.PROVIDERS.items():
        assert config.supports(name, "video") == ("image" in provider.capabilities)
        assert "video" not in provider.capabilities  # nobody is sent a video


def test_a_video_cannot_reach_a_provider_as_a_video():
    from core.ai_media.payload import Media
    from core.ai_media.providers import build_request
    for name, provider in config.PROVIDERS.items():
        with pytest.raises(DescriptionError, match="ai_error_request"):
            build_request(name, provider.model, "key", Media("video", b"x", "video/mp4", "video.mp4"), (), "q", "i", "balanced")


def test_the_prompt_describes_one_frame_and_does_not_promise_story_or_speech():
    text = prompts.instructions("pt-BR", "balanced", "video", "one")
    assert "single frame taken from a video" in text and "do not guess" in text
    assert "what is said in order" not in text and "what happens in order" not in text
    assert "frame" not in prompts.instructions("pt-BR", "balanced", "image")


# -- several frames in one mosaic ---------------------------------------------

def test_frame_times_are_the_middles_of_equal_slices():
    assert frame_times(10.0, 5) == pytest.approx([1.0, 3.0, 5.0, 7.0, 9.0])


def test_a_short_clip_gets_fewer_frames_and_an_unknown_length_one():
    assert len(frame_times(1.0)) == 2
    assert len(frame_times(0.2)) == 1 and frame_times(0.2)[0] == pytest.approx(0.1)
    assert frame_times(0) == [0.0] and frame_times(None) == [0.0]
    assert len(frame_times(600.0)) == 5  # never more than the count asked for


def test_the_grid_reads_left_to_right_then_top_to_bottom():
    assert [grid_shape(n) for n in range(1, 7)] == [(1, 1), (2, 1), (3, 1), (2, 2), (3, 2), (3, 2)]


def test_the_mosaic_holds_the_frames_in_chronological_order():
    from core.ai_media import video_frame
    mosaic = Image.open(io.BytesIO(extract_mosaic(make_video(), 5))).convert("L")  # 3 s, gray = 8 * frame
    columns, rows = grid_shape(5)
    assert mosaic.size == (columns * 64 + (columns - 1) * video_frame._GAP,
                           rows * 48 + (rows - 1) * video_frame._GAP)
    levels = []
    for index in range(5):
        row, column = divmod(index, columns)
        x = column * (64 + video_frame._GAP) + 32
        y = row * (48 + video_frame._GAP) + 24
        levels.append(mosaic.getpixel((x, y)))
    assert levels == sorted(levels) and levels[0] < 60 and levels[-1] > 160


def test_a_single_frame_video_gives_a_mosaic_of_that_frame():
    image = Image.open(io.BytesIO(extract_mosaic(make_video(frames=1))))
    assert image.size == (64, 48)


def test_the_display_rotation_is_applied_to_every_cell_of_the_mosaic():
    image = Image.open(io.BytesIO(extract_mosaic(make_video(rotation=90), 4)))
    assert image.size == (2 * 48 + 8, 2 * 64 + 8)  # cells are 48 wide and 64 tall once turned


@pytest.mark.parametrize("data", [b"", b"not a video at all", b"%PDF-1.7 body"])
def test_the_mosaic_of_anything_that_is_not_a_video_is_a_format_error(data):
    with pytest.raises(DescriptionError, match="ai_error_media_format"):
        extract_mosaic(data)


def test_the_mosaic_refuses_a_picture_larger_than_the_image_limit(monkeypatch):
    from core.ai_media import video_frame
    monkeypatch.setattr(video_frame, "MAX_PIXELS", 64 * 48 - 1)
    with pytest.raises(DescriptionError, match="ai_error_media_size"):
        extract_mosaic(make_video())


def test_by_default_a_video_travels_as_one_mosaic_without_metadata():
    video = make_video(audio=True)
    media = prepare_media("video", video, "video/mp4")
    assert (media.kind, media.mime, media.filename) == ("image", "image/jpeg", "frames.jpg")
    assert media.data[:2] == b"\xff\xd8" and b"Exif" not in media.data[:64]
    assert gray(media.data)[1][0] > 64  # wider than one frame: several travel together


def test_the_mosaic_prompt_orders_the_frames_and_keeps_the_no_guessing_rule():
    text = prompts.instructions("pt-BR", "balanced", "video")
    assert text == prompts.instructions("pt-BR", "balanced", "video", "several")
    assert "chronological order" in text and "left to right, then top to bottom" in text
    assert "you do not know what happens between the frames" in text
    assert "cannot hear" in text and "do not guess what is said" in text
    assert "single frame taken from a video" not in text


def test_the_preference_defaults_to_several_frames_and_ignores_junk():
    class App(dict):
        pass
    assert config.preferences(App())["video_frames"] == "several"
    for value, expected in (("one", "one"), ("several", "several"), ("two", "several"), (1, "several"), (["one"], "several")):
        assert config.preferences(App(ai_media={"video_frames": value}))["video_frames"] == expected
