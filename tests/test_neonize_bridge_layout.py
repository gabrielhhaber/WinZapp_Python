"""The bridge's folders written under the old (Portuguese) names keep working:
they are renamed in place, so an account paired before the rename does not have
to pair again."""

import os

import pytest

from core import neonize_bridge_layout as layout
from core import neonize_bridge_process as nb


@pytest.fixture(autouse=True)
def _data_under_tmp(tmp_path, monkeypatch):
    """A compiled bridge keeps its data under the global data folder of the install:
    keep it out of the working directory."""
    monkeypatch.setattr(nb, "global_dir", lambda *parts: os.path.join(str(tmp_path), "gdata", *parts))


def _legacy_bridge(tmp_path):
    root = tmp_path / "neonize_bridge"
    session = root / "dados-real" / "account1"
    session.mkdir(parents=True)
    (session / "sessao.db").write_bytes(b"keys")
    run = root / "execucao"
    (run / "clientes").mkdir(parents=True)
    (run / "clientes" / "100").write_text("100")
    (run / "ponte.pid").write_text("900\nt900")
    (run / "ponte.log").write_text("output")
    (run / "ponte.lock").write_text("")
    return str(root)


def test_the_legacy_folders_and_run_files_get_the_current_names(tmp_path):
    bridge = _legacy_bridge(tmp_path)

    moved = layout.migrate(bridge, running=False)

    assert moved == 6
    assert (tmp_path / "neonize_bridge" / "data" / "account1" / "sessao.db").read_bytes() == b"keys"
    run = tmp_path / "neonize_bridge" / "run"
    assert (run / "bridge.pid").read_text() == "900\nt900"
    assert (run / "bridge.log").read_text() == "output"
    assert (run / "clients" / "100").exists() and (run / "bridge.lock").exists()
    assert not (tmp_path / "neonize_bridge" / "dados-real").exists()
    assert not (tmp_path / "neonize_bridge" / "execucao").exists()


def test_nothing_is_renamed_while_a_bridge_is_running(tmp_path):
    bridge = _legacy_bridge(tmp_path)

    assert layout.migrate(bridge, running=True) == 0

    assert os.path.isdir(os.path.join(bridge, "dados-real"))
    assert os.path.isdir(os.path.join(bridge, "execucao"))


def test_a_folder_whose_new_name_exists_is_never_replaced(tmp_path):
    bridge = _legacy_bridge(tmp_path)
    os.makedirs(os.path.join(bridge, "data"))
    open(os.path.join(bridge, "data", "keep"), "w").close()

    layout.migrate(bridge, running=False)

    assert os.path.isfile(os.path.join(bridge, "dados-real", "account1", "sessao.db"))
    assert os.listdir(os.path.join(bridge, "data")) == ["keep"]


def test_a_rename_that_fails_leaves_the_old_folder_in_place(tmp_path):
    bridge = _legacy_bridge(tmp_path)

    def refuse(source, target):
        raise PermissionError(target)

    assert layout.migrate(bridge, running=False, rename=refuse) == 0
    assert os.path.isfile(os.path.join(bridge, "dados-real", "account1", "sessao.db"))


def test_a_fresh_install_has_nothing_to_rename(tmp_path):
    root = tmp_path / "neonize_bridge"
    root.mkdir()
    assert layout.migrate(str(root), running=False) == 0


def _make_venv_python(tmp_path):
    """The bridge virtualenv's interpreter where this system looks for it."""
    from core import neonize_bridge_process
    python = neonize_bridge_process.venv_python(str(tmp_path / "neonize_bridge"))
    os.makedirs(os.path.dirname(python))
    with open(python, "w") as stream:
        stream.write("")


