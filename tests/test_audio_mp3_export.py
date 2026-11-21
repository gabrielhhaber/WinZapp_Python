from pathlib import Path
import subprocess

import pytest

from core.audio_transcode import export_audio_as_mp3


def test_mp3_export_keeps_existing_file_if_encoder_fails(tmp_path, monkeypatch):
    destination = tmp_path / "note.mp3"
    destination.write_bytes(b"existing file")
    staged = []

    def fail(args, **kwargs):
        assert "libmp3lame" in args
        assert kwargs["check"] is True
        source = Path(args[args.index("-i") + 1])
        output = Path(args[-1])
        assert source.read_bytes() == b"voice note"
        assert output != destination
        output.write_bytes(b"partial encode")
        staged.extend((source, output))
        raise subprocess.CalledProcessError(1, args)

    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_bytes(b"")
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        export_audio_as_mp3(str(ffmpeg), b"voice note", str(destination))

    assert destination.read_bytes() == b"existing file"
    assert staged and not any(path.exists() for path in staged)


def test_mp3_export_reports_a_missing_ffmpeg_and_keeps_the_file(tmp_path):
    destination = tmp_path / "note.mp3"
    destination.write_bytes(b"existing file")
    with pytest.raises(FileNotFoundError):
        export_audio_as_mp3(None, b"voice note", str(destination))
    assert destination.read_bytes() == b"existing file"


def test_mp3_export_publishes_the_encoded_file(tmp_path, monkeypatch):
    destination = tmp_path / "note.mp3"
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_bytes(b"")

    def encode(args, **kwargs):
        Path(args[-1]).write_bytes(b"mp3 data")

    monkeypatch.setattr(subprocess, "run", encode)
    export_audio_as_mp3(str(ffmpeg), b"voice note", str(destination))
    assert destination.read_bytes() == b"mp3 data"
