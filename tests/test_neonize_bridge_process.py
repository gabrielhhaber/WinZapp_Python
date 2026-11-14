"""Starting and stopping the neonize bridge: the rules, with no real process."""

import os

from core import neonize_bridge_process as nb


class _Proc:
    def __init__(self, pid=900, exits=None):
        self.pid, self._exits, self.returncode = pid, exits, exits

    def poll(self):
        return self._exits


def _bridge(tmp_path, with_python=True):
    root = tmp_path / "neonize_bridge"
    (root / ".venv" / "Scripts").mkdir(parents=True)
    if with_python:
        (root / ".venv" / "Scripts" / "python.exe").write_text("")
        (root / "server.py").write_text("")
    return str(root)


def _with_secret(bridge):
    """A bridge folder whose bridge wrote its secret: the one this install owns (or
    that was started by hand with this --folder)."""
    folder = nb.data_dir(bridge)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, nb.SECRET_FILE), "w", encoding="utf-8") as stream:
        stream.write("secret")
    return bridge


def _bridge_says(pid=900, ppid=1):
    """What a real bridge answers at /winzapp/identity."""
    return lambda port: {"installation_id": "", "instance_id": "",
                         "protocol_version": 1, "pid": pid, "ppid": ppid}


def _handle(bridge, *, port_open=False, alive=lambda pid: False, spawned=None,
            killed=None, proc=None, my_pid=100, created=lambda pid: f"t{pid}",
            rotate=lambda path: False, identity=None,
            shutdown=lambda port, secret: False,
            node_listeners=lambda port: [], node_users=lambda pid: []):
    spawned = [] if spawned is None else spawned
    killed = [] if killed is None else killed

    def start_process(cmd, env, cwd, output=None):
        spawned.append((cmd, cwd))
        return proc or _Proc()

    clock = iter(range(0, 10_000))
    return nb.NeonizeBridge(
        bridge, 6300, is_open=lambda port: port_open() if callable(port_open) else port_open,
        alive=alive, created=created, rotate=rotate, start_process=start_process, kill=killed.append,
        sleep=lambda s: None, clock=lambda: next(clock), my_pid=my_pid,
        identity=identity or _bridge_says(), shutdown=shutdown,
        node_listeners=node_listeners, node_users=node_users)


def test_the_command_uses_the_bridge_virtualenv_and_the_fixed_port(tmp_path):
    bridge = _bridge(tmp_path)
    cmd = nb.command(bridge)
    assert cmd[0].endswith("python.exe") and cmd[1].endswith("server.py")
    assert cmd[cmd.index("--port") + 1] == "6300"
    assert cmd[cmd.index("--folder") + 1] == nb.data_dir(bridge)


def test_no_command_when_the_install_has_no_bridge(tmp_path):
    assert nb.command(_bridge(tmp_path, with_python=False)) is None


def test_a_packaged_bridge_exe_wins(tmp_path):
    bridge = _bridge(tmp_path)
    open(os.path.join(bridge, "neonize_bridge.exe"), "w").close()
    assert nb.command(bridge)[0].endswith("neonize_bridge.exe")


def test_an_open_port_is_adopted_without_spawning(tmp_path):
    spawned = []
    handle = _handle(_with_secret(_bridge(tmp_path)), port_open=True, spawned=spawned)
    assert handle.start() is True
    assert spawned == []
    assert not os.path.exists(handle._pid_file)       # not ours: never stopped


def test_a_closed_port_spawns_the_bridge_and_waits_for_it(tmp_path):
    opened = iter([False, False, False, True, True])
    spawned = []
    handle = _handle(_bridge(tmp_path), port_open=lambda: next(opened, True),
                     spawned=spawned, proc=_Proc(pid=900))
    assert handle.start() is True
    assert len(spawned) == 1
    assert nb._read_pid_file(handle._pid_file)[0] == 900


def test_a_bridge_that_dies_at_once_is_a_failure(tmp_path):
    handle = _handle(_bridge(tmp_path), proc=_Proc(pid=900, exits=1))
    assert handle.start() is False


def test_a_missing_bridge_is_a_failure_not_a_crash(tmp_path):
    spawned = []
    handle = _handle(_bridge(tmp_path, with_python=False), spawned=spawned)
    assert handle.start() is False and spawned == []


