"""The update installer only ever kills WinZapp's own Node, on exactly its port.

It used to run `netstat -aon | findstr :6300 | findstr LISTENING` and
taskkill whatever PID came out, plus the same for :5433. findstr matches a
substring, so a program on 63000-63009 or 54330-54339 was killed too, and
5433 was a PostgreSQL nothing in WinZapp starts any more. See
client/update_node_kill.py.
"""

import os
import re

import pytest

import updater
from update_node_kill import listening_pids, node_kill_lines, own_node_pids

NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1000
  TCP    0.0.0.0:6300           0.0.0.0:0              LISTENING       777
  TCP    0.0.0.0:63001          0.0.0.0:0              LISTENING       2001
  TCP    0.0.0.0:16300          0.0.0.0:0              LISTENING       2002
  TCP    127.0.0.1:6300         127.0.0.1:50123        ESTABLISHED     777
  TCP    127.0.0.1:50123        127.0.0.1:6300         ESTABLISHED     4242
  TCP    0.0.0.0:5433           0.0.0.0:0              LISTENING       3000
  TCP    [::]:6300              [::]:0                 LISTENING       777
  UDP    0.0.0.0:6300           *:*                                    2003
"""


class TestExactPortMatching:
    def test_only_listeners_on_exactly_the_port(self):
        assert listening_pids(NETSTAT, 6300) == [777]

    def test_a_neighbouring_port_is_not_the_port(self):
        assert listening_pids(NETSTAT, 63001) == [2001]
        assert listening_pids(NETSTAT, 630) == []

    def test_an_ipv6_only_listener_is_found(self):
        out = "  TCP    [::]:6300              [::]:0                 LISTENING       888\n"
        assert listening_pids(out, 6300) == [888]

    def test_a_localized_state_column_does_not_matter(self):
        """netstat translates LISTENING (ABHÖREN, OUVINDO, ...); the old
        findstr LISTENING found nothing on those systems."""
        out = ("  TCP    0.0.0.0:6300           0.0.0.0:0              ABHÖREN         777\n"
               "  TCP    0.0.0.0:6301           0.0.0.0:0              OUVINDO         778\n")
        assert listening_pids(out, 6300) == [777]
        assert listening_pids(out, 6301) == [778]


class TestOnlyWinZappsNode:
    def test_a_listener_running_another_executable_is_left_alone(self, tmp_path):
        ours = tmp_path / "WinZapp" / "node" / "node.exe"
        ours.parent.mkdir(parents=True)
        ours.write_bytes(b"node")
        theirs = tmp_path / "nvm" / "node.exe"
        theirs.parent.mkdir()
        theirs.write_bytes(b"node")
        out = ("  TCP    0.0.0.0:6300           0.0.0.0:0              LISTENING       777\n"
               "  TCP    [::]:6300              [::]:0                 LISTENING       778\n")
        paths = {777: str(ours), 778: str(theirs)}
        assert own_node_pids(6300, [str(ours)], netstat_output=out,
                             image_path=paths.get) == [777]

    def test_the_same_file_under_another_spelling_still_matches(self, tmp_path):
        """Compared by the filesystem: an 8.3 or differently-cased path to
        WinZapp's node.exe is still WinZapp's node.exe."""
        ours = tmp_path / "WinZapp" / "node" / "node.exe"
        ours.parent.mkdir(parents=True)
        ours.write_bytes(b"node")
        # A hard link: another name for the same file, as an 8.3 alias is,
        # and creatable on Windows without the privilege symlinks need.
        alias = tmp_path / "WINZAP~1.EXE"
        os.link(ours, alias)
        out = "  TCP    0.0.0.0:6300           0.0.0.0:0              LISTENING       777\n"
        assert own_node_pids(6300, [str(alias)], netstat_output=out,
                             image_path=lambda pid: str(ours)) == [777]

    def test_an_unreadable_process_is_not_ours(self):
        out = "  TCP    0.0.0.0:6300           0.0.0.0:0              LISTENING       4\n"
        assert own_node_pids(6300, [r"C:\WinZapp\node\node.exe"], netstat_output=out,
                             image_path=lambda pid: "") == []

    def test_off_windows_nothing_is_looked_up(self, monkeypatch):
        import update_node_kill
        monkeypatch.setattr(update_node_kill.sys, "platform", "darwin")
        assert own_node_pids(6300, ["x"]) == []


def _script(**kw):
    return updater._build_installer_script(
        r"C:\tmp\ext", r"C:\WinZapp", r"C:\WinZapp\WinZapp.exe",
        r"C:\WinZapp\update_install.log", r"C:\WinZapp\update_failed.marker",
        pid=4242, api_port=6300, **kw,
    )


class TestTheInstallerScript:
    def test_no_substring_match_and_no_postgres_port(self):
        s = _script(node_pids=[777])
        assert "findstr" not in s
        assert "5433" not in s

    def test_the_kill_rechecks_the_exact_port_and_the_image(self):
        """WinZapp's shutdown normally stops Node first and Windows reuses
        pids, so the script checks again before it kills."""
        s = _script(node_pids=[777])
        assert '"!WZ_PID!"=="777"' in s
        assert '"!WZ_LOCAL:~-5!"==":6300"' in s
        assert '"%%c"=="0.0.0.0:0"' in s and '"%%c"=="[::]:0"' in s
        assert 'tasklist /FI "PID eq 777" /FI "IMAGENAME eq node.exe"' in s
        assert s.index('IMAGENAME eq node.exe') < s.index("taskkill /F /PID 777")
        # After every account has exited, before anything is copied.
        assert s.index('"PID eq 4242"') < s.index("taskkill /F /PID 777") < s.index("xcopy")

    def test_the_suffix_length_follows_the_port(self):
        assert '"!WZ_LOCAL:~-6!"==":12345"' in node_kill_lines([9], 12345, "log")

    def test_nothing_of_ours_listening_means_nothing_is_killed(self):
        s = _script(node_pids=[])
        assert "taskkill" not in s and "netstat" not in s

    def test_run_batch_installer_kills_only_what_own_node_pids_found(self, tmp_path, monkeypatch):
        install_dir = tmp_path / "install"
        install_dir.mkdir()
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        monkeypatch.setattr(updater, "log_path", lambda *p: str(tmp_path / "logs" / os.path.join(*p)))
        monkeypatch.setattr(updater, "_needs_admin", lambda: False)
        monkeypatch.setattr(updater.sys, "platform", "win32")
        monkeypatch.setattr(updater.subprocess, "Popen", lambda *a, **kw: None)
        asked = {}

        def _own(port, node_paths):
            asked.update(port=port, paths=list(node_paths))
            return [555]

        monkeypatch.setattr(updater, "own_node_pids", _own)
        seen = {}
        monkeypatch.setattr(updater, "_write_installer_script",
                            lambda path, script: seen.setdefault("script", script) and True)

        assert updater._run_batch_installer(str(extracted), str(install_dir), "WinZapp.exe",
                                            pid=1234, api_port=6311) is True
        assert asked["port"] == 6311
        assert os.path.join(str(install_dir), "node", "node.exe") in asked["paths"]
        assert "taskkill /F /PID 555" in seen["script"]
        assert '":6311"' in seen["script"]


def _cmd_for_f_rest(line):
    """How cmd.exe splits *line* for `for /f "tokens=1-3,*"` with the
    default delimiters (space, tab): the first three tokens, then the rest
    of the line after the delimiters that follow the third."""
    rest, tokens = line, []
    for _ in range(3):
        rest = rest.lstrip(" \t")
        word = re.match(r"[^ \t]*", rest).group()
        tokens.append(word)
        rest = rest[len(word):]
    return tokens, rest.lstrip(" \t")


def _cmd_last_word(rest):
    """`for %%p in (%%d) do set "WZ_PID=%%p"`: a plain for splits on
    space, tab, comma, semicolon and =; the last word is what remains."""
    words = [w for w in re.split(r"[ \t,;=]+", rest) if w]
    return words[-1] if words else ""


class TestTheScriptReadsThePidRegardlessOfTheStateColumn:
    """Reviewed against a translated netstat: a two-word state shifted the
    PID out of token 5, so the old tokens=5 read nothing and the locked
    node.exe made xcopy fail."""

    LINES = [
        "  TCP    0.0.0.0:6300           0.0.0.0:0              LISTENING       777",
        "  TCP    0.0.0.0:6300           0.0.0.0:0              EN ESCUCHA      778",
        "  TCP    [::]:6300              [::]:0                 ABHÖREN         779",
        "  TCP    [::]:6300              [::]:0                 EM ESCUTA       780",
    ]

    def test_the_script_takes_the_last_field_of_the_rest(self):
        block = node_kill_lines([777], 6300, "log")
        assert 'for /f "tokens=1-3,*" %%a in (\'netstat -ano\')' in block
        assert 'for %%p in (%%d) do set "WZ_PID=%%p"' in block

    @pytest.mark.parametrize("line,pid", list(zip(LINES, ["777", "778", "779", "780"])))
    def test_each_sample_line_yields_its_pid_and_listening_socket(self, line, pid):
        tokens, rest = _cmd_for_f_rest(line)
        assert tokens[0] == "TCP"
        assert tokens[1].endswith(":6300")
        assert tokens[2] in ("0.0.0.0:0", "[::]:0")
        assert _cmd_last_word(rest) == pid

    def test_python_agrees_on_every_sample(self):
        assert listening_pids("\n".join(self.LINES), 6300) == [777, 778, 779, 780]
