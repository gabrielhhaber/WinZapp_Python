"""Jump the voice message that is playing to a given second.

What the transcription window's "Listen from here" uses (the `[mm:ss]` list of
a long transcription, core/transcription/segment_list.py). A function over the
panel and not a mixin method: it reads three attributes of the playback that
`audio_playback.py` owns and adds nothing to the panel's own surface.

It only moves a playback that **already is that message's**. Starting one from
here would mean re-running the whole of `_toggle_playback()` (download, decrypt,
tempo, device recovery), and the saved resume position (`_audio_positions`) is
in bytes of a stream that does not exist yet, so it cannot be computed. The
window says so to the user instead of guessing: play the message, then ask.
Videos are left alone: their player is another object.
"""

from __future__ import annotations

import logging


def seek_message_audio(panel, msg_id, seconds) -> bool:
    """Move the playback of `msg_id` to `seconds` and make sure it is playing.

    False — and nothing touched — when that message is not the one loaded in
    the player, or the position cannot be set.
    """
    stream = getattr(panel, "_audio_stream", None)
    if not msg_id or stream is None or getattr(panel, "_current_audio_id", None) != msg_id:
        return False
    ctrl = getattr(panel, "_audio_tempo_ctrl", None) or stream
    try:
        total = ctrl.get_length()
        if total <= 0:
            return False
        # One byte short of the end, as seek_active_playback_by() does: landing
        # on the reported length is what BASS rejects for a decoded stream.
        position = max(0, min(total - 1, ctrl.seconds_to_bytes(max(0.0, float(seconds)))))
        ctrl.set_position(position)
        if not getattr(panel, "_is_audio_playing", False):
            ctrl.play()
            panel._is_audio_playing = True
            panel._audio_timer.Start(30)
        return True
    except Exception as exc:
        logging.info("[transcription] could not move the playback: %s", type(exc).__name__)
        return False
