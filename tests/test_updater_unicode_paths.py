"""Tests for issue #83: the auto-updater never applied the update on a Windows
account whose name contains a non-ASCII character ("Paweł").

The updater downloaded and extracted correctly, closed the app — and nothing
happened. On the next manual start the same update was offered again. No
update_failed.marker, no relaunch, no trace of a failure anywhere.

Root cause: the batch installer was written with encoding="utf-8", but cmd.exe
decodes a .bat file in the console's OEM code page (CP852 on a Polish Windows,
CP850 here). "Paweł" written as UTF-8 and read back as CP852 is mojibake, so
every path in the script — source, target, marker, exe — pointed at a
directory that does not exist. That single fact explains all four symptoms at
once: nothing copied, no marker written (its path has the same character), no
relaunch (so does the exe path), and the script deleted itself afterwards,
taking the only evidence with it.

The fix writes the script in the code page cmd will actually read it with,
prefers 8.3 short paths (pure ASCII) where the volume still generates them,
refuses to write a script it cannot represent instead of running a broken one,
and leaves a log next to the install.
"""

import os
import subprocess
import sys

import pytest

import updater


_OEM = updater._oem_encoding()


def _oem_can_encode(text: str) -> bool:
    try:
        text.encode(_OEM)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


class TestOemEncoding:
    def test_it_names_a_code_page_python_knows(self):
        assert _OEM.startswith("cp")
        "abc".encode(_OEM)  # must be a real codec

    def test_it_falls_back_when_the_api_is_unavailable(self, monkeypatch):
        class _Boom:
            def __getattr__(self, name):
                raise OSError("no windll here")

        monkeypatch.setattr(updater.ctypes, "windll", _Boom(), raising=False)
        assert updater._oem_encoding() == "cp850"


class TestConsoleSafePath:
    def test_an_ascii_path_is_returned_untouched(self):
        assert updater._console_safe_path(r"C:\WinZapp") == r"C:\WinZapp"

    def test_an_empty_path_is_returned_untouched(self):
        assert updater._console_safe_path("") == ""

    def test_a_failing_lookup_falls_back_to_the_long_path(self, monkeypatch):
        """8.3 generation is disabled on many modern volumes, and the path may
        not exist yet — either way the long path is returned and the encoding
        step is what has to carry it."""
        long_path = r"C:\Users\Pawe\u0142\AppData\Local\WinZapp".replace("\\u0142", "\u0142")

        class _Kernel:
            def GetShortPathNameW(self, path, buf, size):
                return 0

        monkeypatch.setattr(updater.ctypes, "windll",
                            type("W", (), {"kernel32": _Kernel()})(), raising=False)
        assert updater._console_safe_path(long_path) == long_path

    def test_a_short_name_is_used_when_it_is_ascii(self, monkeypatch):
        class _Kernel:
            def GetShortPathNameW(self, path, buf, size):
                buf.value = r"C:\Users\PAWE~1\AppData\Local\WinZapp"
                return len(buf.value)

        monkeypatch.setattr(updater.ctypes, "windll",
                            type("W", (), {"kernel32": _Kernel()})(), raising=False)
        out = updater._console_safe_path("C:\\Users\\Pawe\u0142\\AppData\\Local\\WinZapp")
        assert out == r"C:\Users\PAWE~1\AppData\Local\WinZapp"
        assert out.isascii()

    def test_a_non_ascii_short_name_is_rejected(self, monkeypatch):
        """A "short" name that is still non-ASCII buys nothing — the long path
        is just as usable and the encoding step handles both the same way."""
        class _Kernel:
            def GetShortPathNameW(self, path, buf, size):
                buf.value = "C:\\PAWE\u0142~1"
                return len(buf.value)

        monkeypatch.setattr(updater.ctypes, "windll",
                            type("W", (), {"kernel32": _Kernel()})(), raising=False)
        original = "C:\\Users\\Pawe\u0142\\WinZapp"
        assert updater._console_safe_path(original) == original


