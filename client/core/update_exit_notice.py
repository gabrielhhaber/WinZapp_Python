"""The notice an update installed on exit shows when it ends (issue #769).

With "install updates when I exit WinZapp" (update_on_exit.py) the app stays
closed after the install, so nothing said whether it finished: people waited
and guessed before opening WinZapp again. The installer now ends by showing a
Windows message box, in the user's language, saying whether the update was
installed. A message box, not a toast: it takes the focus, so NVDA reads it at
once, and it stays until it is closed.

The text travels inside the batch as PowerShell's -EncodedCommand (base64 of
UTF-16LE), so the script stays ASCII whatever the language
(updater._write_installer_script). `start` lets the script go on (cleanup,
self-delete) while the box waits for the user. DefaultDesktopOnly is what lets
a box from a background process come to the front with the focus.
"""
import base64
from typing import NamedTuple

#: a longer text is cut: the command line of a batch has a limit
MAX_TEXT = 400


class ExitNotice(NamedTuple):
    """Batch lines (each ending in a newline) for each ending."""
    installed: str
    not_installed: str


def _quoted(text) -> str:
    clean = "".join(ch if ch >= " " else " " for ch in str(text or ""))[:MAX_TEXT]
    return "'" + clean.replace("'", "''") + "'"


def notice_line(title, text, *, failed=False, indent="") -> str:
    """One batch line that shows *text* in a message box and does not wait."""
    icon = "Warning" if failed else "Information"
    script = ("Add-Type -AssemblyName System.Windows.Forms;"
              f"[void][System.Windows.Forms.MessageBox]::Show({_quoted(text)},{_quoted(title)},"
              f"'OK','{icon}','Button1','DefaultDesktopOnly')")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return (f'{indent}start "" /min powershell.exe -NoProfile -NonInteractive '
            f"-ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand {encoded}\n")


def exit_notice(i18n, version) -> ExitNotice:
    """The two notices of an install on exit of *version*, in the user's language."""
    title = i18n.t("update_on_exit_notice_title")
    return ExitNotice(
        notice_line(title, i18n.t("update_on_exit_installed").format(version=version)),
        notice_line(title, i18n.t("update_on_exit_not_installed").format(version=version),
                    failed=True),
    )


def indented(line, indent) -> str:
    """*line* under a batch block's indentation."""
    return indent + line.lstrip() if line else ""
