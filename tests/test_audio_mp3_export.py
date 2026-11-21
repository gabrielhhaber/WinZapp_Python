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

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        export_audio_as_mp3("ffmpeg", b"voice note", str(destination))

    assert destination.read_bytes() == b"existing file"
    assert staged and not any(path.exists() for path in staged)
