"""Restart WinZapp the Mac way (core/app_restart.py's restart_this_program).

Choosing another connection engine in the pairing dialog stores it and
restarts WinZapp (ui/dialogs/connect_engine.py). Upstream arranges that with
a Windows batch file (tasklist, ping, start) run by cmd.exe, which cannot
start on a Mac, so the switch always ended in "WinZapp could not restart by
itself" and the engine was put back.

Here a detached /bin/sh waits for this process to end (at most
app_restart.WAIT_PASSES one-second checks; it never starts a second copy over
a process that refuses to end), then opens the SAME app bundle again by its
absolute path with ``open -n``, keeping this run's non-transient arguments
(app_restart.TRANSIENT_ARGUMENTS). Never ``open -a WinZapp``: LaunchServices
may pick another copy of the app. A development run (macos/launcher.py) is
started again through the launcher, so the new run has the Mac layer too.

The caller quits right after a True answer, as on Windows
(connect.py on_engine_chosen -> on_quit_from_connect); Node is stopped on
that quit by server_mac's _stop_wpp_server (atexit or real_exit), before the
helper sees the pid gone.
"""

import logging
import os
import subprocess
import sys

LOG = "[restart_mac]"

#: Arguments macOS itself may add to a launch (old Process Serial Number).
_SYSTEM_ARGUMENT_PREFIXES = ("-psn_",)

# Detached from WinZapp: wait for pid $1 to end, at most $2 one-second checks,
# then run the command ($4...). $3 is the log file ("" for none).
RESTART_SCRIPT = """pid="$1"; limit="$2"; log="$3"; shift 3
n=0
while kill -0 "$pid" 2>/dev/null; do
  n=$((n + 1))
  if [ "$n" -ge "$limit" ]; then
    [ -n "$log" ] && echo "the previous run did not end - not restarting" >> "$log"
    exit 1
  fi
  sleep 1
done
[ -n "$log" ] && echo "restarting WinZapp" >> "$log"
exec "$@"
"""


def bundle_path(executable):
    """The .app holding *executable* (.../WinZapp.app/Contents/MacOS/WinZapp),
    absolute and with symlinks resolved, or "" when it is not inside one."""
    exe = os.path.realpath(executable)
    macos_dir = os.path.dirname(exe)
    contents = os.path.dirname(macos_dir)
    app = os.path.dirname(contents)
    if (os.path.basename(macos_dir) == "MacOS" and os.path.basename(contents) == "Contents"
            and app.endswith(".app")):
        return app
    return ""


def kept_arguments(argv, transient):
    """This run's arguments that the next run keeps."""
    return [a for a in list(argv)[1:]
            if a not in transient and not str(a).startswith(_SYSTEM_ARGUMENT_PREFIXES)]


def restart_command(executable, argv, frozen, launcher, transient):
    """The command the helper runs once this process is gone: the running
    bundle opened again by its path (a new instance, -n, since another
    account's process may still be running), or, in a development run, the
    interpreter on macos/launcher.py."""
    kept = kept_arguments(argv, transient)
    if frozen:
        app = bundle_path(executable)
        if not app:
            return [os.path.realpath(executable)] + kept
        return ["/usr/bin/open", "-n", app] + (["--args"] + kept if kept else [])
    return [executable, os.path.abspath(launcher)] + kept


def helper_command(pid, command, log_path, passes):
    """argv of the detached shell (RESTART_SCRIPT)."""
    return ["/bin/sh", "-c", RESTART_SCRIPT, "winzapp-restart",
            str(int(pid)), str(int(passes)), log_path or "", *command]


def start_helper(pid, command, log_path, passes, environment, popen=None):
    """Launch the helper, detached. True when it started."""
    try:
        (popen or subprocess.Popen)(helper_command(pid, command, log_path, passes), env=environment,
              start_new_session=True, close_fds=True, stdin=subprocess.DEVNULL,
              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, ValueError):
        logging.exception("%s could not start the restart helper", LOG)
        return False


def _launcher_path():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "launcher.py")


def restart_this_program(script_path="", log_path="", popen=None):
    """app_restart.restart_this_program on the Mac. *script_path* (upstream's
    .bat) is not used: the script is passed to /bin/sh inline."""
    from core import app_restart
    from update_relaunch import relaunch_environment

    frozen = bool(getattr(sys, "frozen", False))
    command = restart_command(sys.executable, sys.argv, frozen, _launcher_path(),
                              app_restart.TRANSIENT_ARGUMENTS)
    started = start_helper(os.getpid(), command, log_path, app_restart.WAIT_PASSES,
                           relaunch_environment(os.environ), popen)
    if started:
        logging.info("%s restart arranged: %s once pid %s ends", LOG,
                     command[2] if command[:2] == ["/usr/bin/open", "-n"] else command[0],
                     os.getpid())
    return started


def install():
    from core import app_restart
    app_restart.restart_this_program = restart_this_program