def test_starting_the_bridge_renames_the_legacy_folders_first(tmp_path):
    bridge = _legacy_bridge(tmp_path)
    _make_venv_python(tmp_path)
    (tmp_path / "neonize_bridge" / "server.py").write_text("")

    class _Proc:
        pid, returncode = 900, None

        def poll(self):
            return None

    opened = iter([False, False, True])
    clock = iter(range(0, 10_000))
    handle = nb.NeonizeBridge(
        bridge, 6300, is_open=lambda port: next(opened, True), alive=lambda pid: False,
        created=lambda pid: "t900", rotate=lambda path: False,
        start_process=lambda cmd, env, cwd, output=None: _Proc(), kill=lambda pid: None,
        sleep=lambda s: None, clock=lambda: next(clock), my_pid=100,
        identity=lambda port: {"installation_id": "", "instance_id": "",
                               "protocol_version": 1, "pid": 900, "ppid": 1})

    assert handle.start() is True

    assert os.path.isfile(os.path.join(bridge, "data", "account1", "sessao.db"))
    assert not os.path.exists(os.path.join(bridge, "dados-real"))
    assert os.path.isfile(os.path.join(nb.run_dir(bridge), nb.BRIDGE_PID_FILE))


def _handle(bridge, *, alive, created, killed):
    class _Proc:
        pid, returncode = 900, None

        def poll(self):
            return None

    opened = iter([False, False, True])
    clock = iter(range(0, 10_000))
    return nb.NeonizeBridge(
        bridge, 6300, is_open=lambda port: next(opened, True), alive=alive,
        created=created, rotate=lambda path: False,
        start_process=lambda cmd, env, cwd, output=None: _Proc(), kill=killed.append,
        sleep=lambda s: None, clock=lambda: next(clock), my_pid=100,
        identity=lambda port: {"installation_id": "", "instance_id": "",
                               "protocol_version": 1, "pid": 900, "ppid": 1})


def _installed(tmp_path):
    bridge = _legacy_bridge(tmp_path)
    _make_venv_python(tmp_path)
    (tmp_path / "neonize_bridge" / "server.py").write_text("")
    return bridge


def test_a_reused_legacy_pid_does_not_block_the_rename(tmp_path):
    bridge, killed = _installed(tmp_path), []
    # the old ponte.pid names pid 900 (stamp t900), now another program with another stamp
    handle = _handle(bridge, alive=lambda pid: pid == 900,
                     created=lambda pid: "other", killed=killed)

    assert handle.start() is True

    assert os.path.isfile(os.path.join(bridge, "data", "account1", "sessao.db"))
    assert not os.path.exists(os.path.join(bridge, "dados-real"))
    assert killed == []


def test_a_legacy_bridge_still_running_without_clients_is_stopped_then_migrated(tmp_path):
    bridge, killed = _installed(tmp_path), []
    os.remove(os.path.join(bridge, "execucao", "clientes", "100"))
    handle = _handle(bridge, alive=lambda pid: pid == 900,
                     created=lambda pid: "t900", killed=killed)

    handle.start()

    assert 900 in killed
    assert os.path.isfile(os.path.join(bridge, "data", "account1", "sessao.db"))


def test_a_legacy_bridge_with_a_live_old_client_is_left_alone_and_nothing_starts(tmp_path):
    bridge, killed = _installed(tmp_path), []
    handle = _handle(bridge, alive=lambda pid: pid in (900, 100000),
                     created=lambda pid: "t900", killed=killed)
    with open(os.path.join(bridge, "execucao", "clientes", "100000"), "w") as stream:
        stream.write("100000")

    assert handle.start() is False

    assert killed == []
    assert os.path.isfile(os.path.join(bridge, "dados-real", "account1", "sessao.db"))
    assert not os.path.exists(os.path.join(bridge, "data"))


def test_an_empty_current_folder_does_not_strand_the_legacy_data(tmp_path):
    bridge = _legacy_bridge(tmp_path)
    os.makedirs(os.path.join(bridge, "data"))

    layout.migrate(bridge, running=False)

    assert os.path.isfile(os.path.join(bridge, "data", "account1", "sessao.db"))


# -- the bridge that ships inside a compiled install -----------------------

