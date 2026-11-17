"""Read channels, sample rate and length of an Ogg Vorbis file with the stdlib only."""
import struct
from pathlib import Path


def ogg_vorbis_info(path):
    """Return (channels, sample_rate, frames) of an Ogg Vorbis file, no decoder needed."""
    data = Path(path).read_bytes()
    assert data[:4] == b"OggS", "not an Ogg container"
    start = data.index(b"\x01vorbis")
    channels, rate = struct.unpack_from("<BI", data, start + 11)
    last = data.rindex(b"OggS")
    granule = struct.unpack_from("<q", data, last + 6)[0]
    return channels, rate, granule
