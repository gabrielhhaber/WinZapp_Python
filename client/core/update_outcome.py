"""Read the last committed copy result without mistaking an old failure for it."""
import os
import re

RESULT_NAME = "update_result.marker"
_SUCCESS = re.compile(r"success:[0-9a-f]{32}\Z")


def previous_update_failure(install_dir):
    marker = os.path.join(install_dir, "update_failed.marker")
    try:
        with open(marker, encoding="utf-8", errors="replace") as stream:
            content = stream.read(512).strip()
    except FileNotFoundError:
        return None
    result = os.path.join(install_dir, RESULT_NAME)
    try:
        with open(result, encoding="ascii") as stream:
            latest = stream.read(128).strip()
        # Strictly newer: equal/coarse timestamps must not hide a new failure.
        if (_SUCCESS.fullmatch(latest)
                and os.stat(result).st_mtime_ns > os.stat(marker).st_mtime_ns):
            return None
    except (OSError, UnicodeError):
        pass
    return content


def batch_receipt(path, status, run_id, indent=""):
    if not path:
        return ""
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("invalid update result identity")
    return (f'{indent}cmd /c exit 0\n'
            f'{indent}> "{path}" echo {status}:{run_id}\n')


def batch_success(path, marker, run_id, log_path):
    if not path:
        return ""
    return (
        batch_receipt(path, "success", run_id)
        + "if errorlevel 1 goto UPDATE_RESULT_FAILED\n"
        + f'del "{marker}" >NUL 2>&1\n'
        + f'if exist "{marker}" >> "{log_path}" echo old failure marker retained; success receipt recorded\n'
    )