def test_the_last_client_out_stops_the_bridge(tmp_path):
    bridge, killed = _bridge(tmp_path), []
    alive = lambda pid: pid in (100, 200, 900)
    opened = iter([False, False, True])    # before the lock, inside it, then up
    first = _handle(bridge, port_open=lambda: next(opened, True), alive=alive,
                    killed=killed, my_pid=100, proc=_Proc(pid=900))
    assert first.start() is True
    second = _handle(bridge, port_open=True, alive=alive, killed=killed, my_pid=200)
    assert second.start() is True

    assert first.stop() is False and killed == []      # 200 is still using it
    assert second.stop() is True and killed == [900]   # the last one out
    assert not os.path.exists(first._pid_file)


def test_dead_clients_do_not_keep_the_bridge_alive(tmp_path):
    bridge, killed = _bridge(tmp_path), []
    alive = lambda pid: pid in (100, 900)              # 200 crashed
    opened = iter([False, False, True])    # before the lock, inside it, then up
    first = _handle(bridge, port_open=lambda: next(opened, True), alive=alive,
                    killed=killed, my_pid=100, proc=_Proc(pid=900))
    first.start()
    _handle(bridge, port_open=True, alive=alive, my_pid=200).start()
    assert first.stop() is True and killed == [900]


def test_a_bridge_started_by_hand_is_never_stopped(tmp_path):
    bridge, killed = _bridge(tmp_path), []
    handle = _handle(bridge, port_open=True, alive=lambda pid: True,
                     killed=killed, my_pid=100)
    handle.start()
    assert handle.stop() is False and killed == []


def test_the_environment_puts_ffmpeg_folders_first(tmp_path):
    bridge = _bridge(tmp_path)
    extra = tmp_path / "ffprobe_bin"
    extra.mkdir()
    with open(os.path.join(bridge, nb.EXTRA_PATH_FILE), "w", encoding="utf-8") as stream:
        stream.write(str(extra) + "\n" + str(tmp_path / "missing") + "\n")
    env = nb.environment(bridge, {"PATH": "C:\\base"})
    parts = env["PATH"].split(os.pathsep)
    assert str(extra) in parts and str(tmp_path / "missing") not in parts
    assert parts[-1] == "C:\\base"


def test_the_environment_drops_the_nodes_installation_id(tmp_path):
    env = nb.environment(_bridge(tmp_path), {
        "PATH": "C:\\base", "WINZAPP_INSTALLATION_ID": "abc",
        "WINZAPP_INSTANCE_ID": "def"})
    assert "WINZAPP_INSTALLATION_ID" not in env
    assert "WINZAPP_INSTANCE_ID" not in env


def test_the_secret_is_read_from_the_bridge_data_folder(tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "secret").write_text("abc123\n", encoding="utf-8")
    assert nb.read_secret(str(tmp_path)) == "abc123"


def test_no_secret_file_means_an_empty_secret(tmp_path):
    assert nb.read_secret(str(tmp_path)) == ""


def test_the_stop_kills_only_the_process_that_was_started(tmp_path):
    bridge, killed = _bridge(tmp_path), []
    opened = iter([False, False, True])    # before the lock, inside it, then up
    handle = _handle(bridge, port_open=lambda: next(opened, True), alive=lambda pid: True,
                     killed=killed, proc=_Proc(pid=900), created=lambda pid: "t1")
    handle.start()
    assert open(handle._pid_file, encoding="utf-8").read().split() == ["900", "t1"]
    assert handle.stop() is True and killed == [900]


def test_a_reused_pid_is_not_killed(tmp_path):
    bridge, killed = _bridge(tmp_path), []
    opened = iter([False, False, True])    # before the lock, inside it, then up
    stamps = iter(["t1", "t2"])                  # at spawn, then at stop
    handle = _handle(bridge, port_open=lambda: next(opened, True), alive=lambda pid: True,
                     killed=killed, proc=_Proc(pid=900), created=lambda pid: next(stamps))
    handle.start()
    assert handle.stop() is False and killed == []
    assert not os.path.exists(handle._pid_file)  # stale: it named another program


