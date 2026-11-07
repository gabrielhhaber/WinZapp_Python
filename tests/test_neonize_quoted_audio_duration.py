"""A quoted voice note keeps its duration in the Neonize bridge (issue #707)."""
import sys
from pathlib import Path

import pytest

pytest.importorskip("neonize")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "neonize_bridge"))

import msgmap  # noqa: E402


def test_quoted_voice_note_carries_seconds():
    assert msgmap.quoted({"type": "ptt", "duration": 15}).audioMessage.seconds == 15
    assert msgmap.quoted({"type": "audio", "seconds": "7"}).audioMessage.seconds == 7


def test_quoted_voice_note_without_duration_leaves_seconds_unset():
    assert msgmap.quoted({"type": "ptt"}).audioMessage.seconds == 0
    assert msgmap.quoted({"type": "ptt", "duration": "x"}).audioMessage.seconds == 0


def test_received_quote_keeps_duration():
    q = msgmap._quoted_wpp(msgmap.quoted({"type": "ptt", "duration": 15}), "")
    assert q["type"] == "ptt"
    assert q["duration"] == 15