def _compiled_install(tmp_path):
    """dist/WinZapp/neonize_bridge (frozen) next to an older dist/neonize_bridge."""
    install = tmp_path / "dist" / "WinZapp"
    bridge = install / "neonize_bridge"
    bridge.mkdir(parents=True)
    (bridge / nb.PACKAGED_EXE).write_text("exe")
    older = tmp_path / "dist" / "neonize_bridge" / "data" / "account1"
    older.mkdir(parents=True)
    (older / "session.db").write_bytes(b"keys")
    return str(bridge), os.path.join(str(tmp_path), "gdata", "neonize_bridge", "data"), tmp_path / "dist"


def test_the_sessions_of_an_older_install_move_into_the_compiled_one(tmp_path):
    bridge, target, dist = _compiled_install(tmp_path)

    assert layout.adopt_sibling_data(bridge, target) == "moved"

    assert open(os.path.join(target, "account1", "session.db"), "rb").read() == b"keys"
    assert not (dist / "neonize_bridge" / "data").exists()


def test_a_sibling_with_the_portuguese_names_is_brought_along(tmp_path):
    bridge, target, dist = _compiled_install(tmp_path)
    (dist / "neonize_bridge" / "data").rename(dist / "neonize_bridge" / "dados-real")

    assert layout.adopt_sibling_data(bridge, target) == "moved"

    assert os.path.isfile(os.path.join(target, "account1", "session.db"))


def test_an_install_that_has_its_own_data_keeps_it(tmp_path):
    bridge, target, dist = _compiled_install(tmp_path)
    os.makedirs(os.path.join(target, "mine"))

    assert layout.adopt_sibling_data(bridge, target) == ""

    assert (dist / "neonize_bridge" / "data" / "account1" / "session.db").exists()


def test_an_empty_target_is_replaced_and_nothing_to_adopt_is_not_an_error(tmp_path):
    bridge, target, dist = _compiled_install(tmp_path)
    os.makedirs(target)
    assert layout.adopt_sibling_data(bridge, target) == "moved"
    assert layout.adopt_sibling_data(bridge, target) == ""        # nothing left to adopt


def test_a_move_that_fails_is_reported_and_leaves_the_data_where_it_was(tmp_path):
    bridge, target, dist = _compiled_install(tmp_path)

    def refuse(source, destination):
        raise PermissionError(destination)

    assert layout.adopt_sibling_data(bridge, target, move=refuse) == "failed"
    assert (dist / "neonize_bridge" / "data" / "account1" / "session.db").exists()


def test_a_compiled_bridge_keeps_its_data_and_run_files_outside_the_install_folder(tmp_path):
    bridge, target, _ = _compiled_install(tmp_path)
    assert nb.is_packaged(bridge)
    assert nb.data_dir(bridge) == target
    assert nb.run_dir(bridge).endswith(os.path.join("gdata", "neonize_bridge", "run"))
    assert not nb.is_packaged(str(tmp_path))
    assert nb.data_dir(str(tmp_path)) == str(tmp_path / "data")


# -- starting a compiled install next to an older one ------------------------

def _sibling_pid(dist, pid, stamp="t900", clients=()):
    run = dist / "neonize_bridge" / "run"
    (run / "clients").mkdir(parents=True, exist_ok=True)
    (run / "bridge.pid").write_text(f"{pid}\n{stamp}" if stamp else str(pid))
    for client in clients:
        (run / "clients" / str(client)).write_text(str(client))


def _start(bridge, *, alive, created=lambda pid: "t900", killed=None, spawned=None):
    class _Proc:
        pid, returncode = 900, None

        def poll(self):
            return None

    opened = iter([False, False, True])
    clock = iter(range(0, 10_000))
    spawned = [] if spawned is None else spawned
    handle = nb.NeonizeBridge(
        bridge, 6300, is_open=lambda port: next(opened, True), alive=alive, created=created,
        rotate=lambda path: False,
        start_process=lambda cmd, env, cwd, output=None: spawned.append(cmd) or _Proc(),
        kill=(killed if killed is not None else []).append, sleep=lambda s: None,
        clock=lambda: next(clock), my_pid=100,
        identity=lambda port: {"installation_id": "", "instance_id": "",
                               "protocol_version": 1, "pid": 900, "ppid": 1})
    return handle, spawned