def _script(install=r"C:\WinZapp", source=r"C:\tmp\ext"):
    return updater._build_installer_script(
        source, install,
        os.path.join(install, "WinZapp.exe"),
        os.path.join(install, "update_install.log"),
        os.path.join(install, "update_failed.marker"),
        pid=4242, api_port=6300,
    )


class TestInstallerScript:
    def test_it_copies_from_the_source_to_the_install_dir(self):
        s = _script()
        assert r'xcopy /E /Y /I /H "C:\tmp\ext\*" "C:\WinZapp\"' in s

    def test_it_waits_for_the_pid_and_frees_the_api_port(self):
        s = _script()
        assert '"PID eq 4242"' in s
        assert ":6300" in s
        assert ":5433" in s

    def test_it_writes_a_log_next_to_the_install(self):
        """The original script recorded nothing anywhere, which is why a user
        could only report "it just doesn't update"."""
        s = _script()
        assert r'"C:\WinZapp\update_install.log"' in s
        assert "chcp" in s, "the active code page is the fact that explains a bad path"

    def test_the_previous_log_is_kept_as_dot_old_before_truncating(self):
        """Reported live: a disconnection/re-pairing incident happened right
        as an update landed, but by the time it was investigated
        update_install.log only held the NEXT, unrelated, healthy update —
        the script's `> "{log_path}" echo ...` unconditionally truncates the
        log at the start of every run, with no way to see what an earlier,
        problematic update actually did. Moving the previous log aside
        first means at least the run before the current one survives."""
        s = _script()
        move_idx = s.index('move /Y "C:\\WinZapp\\update_install.log" "C:\\WinZapp\\update_install.log.old"')
        truncate_idx = s.index('> "C:\\WinZapp\\update_install.log" echo [WinZapp] update started')
        assert move_idx < truncate_idx, "must move the old log aside BEFORE truncating the current one"
        # Best-effort: a missing/locked file from the very first update ever
        # must not fail the script.
        move_line = s[move_idx:s.index("\n", move_idx)]
        assert ">NUL 2>&1" in move_line

    def test_a_locked_file_is_retried_once_before_giving_up(self):
        """Reported live: an update copied hundreds of files into the install
        directory and then died on a sharing violation — an on-access antivirus
        scan holding a .pyd xcopy had just written. WinZapp had already exited
        and its Node was killed, so nothing of ours held it; seconds later it
        was free. Without a retry that is a failed update over an install that
        has ALREADY been half-overwritten."""
        s = _script()
        first = s.index("xcopy /E /Y /I /H")
        retry = s.index("xcopy /E /Y /I /H", first + 1)
        assert s.index("if errorlevel 4", first) < retry, (
            "the second copy must be guarded by the first one's failure, not "
            "run unconditionally"
        )
        assert "timeout /t 5 /nobreak" in s[first:retry], (
            "retrying instantly retries the same lock"
        )

    def test_the_retry_is_the_same_idempotent_copy(self):
        """Repeating it is only safe because /E /Y /I /H overwrites what the
        first pass already wrote with the same bytes."""
        s = _script()
        copies = [line.strip() for line in s.splitlines()
                  if line.strip().startswith("xcopy ")]
        assert len(copies) == 2 and copies[0] == copies[1]

    def test_a_failed_copy_marks_it_and_keeps_the_evidence(self):
        s = _script()
        assert r'echo update failed > "C:\WinZapp\update_failed.marker"' in s
        # The LAST errorlevel-4 block is the verdict; the first is the retry.
        start = s.rindex("if errorlevel 4")
        failure_block = s[start:s.index(")\n", start)]
        assert "exit /b 1" in failure_block
        assert 'del "%~f0"' not in failure_block, (
            "deleting the script on failure erases the only evidence of what went wrong"
        )
        assert "xcopy /E" not in failure_block, (
            "the verdict block must not copy anything — it runs when the retry "
            "above has already failed"
        )

    def test_a_successful_copy_relaunches_and_cleans_up(self):
        s = _script()
        assert r'if exist "C:\WinZapp\WinZapp.exe" start "" "C:\WinZapp\WinZapp.exe"' in s
        assert s.rstrip().endswith('del "%~f0"')


