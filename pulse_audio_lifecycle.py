"""Lifecycle cleanup for WinZapp's per-session PulseAudio modules.

These modules only ever exist on a Linux host running WPPConnect Server for a
WinZapp client that has local API mode turned off — the remote-API deployment
described at the top of client/api_patches/src/util/callMediaBridge.ts. There
the session's Chrome has no audio device of its own, so each session gets a
virtual sink/source pair and `pacat`/`parec` carry the call audio to and from
the Windows client.

A `pactl load-module` outlives the process that asked for it, so a crashed or
killed Node leaves its devices behind and the next session stacks another pair
on top. Hence the sweep, and hence it running from the start script as well as
the stop ones.

This is a no-op on Windows, which is where WinZapp itself always runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess


PULSE_SERVER = "unix:/run/pulse/winzapp-native"
WINZAPP_PREFIX = "winzapp_"


def _pactl_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["PULSE_SERVER"] = PULSE_SERVER
    return env


def cleanup_winzapp_pulse_modules(*, quiet: bool = False) -> int:
    """Unload every WinZapp-owned PulseAudio module.

    Best-effort by design: it is called from the start/stop scripts, and the
    common case is that there is no PulseAudio at all (every Windows install,
    which is all of them for the client itself). Returning 0 quietly is the
    right answer there, not an error.

    Only module names containing ``winzapp_`` are touched, so the system
    PulseAudio service and every unrelated application are left alone.
    """
    if os.name == "nt" or shutil.which("pactl") is None:
        return 0
    env = _pactl_environment()
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "modules"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return 0
    if result.returncode != 0:
        return 0

    module_ids: list[str] = []
    for line in result.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) < 3:
            continue
        # Module arguments contain sink_name/source_name for our virtual
        # devices. Unloading in reverse order removes remap sources before
        # their backing sinks.
        if "winzapp_" in fields[2]:
            module_ids.append(fields[0])

    removed = 0
    for module_id in reversed(module_ids):
        unload = subprocess.run(
            ["pactl", "unload-module", module_id],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if unload.returncode == 0:
            removed += 1
    if not quiet and removed:
        print(f"Removed {removed} WinZapp PulseAudio module(s).")
    return removed

