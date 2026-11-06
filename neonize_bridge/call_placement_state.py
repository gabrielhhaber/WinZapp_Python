"""Recover native placement state without holding a lock across dialing."""


class PlacementStateError(RuntimeError):
    """Native ownership/state could not be established; never a user input error."""


class PlacementReplacedError(PlacementStateError):
    """Another native attempt owns this call id now: it is not ours to hang up."""


def placement_state(calls, call_id, result):
    """Return (generation, snapshot), or None for the explicit legacy path.

    A missing native call is terminal; malformed or unsupported snapshots are
    contract errors. Never infer an attempt's identity from a later map lookup.
    """
    if result is None or "generation" not in result:
        return None
    generation = result["generation"]
    if type(generation) is not int or not 0 < generation < 2**64:
        raise PlacementStateError("invalid native placement generation")
    getter = getattr(calls, "placement_state", None)
    if not callable(getter):
        raise PlacementStateError("native placement snapshot unavailable")
    value = getter(call_id)
    if value is None:
        return generation, None
    if isinstance(value, dict) and type(value.get("generation")) is int \
            and value["generation"] != generation:
        raise PlacementReplacedError("native placement snapshot belongs to another attempt")
    if (not isinstance(value, dict) or type(value.get("generation")) is not int
            or type(value.get("peerAccepted")) is not bool
            or type(value.get("ended")) is not bool
            or type(value.get("phase")) is not int
            or not 0 <= value["phase"] <= 8):
        raise PlacementStateError("native placement snapshot does not match the attempt")
    return generation, value
