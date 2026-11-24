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
