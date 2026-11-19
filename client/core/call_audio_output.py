"""Output rendering: mandatory playout and optional echo reference are isolated."""


def render_output(outdata, buffer, owner, rate, tap, echo_enabled):
    """No logging, reopen or new lock on PortAudio's callback thread.

    The reservoir checks the captured stream owner under its existing drain
    lock. A retired callback writes silence without consuming replacement PCM.
    Its tap and rate are snapshots too, including a callback paused after fill.
    """
    try:
        view = outdata[:, 0] if getattr(outdata, "ndim", 1) > 1 else outdata
        if not buffer.fill(view, owner=owner):
            outdata[:] = 0
            return
        if getattr(outdata, "ndim", 1) > 1:
            outdata[:, 1:] = view[:, None]
    except Exception:
        try:
            outdata[:] = 0
        except Exception:
            pass
        return
    if echo_enabled:
        try:
            tap.append((view.copy(), rate))
        except Exception:
            # Reference failure must not erase successfully rendered speech.
            pass
