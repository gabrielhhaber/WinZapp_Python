"""Prepare the guardian and waiter before granting the batch permission."""
import logging
import os
import tempfile
import uuid

from core.update_file_lease import package_lock_path
from core.update_guard_process import guardian_script, launcher_script, powershell_path
from core.update_install_guard import await_ready, guard_paths, write_signal
from core.update_recovery import write_recovery_note
from core.update_guard_diagnostics import diagnostic_summary


def prepare_guard(global_dir, token):
    if global_dir and token:
        import update_coord
        return update_coord.arm_installer_guard(global_dir, token)
    return guard_paths(tempfile.gettempdir(), uuid.uuid4().hex)


def launch_guarded(batch, paths, package_root, handoff, exe, log, parent_pid,
                   needs_admin, write_batch, run_elevated, popen, start_waiter,
                   environment, recovery_text=""):
    """False is safe: until a commit receipt exists, no install writes occur."""
    guardian = batch[:-4] + "_guard.ps1"
    launcher = batch[:-4] + "_launch.bat"
    phase = "recovery_note"
    try:
        recovery_path = handoff + ".recovery.txt"
        write_recovery_note(recovery_path, recovery_text, os.path.dirname(exe), log)
        phase = "guardian_script"
        text = guardian_script(batch, paths,
                               package_lock_path(package_root) if package_root else "")
        # A BOM lets Windows PowerShell 5 read non-ASCII paths correctly.
        with open(guardian, "w", encoding="utf-8-sig", newline="\r\n") as stream:
            stream.write(text)
        phase = "launcher_script"
        if not write_batch(launcher, launcher_script(guardian, powershell_path())):
            raise OSError("installer launcher could not be prepared")
        phase = "spawn"
        if needs_admin:
            launched, pid = run_elevated(launcher)
            if not launched:
                paths["error"] = "update_uac_declined"
                raise OSError("installer launch declined or failed")
        else:
            import subprocess
            proc = popen(["cmd.exe", "/c", launcher],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         env=environment)
            pid = proc.pid
        def before_commit(guardian_pid):
            nonlocal phase
            phase = "owner_transfer"
            if getattr(paths, "owner_root", None):
                import update_coord
                update_coord.transfer_installer_guard(paths.owner_root, paths.owner_token,
                                                       guardian_pid)
            phase = "waiter"
            ready = start_waiter(
                batch, guardian_pid, exe, log, handoff, write_batch,
                parent_pid=parent_pid, completion_path=paths["done"],
                installer_image="powershell.exe", recovery_path=recovery_path)
            if ready:
                phase = "grant_verify"
            return ready
        phase = "ready_wait"
        if not pid or not await_ready(paths, before_commit=before_commit):
            raise OSError("installer guardian not ready before grant")
        return True  # Nothing fallible after a possibly-consumed commit.
    except Exception as exc:
        # A failed/ambiguous atomic commit must never be revoked.
        from core.update_install_guard import read_signal
        if read_signal(paths["commit"]) == "commit":
            return True
        try:
            logging.error("Auto-updater: installer preparation failed at %s (%s); guardian=%s",
                          phase, type(exc).__name__, diagnostic_summary(paths["diagnostic"]))
        except Exception:
            pass  # Neither a broken diagnostic nor a log sink may grant installation.
        try:
            write_signal(paths["abort"], "abort")
        except OSError:
            pass
        return False
