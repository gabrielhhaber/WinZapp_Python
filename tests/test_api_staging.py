"""A WPPConnect Server built next to the running one is swapped in safely.

With "download updates in the background" on, the new server is built in
api_staging/ while the old one keeps running, and installing it is two
directory renames with the server stopped (core/api_staging.py). The rule the
swap exists to keep: whatever goes wrong, api/ is a server that runs — the
worst outcome is "not updated", never "no server", which is what the in-place
update risks every time it wipes api/ before building.
"""

import os
from collections import namedtuple

import pytest

from core import api_staging
from core.api_staging import (
    SwapError,
    has_room_for_staging,
    replaced_dir_for,
    staging_dir_for,
    swap_in_staged_api,
)


def _server(path, marker, built=True, **extra):
    """A server tree: dist/server.js says which build it is."""
    os.makedirs(os.path.join(path, "dist"), exist_ok=True)
    if built:
        with open(os.path.join(path, "dist", "server.js"), "w") as f:
            f.write(marker)
    for name, content in extra.items():
        target = os.path.join(path, name)
        if content is None:
            os.makedirs(target, exist_ok=True)
            with open(os.path.join(target, "session.json"), "w") as f:
                f.write(marker)
        else:
            with open(target, "w") as f:
                f.write(content)
    return path


def _build(path):
    with open(os.path.join(path, "dist", "server.js")) as f:
        return f.read()


@pytest.fixture
def dirs(tmp_path):
    api = str(tmp_path / "api")
    return api, staging_dir_for(api)


class TestWhereItIsBuilt:
    def test_next_to_the_server_on_the_same_volume(self, tmp_path):
        api = str(tmp_path / "api")
        assert staging_dir_for(api) == api + "_staging"
        assert replaced_dir_for(api) == api + "_old"
        assert os.path.dirname(staging_dir_for(api)) == os.path.dirname(api)

    def test_a_trailing_separator_does_not_put_it_inside_the_server(self, tmp_path):
        api = str(tmp_path / "api")
        assert staging_dir_for(api + os.sep) == api + "_staging"


class TestRoomForASecondServer:
    _Usage = namedtuple("_Usage", "total used free")

    def _free(self, gigabytes):
        return lambda path: self._Usage(0, 0, int(gigabytes * 1024 ** 3))

    def test_enough(self, tmp_path):
        assert has_room_for_staging(str(tmp_path / "api"), disk_usage=self._free(10)) is True

    def test_not_enough(self, tmp_path):
        assert has_room_for_staging(str(tmp_path / "api"), disk_usage=self._free(1)) is False

    def test_the_volume_asked_about_is_the_servers(self, tmp_path):
        seen = []

        def _usage(path):
            seen.append(path)
            return self._Usage(0, 0, 10 * 1024 ** 3)

        has_room_for_staging(str(tmp_path / "api"), disk_usage=_usage)
        assert seen == [str(tmp_path)]

    def test_when_it_cannot_be_told_the_update_is_done_in_place(self, tmp_path):
        def _broken(path):
            raise OSError("no such drive")

        assert has_room_for_staging(str(tmp_path / "api"), disk_usage=_broken) is False


