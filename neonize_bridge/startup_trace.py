"""Marks of the bridge's start in its output (bridge.log).

A bridge that hangs before it logs anything leaves an empty file and no hint of
where it stopped. Each mark is one flushed line: the last one in the file is the
step that never finished. Nothing here is personal data.
"""
import platform
import sys
import time

_STARTED = time.monotonic()


def line(step: str, elapsed: float) -> str:
    return "bridge start +%.1fs: %s" % (elapsed, step)


def mark(step: str, stream=None) -> None:
    stream = stream or sys.stderr
    try:
        print(line(step, time.monotonic() - _STARTED), file=stream, flush=True)
    except (OSError, ValueError):       # no usable stream: never stop the start for a mark
        pass


def describe() -> str:
    return "python %s %s, platform.machine()=%s" % (
        platform.python_version(), platform.architecture()[0], platform.machine())
