import io

import startup_trace


def test_a_mark_is_one_flushed_line_with_the_time_since_the_start():
    out = io.StringIO()
    startup_trace.mark("loading neonize", out)
    assert out.getvalue().startswith("bridge start +") and out.getvalue().endswith("loading neonize\n")


def test_line_format():
    assert startup_trace.line("x", 1.26) == "bridge start +1.3s: x"


def test_a_closed_stream_never_stops_the_start():
    out = io.StringIO()
    out.close()
    startup_trace.mark("x", out)


def test_a_start_that_hangs_dumps_the_stack_and_a_finished_one_does_not(tmp_path):
    import time
    stuck = tmp_path / "stuck.txt"
    with open(stuck, "w") as stream:
        assert startup_trace.watch(0.05, stream, repeat=False) is True
        time.sleep(0.4)
        startup_trace.done()
    assert "File" in stuck.read_text()
    fine = tmp_path / "fine.txt"
    with open(fine, "w") as stream:
        startup_trace.watch(0.3, stream, repeat=False)
        startup_trace.done()
        time.sleep(0.5)
    assert fine.read_text() == ""


def test_describe_names_the_go_settings_when_there_are_any():
    assert startup_trace.describe({"GODEBUG": "asyncpreemptoff=1"}).endswith(", GODEBUG=asyncpreemptoff=1")
    assert "GODEBUG" not in startup_trace.describe({})