def test_a_running_older_bridge_nobody_uses_is_stopped_then_its_data_moves(tmp_path):
    bridge, target, dist = _compiled_install(tmp_path)
    _sibling_pid(dist, 5000)
    killed = []
    handle, spawned = _start(bridge, alive=lambda pid: pid == 5000, killed=killed)

    assert handle.start() is True

    assert killed == [5000]
    assert open(os.path.join(target, "account1", "session.db"), "rb").read() == b"keys"


def test_an_older_bridge_an_old_client_still_uses_is_left_alone_and_nothing_starts(tmp_path):
    bridge, target, dist = _compiled_install(tmp_path)
    _sibling_pid(dist, 5000, clients=[777])
    killed = []
    handle, spawned = _start(bridge, alive=lambda pid: pid in (5000, 777), killed=killed)

    assert handle.start() is False

    assert killed == [] and spawned == []
    assert (dist / "neonize_bridge" / "data" / "account1" / "session.db").exists()
    assert not os.path.exists(target)
    assert not os.path.exists(os.path.join(nb.run_dir(bridge), "bridge.pid"))


def test_an_older_bridge_that_cannot_be_verified_is_never_killed(tmp_path):
    bridge, target, dist = _compiled_install(tmp_path)
    _sibling_pid(dist, 5000, stamp="")
    killed = []
    handle, spawned = _start(bridge, alive=lambda pid: pid == 5000, killed=killed)

    assert handle.start() is False
    assert killed == [] and spawned == []
    assert (dist / "neonize_bridge" / "data" / "account1" / "session.db").exists()


def test_a_reused_pid_of_the_older_bridge_does_not_block_the_move(tmp_path):
    bridge, target, dist = _compiled_install(tmp_path)
    _sibling_pid(dist, 5000, stamp="t900")
    killed = []
    handle, _ = _start(bridge, alive=lambda pid: pid == 5000,
                       created=lambda pid: "someone-else", killed=killed)

    assert handle.start() is True
    assert killed == []
    assert os.path.isfile(os.path.join(target, "account1", "session.db"))


def test_the_move_never_copies_when_the_rename_cannot_happen(tmp_path):
    """A copy-and-delete fallback would leave a stale copy for the new bridge."""
    bridge, target, dist = _compiled_install(tmp_path)

    def locked(source, destination):
        raise PermissionError(destination)

    assert layout.adopt_sibling_data(bridge, target, move=locked) == "failed"
    assert not os.path.exists(target)
    assert (dist / "neonize_bridge" / "data" / "account1" / "session.db").exists()
    import inspect
    assert inspect.signature(layout.adopt_sibling_data).parameters["move"].default is os.rename


def test_a_compiled_install_finds_its_bridge_inside_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(nb.sys, "frozen", True, raising=False)
    monkeypatch.setattr(nb, "resource_path", lambda *parts: os.path.join(str(tmp_path), *parts))
    assert nb.bridge_dir() == os.path.join(str(tmp_path), "neonize_bridge")
    monkeypatch.setattr(nb.sys, "frozen", False)
    assert nb.bridge_dir().endswith("neonize_bridge")


def test_a_rename_refused_for_a_moment_is_tried_again(tmp_path):
    bridge, target, dist = _compiled_install(tmp_path)
    attempts, pauses = [], []

    def late(source, destination):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError(destination)         # the stopped bridge still holds its files
        os.rename(source, destination)

    assert layout.adopt_sibling_data(bridge, target, move=late, sleep=pauses.append) == "moved"
    assert len(attempts) == 3 and len(pauses) == 2
    assert os.path.isfile(os.path.join(target, "account1", "session.db"))


def test_a_rename_that_keeps_being_refused_gives_up_and_leaves_the_data(tmp_path):
    bridge, target, dist = _compiled_install(tmp_path)

    def locked(source, destination):
        raise PermissionError(destination)

    assert layout.adopt_sibling_data(bridge, target, move=locked, sleep=lambda s: None) == "failed"
    assert (dist / "neonize_bridge" / "data" / "account1" / "session.db").exists()
