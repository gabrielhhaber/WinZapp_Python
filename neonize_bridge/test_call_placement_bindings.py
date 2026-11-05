"""Lifecycle binding contract against a fake ctypes target, never a DLL."""
import ctypes
import json
from types import SimpleNamespace as N

import pytest

from test_call_pcm48_bindings import binding


def native(value=None, result=None):
    def info(uuid, call_id, buffer, capacity):
        if result is not None:
            return result
        data = json.dumps(value).encode()
        ctypes.memmove(buffer, data, len(data))
        return len(data)
    return N(CallInfo=info)


def test_lifecycle_snapshot_is_loaded_from_the_exact_native_response(monkeypatch):
    value = {"generation": 17, "peerAccepted": True, "ended": False, "phase": 4}
    layer = binding(monkeypatch, native(value))
    assert layer.Calls(b"fake").placement_state("C") == value


def test_only_the_no_call_status_means_terminal(monkeypatch):
    layer = binding(monkeypatch, native(result=-2))
    assert layer.Calls(b"fake").placement_state("C") is None


@pytest.mark.parametrize("value", [{}, [], {"video": False},
    {"generation": True, "peerAccepted": False, "ended": False, "phase": 4},
    {"generation": 0, "peerAccepted": False, "ended": False, "phase": 4},
    {"generation": 17, "peerAccepted": 1, "ended": False, "phase": 4},
    {"generation": 17, "peerAccepted": False, "ended": 0, "phase": 4},
    {"generation": 17, "peerAccepted": False, "ended": False, "phase": True}])
def test_legacy_or_malformed_snapshot_is_not_mistaken_for_an_ended_call(monkeypatch, value):
    layer = binding(monkeypatch, native(value))
    with pytest.raises(layer.CallError):
        layer.Calls(b"fake").placement_state("C")


@pytest.mark.parametrize("status", [0, -3, -4, 4097])
def test_other_native_statuses_are_contract_errors(monkeypatch, status):
    layer = binding(monkeypatch, native(result=status))
    with pytest.raises(layer.CallError):
        layer.Calls(b"fake").placement_state("C")