class TestTheSwap:
    def test_the_new_server_takes_the_place_of_the_old(self, dirs):
        api, staged = dirs
        _server(api, "old")
        _server(staged, "new")

        replaced = swap_in_staged_api(api, staged, pause=0)

        assert _build(api) == "new"
        assert not os.path.exists(staged)
        assert replaced == replaced_dir_for(api) and _build(replaced) == "old"

    def test_what_belongs_to_the_install_follows_it(self, dirs):
        """Tokens, the log and .env are per-install state inside api/."""
        api, staged = dirs
        _server(api, "old", wppconnect_tokens=None, **{".env": "PORT=1", "wppconnect.log": "log"})
        _server(staged, "new")

        swap_in_staged_api(api, staged, pause=0)

        assert os.path.isfile(os.path.join(api, "wppconnect_tokens", "session.json"))
        assert open(os.path.join(api, ".env")).read() == "PORT=1"
        assert open(os.path.join(api, "wppconnect.log")).read() == "log"

    def test_the_old_servers_code_does_not_follow(self, dirs):
        api, staged = dirs
        _server(api, "old", **{"package.json": "old"})
        _server(staged, "new", **{"package.json": "new"})
        os.makedirs(os.path.join(api, "node_modules", "left-behind"))

        swap_in_staged_api(api, staged, pause=0)

        assert open(os.path.join(api, "package.json")).read() == "new"
        assert not os.path.exists(os.path.join(api, "node_modules", "left-behind"))

    def test_a_file_the_new_build_already_has_is_not_overwritten(self, dirs):
        api, staged = dirs
        _server(api, "old", **{".env": "old"})
        _server(staged, "new", **{".env": "new"})

        swap_in_staged_api(api, staged, pause=0)

        assert open(os.path.join(api, ".env")).read() == "new"

    def test_a_first_install_has_nothing_to_replace(self, dirs):
        api, staged = dirs
        _server(staged, "new")

        assert swap_in_staged_api(api, staged, pause=0) == ""
        assert _build(api) == "new"

    def test_a_leftover_from_an_earlier_swap_is_cleared_first(self, dirs):
        api, staged = dirs
        _server(api, "old")
        _server(staged, "new")
        _server(replaced_dir_for(api), "ancient")

        replaced = swap_in_staged_api(api, staged, pause=0)

        assert _build(replaced) == "old"


class TestTheSwapNeverLeavesNoServer:
    def test_an_unbuilt_staging_directory_is_refused(self, dirs):
        api, staged = dirs
        _server(api, "old")
        _server(staged, "new", built=False)

        with pytest.raises(SwapError):
            swap_in_staged_api(api, staged, pause=0)

        assert _build(api) == "old"
        assert not os.path.exists(replaced_dir_for(api))

    def test_a_server_that_cannot_be_moved_aside_stays(self, dirs, monkeypatch):
        api, staged = dirs
        _server(api, "old")
        _server(staged, "new")

        def _locked(source, target):
            raise PermissionError("in use")

        monkeypatch.setattr(api_staging.os, "replace", _locked)

        with pytest.raises(SwapError):
            swap_in_staged_api(api, staged, attempts=2, pause=0)

        assert _build(api) == "old" and _build(staged) == "new"

    def test_the_old_server_is_put_back_when_the_new_one_cannot_move_in(self, dirs, monkeypatch):
        api, staged = dirs
        _server(api, "old")
        _server(staged, "new")
        real = os.replace

        def _replace(source, target):
            if source == staged:
                raise PermissionError("in use")
            return real(source, target)

        monkeypatch.setattr(api_staging.os, "replace", _replace)

        with pytest.raises(SwapError):
            swap_in_staged_api(api, staged, attempts=2, pause=0)

        assert _build(api) == "old"
        assert not os.path.exists(replaced_dir_for(api))

    def test_a_file_held_for_a_moment_is_retried(self, dirs, monkeypatch):
        """Right after Node and Chrome exit, an antivirus can still hold one."""
        api, staged = dirs
        _server(api, "old")
        _server(staged, "new")
        real, failures = os.replace, [PermissionError("scan"), PermissionError("scan")]

        def _replace(source, target):
            if failures:
                raise failures.pop()
            return real(source, target)

        monkeypatch.setattr(api_staging.os, "replace", _replace)

        swap_in_staged_api(api, staged, attempts=5, pause=0)

        assert _build(api) == "new"


