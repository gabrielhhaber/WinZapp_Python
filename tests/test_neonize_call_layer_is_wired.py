"""The neonize patch series must hook the call layer into goneonize/main.go.

calls.go defines `instalarChamadas`, but nothing runs it unless `Neonize()` calls
it right after the client is created. Porting the series to neonize 0.10.0
dropped that one line: the DLL still compiled, `CallsEnabled` answered 0 and the
bridge reported `calls: False`, so the client hid every call button (issue #731).
A compile never notices, so the series is checked for the hook itself."""

from pathlib import Path

PATCHES = Path(__file__).resolve().parent.parent / "neonize_bridge" / "neonize_patches" / "neonize"


def _added_to_main_go():
    added = []
    for patch in sorted(PATCHES.glob("*.patch")):
        in_main = False
        for line in patch.read_text(encoding="utf-8").splitlines():
            if line.startswith("diff --git "):
                in_main = line.endswith("b/goneonize/main.go")
            elif in_main and line.startswith("+") and not line.startswith("+++"):
                added.append(line[1:].strip())
    return added


def test_neonize_installs_the_call_layer_when_it_creates_a_client():
    assert "instalarChamadas(uuid)" in _added_to_main_go()


def test_stopping_a_client_removes_its_call_layer():
    assert any(line.startswith("removerChamadas(") for line in _added_to_main_go())