def test_an_unverifiable_pid_is_left_alone(tmp_path):
    bridge, killed = _bridge(tmp_path), []
    handle = _handle(bridge, alive=lambda pid: True, killed=killed, my_pid=100)
    os.makedirs(nb.run_dir(bridge))
    with open(handle._pid_file, "w", encoding="utf-8") as stream:
        stream.write("900")                      # written without a creation time
    assert handle.stop() is False and killed == []

    with open(handle._pid_file, "w", encoding="utf-8") as stream:
        stream.write("900\nt1")
    unreadable = _handle(bridge, alive=lambda pid: True, killed=killed, created=lambda pid: "")
    assert unreadable.stop() is False and killed == []
    assert os.path.exists(handle._pid_file)      # not proven stale, kept


def test_the_output_file_is_rotated_when_it_is_too_big():
    moved = []
    assert nb.rotate_output("o.log", 100, getsize=lambda p: 101,
                            replace=lambda a, b: moved.append((a, b))) is True
    assert moved == [("o.log", "o.log.1")]


def test_a_small_output_file_is_kept():
    moved = []
    assert nb.rotate_output("o.log", 100, getsize=lambda p: 100,
                            replace=lambda a, b: moved.append((a, b))) is False
    assert moved == []


def test_rotation_failures_never_stop_the_start():
    def missing(path):
        raise FileNotFoundError(path)

    def locked(a, b):
        raise PermissionError(b)

    assert nb.rotate_output("o.log", getsize=missing) is False
    assert nb.rotate_output("o.log", 1, getsize=lambda p: 2, replace=locked) is False


def test_starting_the_bridge_rotates_its_output_first(tmp_path):
    rotated = []
    handle = _handle(_bridge(tmp_path), proc=_Proc(pid=900), rotate=rotated.append)
    handle.start()
    assert rotated == [os.path.join(nb.run_dir(handle.bridge), nb.OUTPUT_FILE)]


# -- audit F4, M4a and M7 -------------------------------------------------

def test_an_install_with_no_bridge_leaves_no_client_file_behind(tmp_path):
    """A packaged build without a bridge: start() used to register the client
    (and create run/clients) before finding out there was nothing to start."""
    bridge = _bridge(tmp_path, with_python=False)
    handle = _handle(bridge)
    assert handle.start() is False
    assert not os.path.exists(nb.run_dir(bridge))


def test_a_bridge_started_by_hand_is_still_joined_when_the_install_has_no_command(tmp_path):
    bridge = _with_secret(_bridge(tmp_path, with_python=False))
    handle = _handle(bridge, port_open=True)
    assert handle.start() is True
    assert os.path.exists(os.path.join(handle._clients, "100"))