class TestDiscard:
    def test_it_removes_a_tree_and_ignores_what_is_not_there(self, tmp_path):
        target = _server(str(tmp_path / "api_staging"), "x")
        api_staging.discard(target)
        api_staging.discard(target)
        api_staging.discard("")
        assert not os.path.exists(target)

    @pytest.mark.skipif(os.name != "nt", reason="only Windows refuses to delete a read-only file")
    def test_it_removes_read_only_files(self, tmp_path):
        """npm leaves read-only files in node_modules; Windows refuses to
        delete them, and ignore_errors hid that until the swap tripped on it."""
        import stat
        target = _server(str(tmp_path / "api_old"), "x")
        os.chmod(os.path.join(target, "dist", "server.js"), stat.S_IREAD)

        api_staging.discard(target)

        assert not os.path.exists(target)

    def test_a_read_only_file_is_made_writable_and_deleted_on_any_system(self, tmp_path, monkeypatch):
        """The Windows refusal, simulated: the delete fails while the write bit
        is off. Runs everywhere, as root too, because it checks the mode bits
        itself rather than relying on the platform."""
        import stat
        target = _server(str(tmp_path / "api_old"), "x")
        locked = os.path.join(target, "dist", "server.js")
        os.chmod(locked, stat.S_IREAD)

        def _remove(path):
            if not os.stat(path).st_mode & stat.S_IWRITE:
                raise PermissionError(path)
            os.remove(path)

        def _rmtree(path, onexc):
            onexc(_remove, locked, PermissionError(locked))

        monkeypatch.setattr(api_staging.shutil, "rmtree", _rmtree)

        api_staging.discard(target)

        assert not os.path.exists(locked)

    def test_it_does_not_raise_when_the_removal_blows_up(self, tmp_path, monkeypatch):
        target = _server(str(tmp_path / "api_old"), "x")

        def _boom(*args, **kwargs):
            raise ValueError("odd")

        monkeypatch.setattr(api_staging.shutil, "rmtree", _boom)
        api_staging.discard(target)


class TestLongPath:
    def test_a_plain_windows_path_gets_the_extended_prefix(self, monkeypatch):
        bs = chr(92)
        monkeypatch.setattr(api_staging.os, "name", "nt")
        monkeypatch.setattr(api_staging.os.path, "abspath", lambda p: p)
        plain = bs.join(["D:", "WinzApp", "api_old"])
        prefix = bs * 2 + "?" + bs
        assert api_staging._long_path(plain) == prefix + plain
        assert api_staging._long_path(prefix + plain) == prefix + plain
        unc = bs * 2 + bs.join(["srv", "share", "x"])
        assert api_staging._long_path(unc) == prefix + "UNC" + bs + unc[2:]


class TestALeftoverThatCannotBeDeleted:
    def test_it_is_moved_aside_so_the_update_still_goes_through(self, dirs, monkeypatch):
        api, staged = dirs
        _server(api, "old")
        _server(staged, "new")
        _server(replaced_dir_for(api), "stuck")
        monkeypatch.setattr(api_staging, "discard", lambda path: None)

        replaced = swap_in_staged_api(api, staged, pause=0)

        assert _build(api) == "new" and _build(replaced) == "old"
        aside = api_staging.stale_dirs_for(api)
        assert len(aside) == 1 and _build(aside[0]) == "stuck"

    def test_the_next_update_sweeps_what_was_moved_aside(self, dirs):
        api, staged = dirs
        stale = _server(replaced_dir_for(api) + ".stale-1", "stuck")
        _server(replaced_dir_for(api), "old")

        leftovers = api_staging.leftovers_for(api)

        assert {staged, replaced_dir_for(api), stale} <= set(leftovers)
        assert api not in leftovers

    def test_only_what_was_moved_aside_is_safe_to_delete_at_startup(self, dirs):
        """api_old and api_staging may belong to an update in progress."""
        api, staged = dirs
        stale = _server(replaced_dir_for(api) + ".stale-1", "stuck")
        _server(replaced_dir_for(api), "old")
        _server(staged, "new")

        assert api_staging.stale_dirs_for(api) == [stale]

    def test_when_it_cannot_be_moved_aside_either_the_swap_is_refused(self, dirs, monkeypatch):
        api, staged = dirs
        _server(api, "old")
        _server(staged, "new")
        _server(replaced_dir_for(api), "stuck")
        monkeypatch.setattr(api_staging, "discard", lambda path: None)

        def _locked(source, target):
            raise PermissionError("in use")

        monkeypatch.setattr(api_staging.os, "replace", _locked)

        with pytest.raises(SwapError):
            swap_in_staged_api(api, staged, attempts=1, pause=0)

        assert _build(api) == "old"
