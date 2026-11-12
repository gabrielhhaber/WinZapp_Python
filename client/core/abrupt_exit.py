"""A run that ended without closing, and what it leaves for the next report.

Reported 2026-11-12: a WinZapp run ended right after a group call dropped,
and the next one after the engine was switched to Neonize, both without
going through the close: shutdown_audit.log, which records every close,
had no line for them. The WPPConnect Node of the first was left running,
the second adopted it and left it too, and the Neonize bridge then found
the port taken. Nothing said why: log.log is rewritten at every launch,
and a native crash (in the call audio, say) reaches no Python handler.

* `enable_fatal_log()` keeps a fatal.log in the account's logs folder where
  faulthandler writes every thread's stack if the process dies of a native
  fault. The log export takes the whole folder, so it reaches a report.
* `abrupt_previous_run()` reads shutdown_audit.log at startup: the previous
  run that never reached `_stop_wpp_server` (the first thing every close
  records) ended abruptly, and the next start says so.
"""

import faulthandler
import logging
import os
import re
import time

#: what a run writes to shutdown_audit.log when it closes, in any way the
#: app controls (a normal quit, Windows shutting down, the forced exit)
CLOSE_MARKS = ("_stop_wpp_server START", "quit did not finish")
_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[pid=(\d+)\] (.*)$")
#: bytes read from the end of shutdown_audit.log (it grows for months)
TAIL_BYTES = 64 * 1024

_fatal_file = None


def enable_fatal_log(path) -> bool:
    """faulthandler into *path* (appended, one header per run). The file
    stays open for the life of the process, as faulthandler needs."""
    global _fatal_file
    try:
        stream = open(path, "a", encoding="utf-8")
        stream.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} started\n")
        stream.flush()
        faulthandler.enable(file=stream, all_threads=True)
    except Exception:
        logging.warning("[startup] fatal.log unavailable; a native crash would leave no trace")
        return False
    _fatal_file = stream
    return True


def abrupt_previous_run(lines, my_pid):
    """(pid, last time it wrote) of the run before *my_pid* when it never
    recorded a close; None when it closed, or there is none."""
    runs = []                      # [pid, last time, closed], oldest first
    for line in lines:
        found = _LINE.match(line.rstrip("\n"))
        if not found:
            continue
        stamp, pid, message = found.group(1), int(found.group(2)), found.group(3)
        if pid == my_pid:
            continue
        if not runs or runs[-1][0] != pid:
            runs.append([pid, stamp, False])
        runs[-1][1] = stamp
        if any(mark in message for mark in CLOSE_MARKS):
            runs[-1][2] = True
    if not runs or runs[-1][2]:
        return None
    return runs[-1][0], runs[-1][1]


def read_tail(path, size=TAIL_BYTES) -> list:
    """The last lines of *path* (a partial first line dropped); [] when unreadable."""
    try:
        with open(path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            end = stream.tell()
            stream.seek(max(0, end - size))
            data = stream.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = data.splitlines()
    return lines[1:] if end > size else lines


def note_abrupt_previous_run(audit_path, my_pid, audit) -> bool:
    """Say, in log.log and through *audit*, that the previous run ended
    without closing. True when it did."""
    found = abrupt_previous_run(read_tail(audit_path), my_pid)
    if found is None:
        return False
    pid, last = found
    logging.warning("[startup] the previous run (pid %s, last seen %s) ended without closing: "
                    "a crash or a forced end; its Node may still be running", pid, last)
    audit(f"previous run pid={pid} ended without closing (last line {last})")
    return True