def test_two_clients_starting_together_spawn_one_bridge(tmp_path):
    """Two accounts launched at login: both found the port closed and both
    spawned; the loser failed to bind and bridge.pid could name the dead one."""
    import threading
    import time

    bridge = _with_secret(_bridge(tmp_path))
    state = {"open": False}
    spawned = []

    def start_process(cmd, env, cwd, output=None):
        spawned.append(cmd)
        time.sleep(0.2)             # the window the race lived in
        state["open"] = True
        return _Proc(pid=900)

    def client(pid):
        handle = nb.NeonizeBridge(
            bridge, 6300, is_open=lambda port: state["open"], alive=lambda p: False,
            created=lambda p: "t", rotate=lambda p: False, start_process=start_process,
            kill=lambda p: None, sleep=lambda s: time.sleep(0.01), my_pid=pid,
            identity=_bridge_says())
        results.append(handle.start())

    results = []
    threads = [threading.Thread(target=client, args=(pid,)) for pid in (101, 102)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert results == [True, True]
    assert len(spawned) == 1
    assert nb._read_pid_file(os.path.join(nb.run_dir(bridge), nb.BRIDGE_PID_FILE))[0] == 900


def test_a_start_lock_that_never_frees_is_a_failure_not_a_hang(tmp_path):
    from coord_locks import LockTimeout

    class _Stuck:
        def __init__(self, run):
            pass

        def __enter__(self):
            raise LockTimeout("busy")

        def __exit__(self, *exc):
            return False

    opened = iter([False])          # closed once; a spawn would find it open
    spawned = []
    handle = _handle(_bridge(tmp_path), port_open=lambda: next(opened, True), spawned=spawned)
    handle._lock = _Stuck
    assert handle.start() is False and spawned == []


# -- an open port is only trusted when what listens on it is the bridge

def _write_pid_file(handle, pid, stamp):
    os.makedirs(nb.run_dir(handle.bridge), exist_ok=True)
    with open(handle._pid_file, "w", encoding="utf-8") as stream:
        stream.write(f"{pid}\n{stamp}")


def test_identity_matches_the_pid_bridge_pid_names_or_its_child():
    good = {"protocol_version": 1, "pid": 900, "ppid": 5}
    assert nb.identity_matches(good, 900)
    assert nb.identity_matches({"protocol_version": 1, "pid": 901, "ppid": 900}, 900)  # launcher
    assert not nb.identity_matches({"protocol_version": 1, "pid": 901, "ppid": 5}, 900)


def test_identity_without_a_pid_to_compare_only_needs_the_bridge_shape():
    assert nb.identity_matches({"protocol_version": 1, "pid": 7}, 0)
    for bad in (None, [], "x", {}, {"pid": 7}, {"protocol_version": 1},
                {"protocol_version": "1", "pid": 7}, {"protocol_version": 1, "pid": True}):
        assert not nb.identity_matches(bad, 0)


def test_a_listener_that_is_not_our_bridge_is_refused_and_gets_nothing(tmp_path):
    asked = []
    handle = _handle(_bridge(tmp_path), port_open=True, alive=lambda pid: pid == 900,
                     identity=lambda port: asked.append(port) or {
                         "protocol_version": 1, "pid": 31337, "ppid": 1})
    _write_pid_file(handle, 900, "t900")
    assert handle.start() is False
    assert asked == [6300]


def test_a_listener_that_does_not_answer_like_a_bridge_is_refused(tmp_path):
    handle = _handle(_bridge(tmp_path), port_open=True, identity=lambda port: None)
    assert handle.start() is False


def test_a_listener_whose_identity_check_raises_is_refused(tmp_path):
    def boom(port):
        raise RuntimeError("x")
    assert _handle(_bridge(tmp_path), port_open=True, identity=boom).start() is False


def test_the_bridge_named_in_bridge_pid_is_trusted_even_behind_a_launcher(tmp_path):
    handle = _handle(_bridge(tmp_path), port_open=True, alive=lambda pid: pid == 900,
                     identity=_bridge_says(pid=901, ppid=900))
    _write_pid_file(handle, 900, "t900")
    assert handle.start() is True


def test_a_stale_bridge_pid_does_not_block_a_bridge_started_by_hand(tmp_path):
    # the recorded process is gone, or its pid now belongs to another program
    bridge = _with_secret(_bridge(tmp_path))
    for alive, created in ((lambda pid: False, lambda pid: "t900"),
                           (lambda pid: True, lambda pid: "other")):
        handle = _handle(bridge, port_open=True, alive=alive,
                         created=created, identity=_bridge_says(pid=31337, ppid=1))
        _write_pid_file(handle, 900, "t900")
        assert handle.start() is True


def _handle_whose_port_opens_when_spawned(bridge, **kwargs):
    """A handle whose port is closed until it spawns, then open."""
    state = {"up": False}
    handle = _handle(bridge, port_open=lambda: state["up"], **kwargs)
    real_spawn = handle._spawn

    def spawn(*args, **kw):
        state["up"] = True
        return real_spawn(*args, **kw)

    handle._spawn = spawn
    return handle


def test_a_port_taken_while_we_spawn_is_refused(tmp_path):
    # our process exits (it could not bind) and a stranger answers on the port
    handle = _handle_whose_port_opens_when_spawned(_bridge(tmp_path),
                                                   proc=_Proc(pid=900, exits=1))
    assert handle.start() is False


def test_a_spawned_bridge_that_is_not_the_one_on_the_port_is_refused(tmp_path):
    handle = _handle_whose_port_opens_when_spawned(
        _bridge(tmp_path), proc=_Proc(pid=900), alive=lambda pid: pid == 900,
        identity=_bridge_says(pid=31337, ppid=1))
    assert handle.start() is False


def test_a_spawned_bridge_that_is_the_one_on_the_port_is_used(tmp_path):
    handle = _handle_whose_port_opens_when_spawned(
        _bridge(tmp_path), proc=_Proc(pid=900), alive=lambda pid: pid == 900,
        identity=_bridge_says(pid=901, ppid=900))
    assert handle.start() is True


def test_a_bridge_of_another_installation_is_refused(tmp_path):
    """Two WinZapp folders share port 6300: the second found the first one's bridge
    on it, joined it with no secret and got 401 on every call."""
    handle = _handle(_bridge(tmp_path), port_open=True)        # no bridge.pid, no secret

    assert handle.start() is False


def test_a_bridge_this_install_started_needs_no_secret_file_to_be_recognised(tmp_path):
    handle = _handle(_bridge(tmp_path), port_open=True, alive=lambda pid: pid == 900,
                     identity=_bridge_says(pid=900, ppid=1))
    _write_pid_file(handle, 900, "t900")

    assert handle.start() is True


def test_the_node_and_the_bridge_are_told_apart_by_the_installation_id():
    bridge = {"installation_id": "", "instance_id": "", "protocol_version": 1, "pid": 9, "ppid": 1}
    node = {"installation_id": "abc", "instance_id": "def", "protocol_version": 1, "pid": 9, "ppid": 1}

    assert nb.is_bridge_identity(bridge)
    assert not nb.is_bridge_identity(node)
    for junk in (None, [], "x", {"installation_id": ""}, {"protocol_version": "1"}):
        assert not nb.is_bridge_identity(junk)


NODE_SAYS = lambda port: {"installation_id": "abc", "instance_id": "def",
                          "protocol_version": 1, "pid": 4242, "ppid": 1}


def test_a_node_still_closing_on_the_port_is_waited_for_and_the_bridge_then_starts(tmp_path):
    """Switching to Neonize while the WPPConnect Node of the last run lingers: the
    port was taken for "the bridge", the Node answered 401 to the bridge's routes
    and the account showed connecting, then offline, until the next restart."""
    state = {"asks": 0}

    def port_open():
        state["asks"] += 1
        return state["asks"] <= 4            # open for the first checks, then free

    handle = _handle(_bridge(tmp_path), port_open=port_open, identity=NODE_SAYS)
    spawned = []
    handle._spawn = lambda cmd, env, cwd, output=None: spawned.append(cmd) or _Proc()

    started = handle.start()

    assert spawned, "the bridge was never started"
    assert started in (True, False)          # whether it then answers is not under test here


def test_a_node_that_never_lets_go_leaves_the_bridge_unstarted(tmp_path):
    spawned = []
    handle = _handle(_bridge(tmp_path), port_open=True, identity=NODE_SAYS, spawned=spawned)

    assert handle.start() is False
    assert spawned == []


def _held_by_a_node(killed):
    """A port the Node holds until it is stopped."""
    return lambda: not killed


def test_a_leftover_node_of_this_install_is_stopped_and_the_bridge_starts(tmp_path):
    """Reported 2026-11-12: a run that ended abruptly left its WPPConnect Node
    on the port; switching to Neonize then failed ("could not start the
    Neonize bridge"). No other WinZapp uses it, and it is WinZapp's own
    node.exe: it is stopped and the bridge starts."""
    killed, spawned = [], []
    handle = _handle(_bridge(tmp_path), port_open=_held_by_a_node(killed), identity=NODE_SAYS,
                     killed=killed, spawned=spawned, node_listeners=lambda port: [4242])
    handle.start()
    assert killed == [4242] and spawned, "the leftover Node was not stopped"


def test_a_node_another_account_uses_is_never_stopped(tmp_path):
    killed, spawned = [], []
    handle = _handle(_bridge(tmp_path), port_open=_held_by_a_node(killed), identity=NODE_SAYS,
                     killed=killed, spawned=spawned, node_listeners=lambda port: [4242],
                     node_users=lambda pid: [{"account_id": "other", "pid": 555}])
    assert handle.start() is False
    assert killed == [] and spawned == []


def test_a_program_that_is_not_winzapps_node_is_never_stopped(tmp_path):
    killed = []
    handle = _handle(_bridge(tmp_path), port_open=_held_by_a_node(killed), identity=NODE_SAYS,
                     killed=killed, node_listeners=lambda port: [])
    assert handle.start() is False and killed == []


def test_whose_node_it_is_unreadable_leaves_it_running(tmp_path):
    killed = []

    def broken(pid):
        raise OSError("lease folder unreadable")
    handle = _handle(_bridge(tmp_path), port_open=_held_by_a_node(killed), identity=NODE_SAYS,
                     killed=killed, node_listeners=lambda port: [4242], node_users=broken)
    assert handle.start() is False and killed == []


def test_other_users_are_the_leases_of_other_processes(monkeypatch):
    import node_coord
    leases = [{"account_id": "me", "pid": 100}, {"account_id": "b", "pid": 200},
              {"account_id": "x", "_corrupt": True}]
    monkeypatch.setattr(node_coord, "live_node_leases", lambda gd, is_alive: leases)
    monkeypatch.setattr(nb, "global_dir", lambda: "G")
    others = nb.other_node_users(100)
    assert [o["account_id"] for o in others] == ["b", "x"]   # a corrupt lease counts: unsure


def test_a_foreign_bridge_is_not_waited_for(tmp_path):
    ticks = []
    handle = _handle(_bridge(tmp_path), port_open=True)                 # bridge-shaped, no secret
    handle._sleep = lambda s: ticks.append(s)

    assert handle.start() is False
    assert ticks == []                                                  # refused at once


def _running_bridge(tmp_path, shutdown, alive, killed, secret=True):
    bridge = _with_secret(_bridge(tmp_path)) if secret else _bridge(tmp_path)
    opened = iter([False, False, True])
    handle = _handle(bridge, port_open=lambda: next(opened, True), alive=alive, killed=killed,
                     my_pid=100, proc=_Proc(pid=900), shutdown=shutdown)
    assert handle.start() is True
    return handle


def test_the_last_client_out_asks_the_bridge_to_exit_before_any_kill(tmp_path):
    killed, asked, gone = [], [], []

    def shutdown(port, secret):
        asked.append((port, secret))
        gone.append(True)               # the bridge exits on its own
        return True

    handle = _running_bridge(tmp_path, shutdown,
                             lambda pid: pid == 100 or (pid == 900 and not gone), killed)
    assert handle.stop() is True
    assert asked == [(6300, "secret")] and killed == []
    assert not os.path.exists(handle._pid_file)


def test_a_bridge_that_does_not_exit_after_the_request_is_killed(tmp_path):
    killed = []
    handle = _running_bridge(tmp_path, lambda port, secret: True,
                             lambda pid: pid in (100, 900), killed)
    assert handle.stop() is True and killed == [900]


def test_a_refused_shutdown_request_falls_back_to_the_kill(tmp_path):
    killed = []
    handle = _running_bridge(tmp_path, lambda port, secret: False,
                             lambda pid: pid in (100, 900), killed)
    assert handle.stop() is True and killed == [900]


def test_without_a_secret_there_is_no_request_and_the_kill_stands(tmp_path):
    killed, asked = [], []
    handle = _running_bridge(tmp_path, lambda port, secret: asked.append(1) or True,
                             lambda pid: pid in (100, 900), killed, secret=False)
    assert handle.stop() is True and killed == [900] and asked == []


def test_request_shutdown_posts_to_the_loopback_route_without_proxies(monkeypatch):
    import requests
    seen = {}

    class _Session:
        trust_env = True

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, timeout):
            seen["url"], seen["timeout"], seen["trust_env"] = url, timeout, self.trust_env
            return type("R", (), {"status_code": 200})()

    monkeypatch.setattr(requests, "Session", _Session)
    assert nb.request_shutdown(6300, "s3cret") is True
    assert seen["url"] == "http://127.0.0.1:6300/api/s3cret/shutdown" and seen["trust_env"] is False


def test_request_shutdown_reports_false_on_an_error(monkeypatch):
    import requests

    class _Broken:
        def __enter__(self):
            raise requests.ConnectionError("down")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(requests, "Session", _Broken)
    assert nb.request_shutdown(6300, "s3cret") is False
