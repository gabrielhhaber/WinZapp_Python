"""Media measured without ffprobe: the duration read from Ogg and MP4 bytes built
here, the ffmpeg stderr parsing and the fallbacks. Runs no ffmpeg."""
import struct

import pytest

import media_probe as mp


def _ogg_page(payload, granule):
    return (b"OggS" + bytes([0, 0]) + struct.pack("<q", granule) + b"\0" * 12
            + bytes([1, len(payload)]) + payload)


def opus(granule, pre_skip=312, last_granule=None):
    head = b"OpusHead" + bytes([1, 1]) + struct.pack("<H", pre_skip) + b"\0" * 7
    pages = _ogg_page(head, 0) + _ogg_page(b"OpusTags", 0) + _ogg_page(b"audio", granule)
    if last_granule is not None:
        pages += _ogg_page(b"more", last_granule)
    return pages


def _vorbis(granule, rate=44100):
    head = b"\x01vorbis" + struct.pack("<I", 0) + bytes([2]) + struct.pack("<I", rate) + b"\0" * 10
    return _ogg_page(head, 0) + _ogg_page(b"audio", granule)


def _box(kind, payload):
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def mp4(scale, length, version=0, moov_last=False):
    if version == 1:
        mvhd = bytes([1, 0, 0, 0]) + b"\0" * 16 + struct.pack(">IQ", scale, length)
    else:
        mvhd = bytes([0, 0, 0, 0]) + b"\0" * 8 + struct.pack(">II", scale, length)
    moov = _box(b"moov", _box(b"trak", b"") + _box(b"mvhd", mvhd + b"\0" * 80))
    mdat = _box(b"mdat", b"\0" * 64)
    head = _box(b"ftyp", b"isom\0\0\0\0") + _box(b"free", b"")
    return head + (mdat + moov if moov_last else moov + mdat)


def test_opus_voice_note_counts_at_48_khz_minus_the_pre_skip():
    assert mp.ogg_seconds(opus(48000 * 7 + 312)) == pytest.approx(7.0)


def test_ogg_skips_a_last_page_where_no_packet_ends():
    assert mp.ogg_seconds(opus(48000 * 3 + 312, last_granule=-1)) == pytest.approx(3.0)


def test_vorbis_uses_the_rate_of_its_header():
    assert mp.ogg_seconds(_vorbis(44100 * 5, rate=44100)) == pytest.approx(5.0)


@pytest.mark.parametrize("data", [b"", b"ID3...", b"OggS" + b"\0" * 40])
def test_not_an_ogg_it_can_read_is_none(data):
    assert mp.ogg_seconds(data) is None


@pytest.mark.parametrize("version", [0, 1])
@pytest.mark.parametrize("moov_last", [False, True])
def test_mp4_duration_comes_from_mvhd(version, moov_last):
    assert mp.mp4_seconds(mp4(44100, 44100 * 12, version, moov_last)) == pytest.approx(12.0)


@pytest.mark.parametrize("data", [b"", opus(48000), _box(b"moov", b""), mp4(0, 100)])
def test_not_an_mp4_it_can_read_is_none(data):
    assert mp.mp4_seconds(data) is None


def test_ffmpeg_output_duration_line():
    stderr = ("Input #0, mp3, from 'x.media':\n"
              "  Duration: 00:01:02.53, start: 0.025057, bitrate: 128 kb/s\n"
              "At least one output file must be specified\n")
    assert mp.ffmpeg_output_seconds(stderr) == pytest.approx(62.53)


@pytest.mark.parametrize("stderr", ["", None, "Duration: N/A, bitrate: N/A"])
def test_ffmpeg_output_without_a_duration_is_none(stderr):
    assert mp.ffmpeg_output_seconds(stderr) is None


def test_duration_asks_ffmpeg_only_for_what_it_cannot_read():
    asked = []

    def ffmpeg(data):
        asked.append(data)
        return 9.6
    assert mp.duration_seconds(opus(48000 * 4 + 312), ffmpeg) == 4
    assert mp.duration_seconds(mp4(1000, 2400), ffmpeg) == 2
    assert asked == []
    assert mp.duration_seconds(b"ID3 mp3", ffmpeg) == 10
    assert asked == [b"ID3 mp3"]


def test_duration_nobody_can_measure_is_zero_not_an_error():
    assert mp.duration_seconds(b"ID3 mp3", lambda data: None) == 0


def test_without_ffmpeg_the_thumbnail_is_empty_and_the_duration_unknown(monkeypatch):
    monkeypatch.setattr(mp.shutil, "which", lambda name: None)
    assert mp.thumbnail_and_seconds(b"video") == (b"", None)


def test_read_takes_a_path_or_the_bytes(tmp_path):
    path = tmp_path / "a.bin"
    path.write_bytes(b"abc")
    assert mp.read(str(path)) == b"abc"
    assert mp.read(bytearray(b"xyz")) == b"xyz"


# --- libmagic that cannot load its database ------------------------------

def _broken_magic(monkeypatch):
    import sys
    import types

    def from_buffer(data, mime=False):
        raise RuntimeError("could not find any valid magic files!")
    monkeypatch.setitem(sys.modules, "magic", types.SimpleNamespace(from_buffer=from_buffer))


@pytest.mark.parametrize("data,expected", [
    (opus(48000), "audio/ogg"),
    (b"\xff\xd8\xff\xe0" + b"\0" * 20, "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n" + b"\0" * 8, "image/png"),
    (b"\0\0\0\x20ftypM4A \0\0\0\0", "audio/mp4"),
    (b"\0\0\0\x20ftypisom\0\0\0\0", "video/mp4"),
    (b"RIFF\0\0\0\0WAVEfmt ", "audio/x-wav"),
    (b"RIFF\0\0\0\0WEBPVP8 ", "image/webp"),
    (b"ID3\x04" + b"\0" * 12, "audio/mpeg"),
    (b"%PDF-1.7" + b"\0" * 8, "application/pdf"),
    (b"nothing known here!!", ""),
])
def test_sniff_mimetype_from_the_first_bytes(data, expected):
    assert mp.sniff_mimetype(data) == expected


def test_mimetype_falls_back_to_the_signature_when_libmagic_cannot_run(monkeypatch):
    _broken_magic(monkeypatch)
    assert mp.mimetype(opus(48000)) == "audio/ogg"


def test_media_type_of_follows_the_mimetype():
    from neonize.utils.enum import MediaType
    assert mp.media_type_of(b"x", lambda d: "audio/ogg") is MediaType.MediaAudio
    assert mp.media_type_of(b"x", lambda d: "video/mp4") is MediaType.MediaVideo
    assert mp.media_type_of(b"x", lambda d: "image/png") is MediaType.MediaImage
    assert mp.media_type_of(b"x", lambda d: "application/pdf") is MediaType.MediaDocument
    assert mp.media_type_of(b"x", lambda d: "") is MediaType.MediaDocument


def test_neonizes_from_magic_survives_a_broken_libmagic(monkeypatch):
    from neonize.utils.enum import MediaType
    def broken(cls, data):
        raise RuntimeError("could not find any valid magic files!")
    monkeypatch.setattr(MediaType, "from_magic", classmethod(broken))
    mp.install_safe_from_magic()
    assert MediaType.from_magic(opus(48000)) is MediaType.MediaAudio
    assert MediaType.from_magic(b"\xff\xd8\xff\xe0" + b"\0" * 12) is MediaType.MediaImage