class TestWritingTheScript:
    def test_an_ascii_script_is_written_as_ascii(self, tmp_path):
        bat = tmp_path / "u.bat"
        assert updater._write_installer_script(str(bat), _script()) is True
        assert bat.read_bytes().decode("ascii")

    @pytest.mark.skipif(not _oem_can_encode("\u00e9"), reason="OEM code page has no accents")
    def test_a_non_ascii_script_is_written_in_the_oem_code_page(self, tmp_path):
        """Not UTF-8 — that is the whole bug. cmd.exe reads the file in the
        OEM code page, so that is what it has to be written in."""
        bat = tmp_path / "u.bat"
        script = _script(install="C:\\Users\\Jos\u00e9\\WinZapp")

        assert updater._write_installer_script(str(bat), script) is True

        raw = bat.read_bytes()
        assert "Jos\u00e9" in raw.decode(_OEM), "cmd.exe must read the real path back"
        with pytest.raises(UnicodeDecodeError):
            raw.decode("ascii")

    def test_a_script_the_code_page_cannot_hold_is_refused(self, tmp_path, monkeypatch):
        """Refusing beats writing a corrupt script: the caller then reports a
        failed update instead of closing the app for an installer that would
        quietly do nothing — which is exactly what issue #83 looked like."""
        monkeypatch.setattr(updater, "_oem_encoding", lambda: "ascii")
        bat = tmp_path / "u.bat"

        assert updater._write_installer_script(
            str(bat), _script(install="C:\\Users\\Pawe\u0142\\WinZapp")
        ) is False

    def test_lines_end_crlf_so_cmd_can_parse_them(self, tmp_path):
        bat = tmp_path / "u.bat"
        updater._write_installer_script(str(bat), _script())
        assert b"\r\n" in bat.read_bytes()


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe only")
@pytest.mark.skipif(not _oem_can_encode("\u00e9"), reason="OEM code page has no accents")
class TestAgainstRealCmd:
    """The mechanism itself, proven with the real interpreter: same command,
    same path, same cmd.exe — only the encoding of the .bat differs."""

    def _run(self, tmp_path, encoding):
        target_dir = tmp_path / "Jos\u00e9"
        target_dir.mkdir()
        proof = target_dir / "ran.txt"
        bat = tmp_path / f"t_{encoding}.bat"
        with open(bat, "w", encoding=encoding, newline="\r\n") as f:
            f.write(f'@echo off\r\necho hello > "{proof}"\r\n')
        # _oem_encoding() answers for the SYSTEM default OEM code page
        # (GetOEMCP()). A plain subprocess.run() with no creation flags
        # inherits the calling process's (pytest's) own console/code page —
        # which matches GetOEMCP() on an ordinary machine, since nothing
        # changed it, but can differ on a CI runner whose parent console was
        # already switched to something else (observed on GitHub Actions'
        # Windows runner). CREATE_NO_WINDOW forces a fresh hidden console
        # using the system default instead, matching what production
        # actually gets (see the win32 install-launch branch of
        # updater.py — deliberately CREATE_NO_WINDOW only, no
        # DETACHED_PROCESS, which would remove the console entirely and
        # flip decoding to the ANSI code page instead; confirmed empirically
        # while diagnosing this same test).
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            ["cmd.exe", "/c", str(bat)], capture_output=True, creationflags=flags
        )
        return proof.is_file()

    def test_utf8_sends_the_command_to_a_path_that_does_not_exist(self, tmp_path):
        assert self._run(tmp_path, "utf-8") is False

    def test_the_oem_code_page_reaches_the_real_path(self, tmp_path):
        assert self._run(tmp_path, _OEM) is True
