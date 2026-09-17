"""The pure rules behind mirroring phone-side deletions (core/remote_reconcile.py).

The reconcile decides what "WhatsApp Web does not have this message" is allowed
to mean. Both have to hold together, and each was a real case:

- a message absent from one read must not be deleted on the spot
  (199 messages removed at once from an open group, 2026-09-15);
- a real deletion must still arrive, including a clear made on the phone while
  WinZapp was closed with new messages since, and a deletion of the OLDEST
  messages of a short chat — nothing v1.1.1.0 mirrored may stop being mirrored.
"""

from core.remote_reconcile import (
    MAX_MIRRORED_DELETIONS,
    observe_deletions,
    older_than_window,
    oldest_anchor,
    serialized_id,
    split_deletions,
)


def _msg(mid, ts):
    return {"key": {"id": mid}, "messageTimestamp": ts}


class TestOlderThanWindow:
    def test_records_at_or_before_the_window_start_and_not_in_it(self):
        local = [_msg("a", 100), _msg("b", 200), _msg("c", 300), _msg("d", 400)]
        assert [m["key"]["id"] for m in older_than_window(local, {"b", "d"}, 300)] == ["a", "c"]

    def test_no_window_start_means_nothing_to_settle(self):
        assert older_than_window([_msg("a", 100)], {"x"}, 0) == []


class TestAnchors:
    def test_serialized_id_reads_the_raw_item(self):
        assert serialized_id({"id": {"_serialized": "false_1@g.us_ABC_2@lid"}}) == "false_1@g.us_ABC_2@lid"
        assert serialized_id({"id": "true_1@c.us_XYZ"}) == "true_1@c.us_XYZ"
        assert serialized_id({"id": 5}) == ""
        assert serialized_id(None) == ""

    def test_the_anchor_is_the_oldest_message_of_the_page(self):
        pairs = [
            (_msg("b", 200), {"id": {"_serialized": "ser_b"}}),
            (_msg("a", 100), {"id": {"_serialized": "ser_a"}}),
            (_msg("c", 300), {"id": {"_serialized": "ser_c"}}),
        ]
        assert oldest_anchor(pairs) == "ser_a"

    def test_a_message_without_a_serialized_id_cannot_be_the_anchor(self):
        pairs = [(_msg("a", 100), {"id": 7}), (_msg("b", 200), {"id": {"_serialized": "ser_b"}})]
        assert oldest_anchor(pairs) == "ser_b"
        assert oldest_anchor([]) == ""


class TestSplitDeletions:
    def test_a_handful_of_direct_deletions_mirror_at_once(self):
        direct = {str(i) for i in range(MAX_MIRRORED_DELETIONS)}
        assert split_deletions(direct, set()) == (direct, set())

    def test_more_than_the_cap_waits_for_confirmation(self):
        direct = {str(i) for i in range(MAX_MIRRORED_DELETIONS + 1)}
        assert split_deletions(direct, {"old"}) == (set(), direct | {"old"})

    def test_an_inference_always_waits_even_beside_a_direct_one(self):
        assert split_deletions({"new"}, {"old"}) == ({"new"}, {"old"})


class TestObserveDeletions:
    def test_confirms_only_after_the_threshold(self):
        state = {}
        assert observe_deletions(state, "j", {"a", "b"}, 3) == set()
        assert observe_deletions(state, "j", {"a", "b"}, 3) == set()
        assert observe_deletions(state, "j", {"a", "b"}, 3) == {"a", "b"}
        assert "j" not in state

    def test_only_what_was_missing_every_time_is_confirmed(self):
        state = {}
        observe_deletions(state, "j", {"a", "b", "c"}, 3)
        observe_deletions(state, "j", {"a", "b"}, 3)
        assert observe_deletions(state, "j", {"a", "b", "d"}, 3) == {"a", "b"}

    def test_a_disjoint_poll_starts_a_new_run(self):
        state = {}
        observe_deletions(state, "j", {"a"}, 3)
        observe_deletions(state, "j", {"a"}, 3)
        observe_deletions(state, "j", {"z"}, 3)
        assert state["j"][:2] == ({"z"}, 1)

    def test_an_empty_poll_clears_the_run(self):
        state = {}
        observe_deletions(state, "j", {"a"}, 3)
        assert observe_deletions(state, "j", set(), 3) == set()
        assert "j" not in state

    def test_runs_are_per_chat(self):
        state = {}
        observe_deletions(state, "one", {"a"}, 2)
        assert observe_deletions(state, "two", {"a"}, 2) == set()
        assert observe_deletions(state, "one", {"a"}, 2) == {"a"}
