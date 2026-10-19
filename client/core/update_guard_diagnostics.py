"""Allowlisted guardian diagnostics; never permission to install/relaunch."""
import re

from core.update_install_guard import read_signal

PHASES = frozenset(("compile", "guard_lock", "package_lock", "job_create",
                    "job_configure", "process_create", "job_assign", "process_resume",
                    "ready", "running", "exit_code"))


def diagnostic_summary(path):
    value = read_signal(path)
    stage = re.fullmatch(r"stage:([a-z_]+)", value)
    if stage and stage[1] in PHASES:
        return value
    error = re.fullmatch(r"error:([a-z_]+):([0-9]{1,10})", value)
    if error and error[1] in PHASES and int(error[2]) <= 0xFFFFFFFF:
        return value
    completed = re.fullmatch(r"completed:([0-9]{1,10})", value)
    if completed and int(completed[1]) <= 0xFFFFFFFF:
        return value
    return "unavailable"


def powershell_reporter(path_literal):
    """The caller supplies a quoted PS literal; diagnostics are best-effort."""
    return (
        "function Write-Diagnostic([string]$value) {\n"
        f"  try {{ [IO.File]::WriteAllText({path_literal}, $value, [Text.Encoding]::ASCII) }} catch {{}}\n"
        "}\n"
    )
