import ast
from pathlib import Path
from types import SimpleNamespace as N

from core.call_media_watchdog import CallMediaWatchdog, media_stalled
from main_window.call_media_stall import CallMediaStallMixin


def sample(at, received=0, timed_out=True, connected=True, call="c1"):
    return {"at": at, "callId": call, "bytesReceived": received,
            "remotes": [{"connected": connected, "timedOut": timed_out, "packets": 0}]}


def test_deaf_call_is_flagged_after_the_window():
    samples = [sample(t) for t in range(0, 19, 3)]
    assert media_stalled(samples, 18.0)


def test_not_flagged_before_the_window_has_passed():
    assert not media_stalled([sample(0), sample(3), sample(6)], 6.0)


def test_bytes_still_arriving_is_healthy():
    samples = [sample(t, received=t * 100) for t in range(0, 19, 3)]
    assert not media_stalled(samples, 18.0)


def test_remote_not_timed_out_is_healthy():
    assert not media_stalled([sample(t, timed_out=False) for t in range(0, 19, 3)], 18.0)


def test_nobody_connected_is_not_a_stall():
    assert not media_stalled([sample(t, connected=False) for t in range(0, 19, 3)], 18.0)


def test_one_good_sample_breaks_the_stall():
    samples = [sample(t) for t in range(0, 19, 3)]
    samples[3] = sample(9, timed_out=False)
    assert not media_stalled(samples, 18.0)


def test_alerts_once_then_reports_recovery():
    dog = CallMediaWatchdog()
    verdicts = [dog.feed(sample(0), t) for t in range(0, 40, 3)]
    assert verdicts.count("stalled") == 1
    assert dog.feed(sample(0, received=5000, timed_out=False), 42.0) == "recovered"


def test_a_new_call_starts_afresh():
    dog = CallMediaWatchdog()
    for t in range(0, 40, 3):
        dog.feed(sample(0), t)
    assert dog.feed(sample(0, call="c2"), 41.0) is None


def test_main_window_speaks_the_alert_once(monkeypatch):
    spoken = []
    win = N(_active_voice_call={"id": "c1"}, i18n=N(t=lambda key: key),
            output=lambda text, interrupt=False: spoken.append(text))
    clock = iter(range(0, 60, 3))
    monkeypatch.setattr("main_window.call_media_stall.time.monotonic", lambda: next(clock))
    for _ in range(20):
        CallMediaStallMixin.on_call_media_sample(win, sample(0))
    assert spoken == ["call_media_stalled"]


def test_sample_without_an_active_call_is_ignored():
    win = N(_active_voice_call=None, i18n=N(t=lambda key: key), output=lambda *a, **k: 1 / 0)
    CallMediaStallMixin.on_call_media_sample(win, sample(0))
    assert win._call_media_watchdog is None


def test_socket_forwards_the_sample_to_the_main_window(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "client" / "core" / "websocket_client.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    fn = next(n for c in tree.body if isinstance(c, ast.ClassDef) for n in c.body
              if isinstance(n, ast.FunctionDef) and n.name == "on_wpp_call_media")
    got = []
    wx = N(CallAfter=lambda f, *a: got.append((f, a)))
    scope = {"wx": wx, "logging": __import__("logging")}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(source), "exec"), scope)
    main = N(on_call_media_sample=lambda s: None)
    ws = N(main_window=main, _belongs_to_this_session=lambda d: True)
    scope["on_wpp_call_media"](ws, {"data": {"callId": "c1", "bytesReceived": 3}})
    assert got and got[0][1] == ({"callId": "c1", "bytesReceived": 3},)
