"""Log directory hygiene: one folder, and no files that say nothing.

Two separate reports, same area — data/log/ folding into data/logs/, and
stdout.log/stderr.log no longer being created until something is written.

stdout.log and stderr.log were written to data/log/ while log.log,
shutdown_audit.log and wppconnect.log went to data/logs/. Nothing
distinguished the two — create_basic_files() simply hardcoded a path that
predated log_path(), and it was the only writer that did not use it. The names
differ by one letter, in a pair of sibling directories a user is asked to find
and send when something breaks.

The fold has to be non-destructive: an install that has already run since the
change can have content on both sides, and losing the old stderr output is
losing exactly the evidence these files exist for.
"""

import os

import pytest

import main as main_module


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """Point data_path()/log_path() at a temp account layout."""
    data_root = tmp_path / "data"
    logs_dir = data_root / "logs"
    legacy_dir = data_root / "log"
    logs_dir.mkdir(parents=True)

    import app_paths

    monkeypatch.setattr(
        app_paths, "data_path",
        lambda *parts: str(data_root.joinpath(*parts)) if parts else str(data_root),
    )
    monkeypatch.setattr(
        app_paths, "log_path",
        lambda *parts: str(logs_dir.joinpath(*parts)) if parts else str(logs_dir),
    )
    return legacy_dir, logs_dir


class TestNothingToDo:
    def test_no_legacy_dir_is_a_no_op(self, dirs):
        legacy, logs = dirs
        main_module._consolidate_legacy_log_dir()
        assert not legacy.exists()
        assert list(logs.iterdir()) == []

    def test_running_twice_is_harmless(self, dirs):
        legacy, logs = dirs
        legacy.mkdir()
        (legacy / "stdout.log").write_bytes(b"uma vez")

        main_module._consolidate_legacy_log_dir()
        main_module._consolidate_legacy_log_dir()

        assert (logs / "stdout.log").read_bytes() == b"uma vez"


class TestTheFold:
    def test_files_move_to_logs(self, dirs):
        legacy, logs = dirs
        legacy.mkdir()
        (legacy / "stdout.log").write_bytes(b"saida")
        (legacy / "stderr.log").write_bytes(b"erros")

        main_module._consolidate_legacy_log_dir()

        assert (logs / "stdout.log").read_bytes() == b"saida"
        assert (logs / "stderr.log").read_bytes() == b"erros"

    def test_the_old_directory_is_removed(self, dirs):
        legacy, logs = dirs
        legacy.mkdir()
        (legacy / "stdout.log").write_bytes(b"saida")

        main_module._consolidate_legacy_log_dir()

        assert not legacy.exists()

    def test_existing_content_on_the_new_side_is_appended_to_not_replaced(self, dirs):
        """Both sides can hold real output. Overwriting either one throws away
        evidence the user may be about to send."""
        legacy, logs = dirs
        legacy.mkdir()
        (logs / "stderr.log").write_bytes(b"novo\n")
        (legacy / "stderr.log").write_bytes(b"antigo\n")

        main_module._consolidate_legacy_log_dir()

        assert (logs / "stderr.log").read_bytes() == b"novo\nantigo\n"

    def test_an_empty_legacy_file_does_not_touch_the_target(self, dirs):
        legacy, logs = dirs
        legacy.mkdir()
        (logs / "stdout.log").write_bytes(b"conteudo")
        (legacy / "stdout.log").write_bytes(b"")

        main_module._consolidate_legacy_log_dir()

        assert (logs / "stdout.log").read_bytes() == b"conteudo"
        assert not legacy.exists()

    def test_subdirectories_are_left_alone_and_stop_the_removal(self, dirs):
        """Only files are folded. Anything unexpected stays put rather than
        being deleted, and the directory then legitimately survives."""
        legacy, logs = dirs
        legacy.mkdir()
        (legacy / "stdout.log").write_bytes(b"saida")
        (legacy / "algo").mkdir()

        main_module._consolidate_legacy_log_dir()

        assert (logs / "stdout.log").read_bytes() == b"saida"
        assert (legacy / "algo").is_dir()


class TestAFileThatCannotBeMoved:
    def test_the_app_still_starts(self, dirs, monkeypatch):
        """A stale handle on Windows must not turn into a startup failure."""
        legacy, logs = dirs
        legacy.mkdir()
        (legacy / "stderr.log").write_bytes(b"preso")

        def _boom(path):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(main_module.os, "remove", _boom)

        main_module._consolidate_legacy_log_dir()   # must not raise

        assert legacy.exists()


class TestLazyLogFile:
    """stdout.log/stderr.log are crash sinks: most runs write nothing to them.

    Creating them up front left two permanently empty files in logs/ that look
    like they ought to contain something — it invites opening them to look for
    the problem, and makes "is there anything in the logs?" impossible to
    answer without checking sizes.
    """

    def test_nothing_written_means_no_file(self, tmp_path):
        path = tmp_path / "logs" / "stderr.log"
        main_module._LazyLogFile(str(path))
        assert not path.exists()

    def test_an_empty_write_does_not_create_the_file(self, tmp_path):
        """print() emits a separate empty write; that must not count."""
        path = tmp_path / "logs" / "stdout.log"
        sink = main_module._LazyLogFile(str(path))
        sink.write("")
        assert not path.exists()

    def test_the_first_real_write_creates_it(self, tmp_path):
        path = tmp_path / "logs" / "stderr.log"
        sink = main_module._LazyLogFile(str(path))
        sink.write("Traceback (most recent call last):\n")
        assert path.read_text(encoding="utf-8").startswith("Traceback")

    def test_the_directory_is_created_if_missing(self, tmp_path):
        path = tmp_path / "nao" / "existe" / "stderr.log"
        main_module._LazyLogFile(str(path)).write("erro")
        assert path.is_file()

    def test_writes_accumulate_in_one_handle(self, tmp_path):
        path = tmp_path / "logs" / "stdout.log"
        sink = main_module._LazyLogFile(str(path))
        sink.write("uma\n")
        sink.write("duas\n")
        assert path.read_text(encoding="utf-8") == "uma\nduas\n"

    def test_it_appends_rather_than_truncating(self, tmp_path):
        path = tmp_path / "logs" / "stderr.log"
        path.parent.mkdir()
        path.write_text("anterior\n", encoding="utf-8")
        main_module._LazyLogFile(str(path)).write("novo\n")
        assert path.read_text(encoding="utf-8") == "anterior\nnovo\n"

    def test_a_write_that_fails_does_not_raise(self, tmp_path, monkeypatch):
        """This stands in for sys.stderr. A sink that raises turns any stray
        print() into a crash — losing the line is the lesser failure."""
        sink = main_module._LazyLogFile(str(tmp_path / "logs" / "stderr.log"))
        monkeypatch.setattr(
            main_module.os, "makedirs",
            lambda *a, **k: (_ for _ in ()).throw(OSError(13, "denied")),
        )
        assert sink.write("algo") == 0

    def test_it_looks_enough_like_a_stream(self, tmp_path):
        """Assigned to sys.stdout/sys.stderr, so libraries probe it."""
        sink = main_module._LazyLogFile(str(tmp_path / "logs" / "stdout.log"))
        assert sink.isatty() is False
        assert sink.writable() is True
        assert sink.readable() is False
        assert sink.closed is False
        sink.flush()          # must not raise before anything is written
        sink.writelines(["a\n", "b\n"])
        with pytest.raises(Exception):
            sink.fileno()     # no real descriptor exists; must not invent one
